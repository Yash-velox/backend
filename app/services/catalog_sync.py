from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.shop_resolver import create_shopify_graphql_client
from app.models import (
    Product,
    ProductMedia,
    ProductVariant,
    Shop,
    ShopifyFile,
    SyncRun,
    SyncRunStatus,
    SyncRunType,
)
from app.services.shopify_graphql import ShopifyGraphQLError
from app.services.snapshot import media_fingerprint, normalize_shopify_product_node

logger = logging.getLogger("app.services.catalog_sync")


class CatalogSyncService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def _client(self):
        return create_shopify_graphql_client(self.db, self.shop)

    def get_latest_run(self) -> SyncRun | None:
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.shop_id == self.shop.id)
            .order_by(SyncRun.created_at.desc())
            .first()
        )

    def list_recent_runs(self, *, limit: int = 20) -> list[SyncRun]:
        limit = min(max(limit, 1), 100)
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.shop_id == self.shop.id)
            .order_by(SyncRun.created_at.desc())
            .limit(limit)
            .all()
        )

    def start_full_sync(self) -> SyncRun:
        now = datetime.now(timezone.utc)
        run = SyncRun(
            shop_id=self.shop.id,
            run_type=SyncRunType.FULL,
            status=SyncRunStatus.RUNNING,
            started_at=now,
            products_synced=0,
            media_synced=0,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        products_synced = 0
        media_synced = 0
        cursor: str | None = None

        try:
            client = self._client()
            while True:
                page = client.fetch_products_page(cursor=cursor, first=25)
                nodes = page.get("products") or []
                for node in nodes:
                    product = self.upsert_product_from_shopify_node(node)
                    products_synced += 1
                    media_synced += len([m for m in product.media if m.is_active])
                run.products_synced = products_synced
                run.media_synced = media_synced
                run.cursor = cursor
                self.db.commit()

                page_info = page.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                if not cursor:
                    break

            run.status = SyncRunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            self.db.commit()
            try:
                from app.services.prompt_product_types import PromptProductTypeService

                PromptProductTypeService(self.db, self.shop).sync_shopify_product_types()
                self.db.commit()
            except Exception:
                logger.exception(
                    "Prompt product-type sync after catalog sync failed | shop=%s",
                    self.shop.id,
                )
            logger.info(
                "Full sync completed | shop=%s run=%s products=%s media=%s",
                self.shop.id,
                run.id,
                products_synced,
                media_synced,
            )
        except ShopifyGraphQLError as exc:
            self._fail_run(run, str(exc))
            logger.error("Full sync failed | shop=%s run=%s error=%s", self.shop.id, run.id, exc)
            raise
        except Exception as exc:
            self._fail_run(run, str(exc)[:2000])
            logger.exception("Full sync crashed | shop=%s run=%s", self.shop.id, run.id)
            raise

        self.db.refresh(run)
        return run

    def _fail_run(self, run: SyncRun, error_message: str) -> None:
        """Mark run FAILED even if the session is poisoned by a prior IntegrityError."""
        run_id = run.id
        try:
            self.db.rollback()
        except Exception:
            logger.exception("Rollback after sync failure failed | run=%s", run_id)
        fresh = self.db.get(SyncRun, run_id)
        if fresh is None:
            return
        fresh.status = SyncRunStatus.FAILED
        fresh.error_message = error_message
        fresh.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        # Keep caller's object in sync for API response shaping.
        run.status = fresh.status
        run.error_message = fresh.error_message
        run.completed_at = fresh.completed_at

    def upsert_product_from_shopify_node(self, node: dict[str, Any]) -> Product:
        normalized = normalize_shopify_product_node(node)
        product_data = normalized["product"]
        variants_data = normalized["variants"]
        media_data = normalized["media"]
        now = datetime.now(timezone.utc)

        product = (
            self.db.query(Product)
            .filter(
                Product.shop_id == self.shop.id,
                Product.shopify_product_gid == product_data["product_gid"],
            )
            .one_or_none()
        )
        if product is None:
            product = Product(
                shop_id=self.shop.id,
                shopify_product_gid=product_data["product_gid"],
            )
            self.db.add(product)

        product.shopify_numeric_id = product_data.get("numeric_id")
        product.title = product_data.get("title")
        product.description_html = product_data.get("description_html")
        product.handle = product_data.get("handle")
        product.status = product_data.get("status")
        product.product_type = product_data.get("product_type")
        product.vendor = product_data.get("vendor")
        product.tags = product_data.get("tags")
        product.shopify_updated_at = product_data.get("updated_at")
        product.synced_at = now
        product.is_deleted = False
        product.raw_snapshot_json = node
        self.db.flush()

        self._upsert_variants(product, variants_data, now)
        self._upsert_media(product, media_data, now)
        self.db.flush()
        self._refresh_has_images(product)
        self.db.flush()
        self.db.refresh(product)
        return product

    def _refresh_has_images(self, product: Product) -> None:
        """Set products.has_images from eligible media (same rule as manual batch / picker)."""
        has = (
            self.db.query(ProductMedia.id)
            .filter(
                ProductMedia.product_id == product.id,
                ProductMedia.is_active.is_(True),
                ProductMedia.is_visible.is_(True),
                ProductMedia.cdn_url.isnot(None),
                ProductMedia.cdn_url != "",
            )
            .first()
            is not None
        )
        product.has_images = has

    def _upsert_variants(self, product: Product, variants_data: list[dict[str, Any]], now: datetime) -> None:
        seen_gids: set[str] = set()
        for vdata in variants_data:
            gid = vdata.get("variant_gid")
            if not gid:
                continue
            seen_gids.add(gid)
            variant = (
                self.db.query(ProductVariant)
                .filter(
                    ProductVariant.shop_id == self.shop.id,
                    ProductVariant.shopify_variant_gid == gid,
                )
                .one_or_none()
            )
            if variant is None:
                variant = ProductVariant(
                    shop_id=self.shop.id,
                    product_id=product.id,
                    shopify_variant_gid=gid,
                )
                self.db.add(variant)
            variant.product_id = product.id
            variant.sku = vdata.get("sku")
            variant.title = vdata.get("title")
            variant.shopify_updated_at = vdata.get("updated_at")

        existing = (
            self.db.query(ProductVariant)
            .filter(ProductVariant.product_id == product.id)
            .all()
        )
        for variant in existing:
            if variant.shopify_variant_gid not in seen_gids:
                self.db.delete(variant)

    def _upsert_shopify_file(self, file_gid: str | None, mdata: dict[str, Any], now: datetime) -> ShopifyFile | None:
        if not file_gid:
            return None
        sf = (
            self.db.query(ShopifyFile)
            .filter(
                ShopifyFile.shop_id == self.shop.id,
                ShopifyFile.shopify_file_gid == file_gid,
            )
            .one_or_none()
        )
        if sf is None:
            sf = ShopifyFile(shop_id=self.shop.id, shopify_file_gid=file_gid)
            self.db.add(sf)
        sf.filename = mdata.get("filename")
        sf.url = mdata.get("cdn_url")
        sf.mime_type = mdata.get("mime_type")
        sf.width = mdata.get("width")
        sf.height = mdata.get("height")
        sf.shopify_updated_at = mdata.get("updated_at")
        sf.synced_at = now
        fp = media_fingerprint(
            {
                "media_gid": mdata.get("media_gid"),
                "file_gid": file_gid,
                "cdn_url": mdata.get("cdn_url"),
                "filename": mdata.get("filename"),
                "width": mdata.get("width"),
                "height": mdata.get("height"),
                "mime_type": mdata.get("mime_type"),
                "updated_at": (
                    mdata.get("updated_at").isoformat()
                    if isinstance(mdata.get("updated_at"), datetime)
                    else mdata.get("updated_at")
                ),
            }
        )
        sf.content_fingerprint = fp
        self.db.flush()
        return sf

    def _upsert_media(self, product: Product, media_data: list[dict[str, Any]], now: datetime) -> None:
        seen_gids: set[str] = set()
        for mdata in media_data:
            media_gid = mdata.get("media_gid")
            if not media_gid:
                continue
            seen_gids.add(media_gid)
            sf = self._upsert_shopify_file(mdata.get("file_gid"), mdata, now)
            updated = mdata.get("updated_at")
            fp = media_fingerprint(
                {
                    "media_gid": media_gid,
                    "file_gid": mdata.get("file_gid"),
                    "cdn_url": mdata.get("cdn_url"),
                    "filename": mdata.get("filename"),
                    "width": mdata.get("width"),
                    "height": mdata.get("height"),
                    "mime_type": mdata.get("mime_type"),
                    "updated_at": updated.isoformat() if isinstance(updated, datetime) else updated,
                }
            )
            media = (
                self.db.query(ProductMedia)
                .filter(
                    ProductMedia.shop_id == self.shop.id,
                    ProductMedia.product_id == product.id,
                    ProductMedia.shopify_media_gid == media_gid,
                )
                .one_or_none()
            )
            if media is None:
                media = ProductMedia(
                    shop_id=self.shop.id,
                    product_id=product.id,
                    shopify_media_gid=media_gid,
                )
                self.db.add(media)
            media.shopify_file_id = sf.id if sf else None
            media.shopify_file_gid = mdata.get("file_gid")
            media.cdn_url = mdata.get("cdn_url")
            media.original_filename = mdata.get("filename")
            media.width = mdata.get("width")
            media.height = mdata.get("height")
            media.mime_type = mdata.get("mime_type")
            media.alt_text = mdata.get("alt_text")
            media.position = mdata.get("position")
            media.is_primary = bool(mdata.get("is_primary"))
            media.is_visible = bool(mdata.get("is_visible", True))
            media.variant_gids_json = mdata.get("variant_gids") or []
            media.content_fingerprint = fp
            media.shopify_updated_at = updated if isinstance(updated, datetime) else None
            media.is_active = True
            self.db.flush()
            from app.services.image_versions import ImageVersionsService

            ImageVersionsService(self.db, self.shop).ensure_original_from_media(
                media, actor_type="catalog_sync"
            )

        existing = (
            self.db.query(ProductMedia)
            .filter(ProductMedia.product_id == product.id)
            .all()
        )
        for media in existing:
            if media.shopify_media_gid not in seen_gids:
                media.is_active = False

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.core.shop_resolver import get_shop_by_domain, resolve_shop_access_token
from app.models import Product, WebhookEvent, WebhookProcessingResult
from app.services.catalog_sync import CatalogSyncService
from app.services.primary_batch import PrimaryBatchService
from app.services.secondary_queue import SecondaryQueueService
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError
from app.services.snapshot import (
    media_snapshots_from_shopify,
    normalize_shopify_product_node,
    product_snapshot_from_shopify,
)

logger = logging.getLogger("app.services.webhook_intake")

_NUMERIC_RE = re.compile(r"^\d+$")
_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/\d+$")


def product_gid_from_webhook_payload(payload: dict[str, Any]) -> str | None:
    admin_gid = payload.get("admin_graphql_api_id")
    if isinstance(admin_gid, str) and admin_gid.strip():
        return admin_gid.strip()
    numeric_id = payload.get("id")
    if numeric_id is not None:
        text = str(numeric_id).strip()
        if _NUMERIC_RE.match(text):
            return f"gid://shopify/Product/{text}"
    return None


def normalize_status(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value).strip().upper() or None


def is_draft_transition(
    *,
    previous_status: str | None,
    new_status: str | None,
) -> bool:
    """True when the update transitions the product to DRAFT."""
    prev = normalize_status(previous_status)
    new = normalize_status(new_status)
    return new == "DRAFT" and prev != "DRAFT"


def detect_status_only(old_product: Product | None, new_payload: dict[str, Any]) -> bool:
    """
    Return True when only product status changed between stored product and webhook payload.

    Non-status product fields compared when present on the webhook payload.
    """
    if old_product is None:
        return False

    new_status = normalize_status(new_payload.get("status"))
    old_status = normalize_status(old_product.status)
    if new_status == old_status:
        return False

    comparisons: list[tuple[Any, Any]] = [
        (new_payload.get("title"), old_product.title),
        (new_payload.get("body_html") or new_payload.get("descriptionHtml"), old_product.description_html),
        (new_payload.get("handle"), old_product.handle),
        (new_payload.get("product_type") or new_payload.get("productType"), old_product.product_type),
        (new_payload.get("vendor"), old_product.vendor),
    ]

    tags = new_payload.get("tags")
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)
    comparisons.append((tags, old_product.tags))

    for new_val, old_val in comparisons:
        if new_val is None:
            continue
        if str(new_val).strip() != str(old_val or "").strip():
            return False

    images = new_payload.get("images")
    if images is not None and len(images) > 0:
        return False

    image = new_payload.get("image")
    if image is not None:
        return False

    return True


def _product_snapshot_from_webhook(payload: dict[str, Any], product_gid: str) -> dict[str, Any]:
    tags = payload.get("tags")
    if isinstance(tags, list):
        tags_str = ", ".join(str(t) for t in tags)
    else:
        tags_str = tags
    numeric_id = None
    if payload.get("id") is not None:
        numeric_id = str(payload.get("id"))
    return {
        "product_gid": product_gid,
        "numeric_id": numeric_id,
        "title": payload.get("title"),
        "description_html": payload.get("body_html"),
        "handle": payload.get("handle"),
        "status": payload.get("status"),
        "product_type": payload.get("product_type"),
        "vendor": payload.get("vendor"),
        "tags": tags_str,
        "updated_at": payload.get("updated_at"),
    }


def _media_snapshot_from_webhook(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        featured = payload.get("image")
        if isinstance(featured, dict):
            images = [featured]
        else:
            return None

    media_rows: list[dict[str, Any]] = []
    for position, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        admin_gid = img.get("admin_graphql_api_id")
        media_gid = admin_gid
        if not media_gid and img.get("id") is not None:
            media_gid = f"gid://shopify/MediaImage/{img.get('id')}"
        src = img.get("src") or img.get("url")
        if not src:
            continue
        media_rows.append(
            {
                "media_gid": media_gid,
                "file_gid": None,
                "cdn_url": src,
                "filename": src.split("?", 1)[0].rsplit("/", 1)[-1],
                "width": img.get("width"),
                "height": img.get("height"),
                "mime_type": None,
                "alt_text": img.get("alt"),
                "position": position,
                "is_primary": position == 0,
                "is_visible": True,
                "updated_at": img.get("updated_at"),
            }
        )
    return media_snapshots_from_shopify(media_rows) if media_rows else None


class WebhookIntakeService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record_and_process_products_update(
        self,
        shop_domain: str,
        webhook_id: str,
        topic: str,
        payload: dict[str, Any],
        raw_hash: str,
    ) -> WebhookEvent:
        existing = (
            self.db.query(WebhookEvent)
            .filter(WebhookEvent.shopify_webhook_id == webhook_id)
            .one_or_none()
        )
        if existing:
            logger.info("Duplicate webhook ignored | webhook_id=%s shop=%s", webhook_id, shop_domain)
            return existing

        shop = get_shop_by_domain(self.db, shop_domain)
        product_gid = product_gid_from_webhook_payload(payload)

        event = WebhookEvent(
            shop_id=shop.id if shop else None,
            shopify_webhook_id=webhook_id,
            topic=topic,
            shopify_product_gid=product_gid,
            payload_hash=raw_hash,
            processing_result=WebhookProcessingResult.ACCEPTED,
        )
        self.db.add(event)

        if shop is None:
            event.processing_result = WebhookProcessingResult.IGNORED
            event.error_summary = "Shop not found"
            self.db.commit()
            self.db.refresh(event)
            return event

        if not product_gid or not _GID_PRODUCT_RE.match(product_gid):
            event.processing_result = WebhookProcessingResult.FAILED
            event.error_summary = "Missing or invalid product GID in webhook payload"
            self.db.commit()
            self.db.refresh(event)
            return event

        try:
            self._process_product_update(shop, product_gid, payload, webhook_id, event)
        except Exception as exc:
            logger.exception(
                "Webhook processing failed | shop=%s webhook=%s product=%s",
                shop.shop_domain,
                webhook_id,
                product_gid,
            )
            # Catalog/image-version flushes can leave the session aborted; reset before
            # persisting the FAILED webhook row so Shopify does not keep retrying a 500.
            self.db.rollback()
            event = WebhookEvent(
                shop_id=shop.id,
                shopify_webhook_id=webhook_id,
                topic=topic,
                shopify_product_gid=product_gid,
                payload_hash=raw_hash,
                processing_result=WebhookProcessingResult.FAILED,
                error_summary=str(exc)[:2000],
            )
            self.db.add(event)
            try:
                self.db.commit()
                self.db.refresh(event)
            except Exception:
                self.db.rollback()
                logger.exception(
                    "Failed to persist webhook failure row | shop=%s webhook=%s",
                    shop.shop_domain,
                    webhook_id,
                )
            return event

        self.db.commit()
        self.db.refresh(event)
        return event

    def _process_product_update(
        self,
        shop,
        product_gid: str,
        payload: dict[str, Any],
        webhook_id: str,
        event: WebhookEvent,
    ) -> None:
        catalog = CatalogSyncService(self.db, shop)
        old_product = (
            self.db.query(Product)
            .options(selectinload(Product.media))
            .filter(Product.shop_id == shop.id, Product.shopify_product_gid == product_gid)
            .one_or_none()
        )
        previous_status = old_product.status if old_product else None
        new_status = payload.get("status")

        status_only = detect_status_only(old_product, payload)
        draft_transition = is_draft_transition(previous_status=previous_status, new_status=new_status)

        # REST webhook images use ProductImage GIDs; catalog/baselines use MediaImage.
        # Always refresh media via GraphQL so Secondary Queue delta identity matches.
        #
        # Freeze an empty ProcessingBaseline from *pre-sync* catalog media before upsert.
        # Conversion used to seed empty baselines from the post-upsert catalog, which
        # already included newly added images — Secondary Queue then skipped with
        # "No eligible new or replaced images detected".
        #
        # Failures here must not fail the webhook (Shopify would retry 5xx). Worst case
        # we keep the old skip behavior until the next update.
        if old_product is not None:
            try:
                PrimaryBatchService(self.db, shop).seed_empty_baseline_from_product_media(old_product)
            except Exception:
                logger.exception(
                    "Baseline pre-seed failed; continuing webhook | shop=%s product=%s",
                    shop.id,
                    product_gid,
                )

        product_snapshot = _product_snapshot_from_webhook(payload, product_gid)
        media_snapshot: list[dict[str, Any]] = []
        graphql_ok = False

        try:
            token = resolve_shop_access_token(shop, db=self.db)
            client = ShopifyGraphQLClient(shop_domain=shop.shop_domain, access_token=token)
            node = client.fetch_product_by_gid(product_gid)
            if node:
                normalized = normalize_shopify_product_node(node)
                # Prefer GraphQL product fields when available; keep REST for status-only compare above.
                product_snapshot = product_snapshot_from_shopify(normalized["product"])
                media_snapshot = media_snapshots_from_shopify(normalized["media"])
                catalog.upsert_product_from_shopify_node(node)
                graphql_ok = True
            elif old_product is None:
                event.processing_result = WebhookProcessingResult.FAILED
                event.error_summary = "Product not found via GraphQL"
                return
        except ShopifyGraphQLError as exc:
            logger.error(
                "GraphQL refresh failed during webhook | shop=%s product=%s error=%s",
                shop.id,
                product_gid,
                exc,
            )
            if old_product is None:
                event.processing_result = WebhookProcessingResult.FAILED
                event.error_summary = str(exc)[:2000]
                return

        if not graphql_ok:
            # Known product: apply REST product fields; fall back to REST media only if needed.
            if old_product:
                old_product.title = product_snapshot.get("title") or old_product.title
                old_product.description_html = product_snapshot.get("description_html") or old_product.description_html
                old_product.handle = product_snapshot.get("handle") or old_product.handle
                old_product.status = product_snapshot.get("status") or old_product.status
                old_product.product_type = product_snapshot.get("product_type") or old_product.product_type
                old_product.vendor = product_snapshot.get("vendor") or old_product.vendor
                old_product.tags = product_snapshot.get("tags") or old_product.tags
            rest_media = _media_snapshot_from_webhook(payload)
            media_snapshot = rest_media if rest_media is not None else []

        if old_product and new_status is not None:
            old_product.status = str(new_status)

        SecondaryQueueService(self.db, shop).upsert_from_webhook(
            product_gid,
            product_snapshot,
            media_snapshot,
            webhook_id,
            previous_status=previous_status,
            new_status=new_status,
            is_status_only=status_only,
            is_draft_transition=draft_transition,
        )

        event.processing_result = WebhookProcessingResult.ACCEPTED

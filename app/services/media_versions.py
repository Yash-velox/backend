"""Complete product media version history (immutable snapshots)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    BatchProduct,
    MediaVersionType,
    Product,
    ProductMediaVersion,
    ProductPublishOperation,
    ProductRollbackOperation,
    PublishStatus,
    RollbackStatus,
    Shop,
)
from app.services.publish_snapshot import snapshot_from_baseline, snapshot_hash

logger = logging.getLogger("app.services.media_versions")

ACTIVE_PUBLISH_STATUSES = {PublishStatus.QUEUED, PublishStatus.PUBLISHING}
ACTIVE_ROLLBACK_STATUSES = {RollbackStatus.QUEUED, RollbackStatus.ROLLING_BACK}


class MediaVersionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def product_has_active_media_op(db: Session, *, shop_id: UUID, shopify_product_gid: str) -> str | None:
    """Return a lock reason if publish or rollback is active for this product."""
    pub = (
        db.query(ProductPublishOperation)
        .filter(
            ProductPublishOperation.shop_id == shop_id,
            ProductPublishOperation.shopify_product_gid == shopify_product_gid,
            ProductPublishOperation.status.in_(list(ACTIVE_PUBLISH_STATUSES)),
        )
        .first()
    )
    if pub:
        return "PUBLISH_ALREADY_ACTIVE"
    rb = (
        db.query(ProductRollbackOperation)
        .filter(
            ProductRollbackOperation.shop_id == shop_id,
            ProductRollbackOperation.shopify_product_gid == shopify_product_gid,
            ProductRollbackOperation.status.in_(list(ACTIVE_ROLLBACK_STATUSES)),
        )
        .first()
    )
    if rb:
        return "ROLLBACK_ALREADY_ACTIVE"
    return None


def _normalize_items(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Ensure items_json is a full publish-style snapshot dict."""
    if "media" in snapshot:
        return {
            "product_gid": snapshot.get("product_gid"),
            "updated_at": snapshot.get("updated_at"),
            "featured_media_gid": snapshot.get("featured_media_gid"),
            "media": list(snapshot.get("media") or []),
            "variants": list(snapshot.get("variants") or []),
        }
    return snapshot_from_baseline(snapshot)


class MediaVersionsService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def get_product(self, product_id: UUID) -> Product:
        product = (
            self.db.query(Product)
            .filter(Product.id == product_id, Product.shop_id == self.shop.id)
            .one_or_none()
        )
        if not product:
            raise MediaVersionError("VERSION_NOT_FOUND", "Product not found")
        return product

    def list_versions(self, product_id: UUID) -> list[ProductMediaVersion]:
        self.get_product(product_id)
        return (
            self.db.query(ProductMediaVersion)
            .filter(
                ProductMediaVersion.shop_id == self.shop.id,
                ProductMediaVersion.product_id == product_id,
            )
            .order_by(ProductMediaVersion.version_number.desc())
            .all()
        )

    def get_version(self, product_id: UUID, version_id: UUID) -> ProductMediaVersion:
        version = (
            self.db.query(ProductMediaVersion)
            .filter(
                ProductMediaVersion.id == version_id,
                ProductMediaVersion.product_id == product_id,
                ProductMediaVersion.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if not version:
            raise MediaVersionError("VERSION_NOT_FOUND", "Version not found")
        return version

    def active_version(self, product_id: UUID) -> ProductMediaVersion | None:
        return (
            self.db.query(ProductMediaVersion)
            .filter(
                ProductMediaVersion.shop_id == self.shop.id,
                ProductMediaVersion.product_id == product_id,
                ProductMediaVersion.is_active.is_(True),
            )
            .one_or_none()
        )

    def next_version_number(self, product_id: UUID) -> int:
        current = (
            self.db.query(func.max(ProductMediaVersion.version_number))
            .filter(
                ProductMediaVersion.shop_id == self.shop.id,
                ProductMediaVersion.product_id == product_id,
            )
            .scalar()
        )
        return int(current or 0) + 1

    def create_version(
        self,
        *,
        product: Product,
        snapshot: dict[str, Any],
        version_type: MediaVersionType,
        activate: bool,
        source_version_id: UUID | None = None,
        processing_batch_id: UUID | None = None,
        publish_operation_id: UUID | None = None,
        rollback_operation_id: UUID | None = None,
        prompt_snapshot_json: Any = None,
        model_name: str | None = None,
        quality: str | None = None,
        created_by: str | None = None,
        skip_duplicate_hash: bool = True,
    ) -> ProductMediaVersion:
        items = _normalize_items(snapshot)
        items["product_gid"] = items.get("product_gid") or product.shopify_product_gid
        digest = snapshot_hash(items)

        if skip_duplicate_hash:
            existing = (
                self.db.query(ProductMediaVersion)
                .filter(
                    ProductMediaVersion.shop_id == self.shop.id,
                    ProductMediaVersion.product_id == product.id,
                    ProductMediaVersion.snapshot_hash == digest,
                    ProductMediaVersion.version_type == version_type,
                )
                .order_by(ProductMediaVersion.version_number.desc())
                .first()
            )
            if existing and not activate:
                return existing
            if existing and existing.is_active and activate:
                return existing

        if activate:
            self._deactivate_all(product.id)

        now = datetime.now(timezone.utc)
        version = ProductMediaVersion(
            shop_id=self.shop.id,
            product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            version_number=self.next_version_number(product.id),
            version_type=version_type,
            source_version_id=source_version_id,
            processing_batch_id=processing_batch_id,
            publish_operation_id=publish_operation_id,
            rollback_operation_id=rollback_operation_id,
            is_active=activate,
            rollback_eligible=True,
            unavailable_reason=None,
            snapshot_hash=digest,
            items_json=items,
            prompt_snapshot_json=prompt_snapshot_json,
            model_name=model_name,
            quality=quality,
            created_by=created_by,
            activated_at=now if activate else None,
        )
        self.db.add(version)
        self.db.flush()
        logger.info(
            "Created media version | product=%s number=%s type=%s active=%s",
            product.id,
            version.version_number,
            version_type.value,
            activate,
        )
        return version

    def _deactivate_all(self, product_id: UUID) -> None:
        versions = (
            self.db.query(ProductMediaVersion)
            .filter(
                ProductMediaVersion.shop_id == self.shop.id,
                ProductMediaVersion.product_id == product_id,
                ProductMediaVersion.is_active.is_(True),
            )
            .all()
        )
        for v in versions:
            v.is_active = False

    def record_publish_success(
        self,
        *,
        batch_product: BatchProduct,
        publish_op: ProductPublishOperation,
        pre_publish_snapshot: dict[str, Any] | None,
        final_snapshot: dict[str, Any],
    ) -> ProductMediaVersion | None:
        """Create ORIGINAL (if needed) + PUBLISHED versions after verified publish."""
        product_id = batch_product.product_id
        if not product_id:
            product = (
                self.db.query(Product)
                .filter(
                    Product.shop_id == self.shop.id,
                    Product.shopify_product_gid == batch_product.shopify_product_gid,
                )
                .one_or_none()
            )
            if not product:
                logger.warning(
                    "Skip version create — catalog product missing | gid=%s",
                    batch_product.shopify_product_gid,
                )
                return None
            product_id = product.id
            batch_product.product_id = product.id
        else:
            product = self.get_product(product_id)

        existing_count = (
            self.db.query(func.count(ProductMediaVersion.id))
            .filter(
                ProductMediaVersion.shop_id == self.shop.id,
                ProductMediaVersion.product_id == product.id,
            )
            .scalar()
            or 0
        )

        if existing_count == 0 and pre_publish_snapshot:
            self.create_version(
                product=product,
                snapshot=pre_publish_snapshot,
                version_type=MediaVersionType.ORIGINAL,
                activate=False,
                processing_batch_id=batch_product.batch_id,
                publish_operation_id=publish_op.id,
                prompt_snapshot_json=getattr(batch_product, "prompt_snapshot_json", None),
                created_by="publish",
                skip_duplicate_hash=False,
            )

        published = self.create_version(
            product=product,
            snapshot=final_snapshot,
            version_type=MediaVersionType.PUBLISHED,
            activate=True,
            processing_batch_id=batch_product.batch_id,
            publish_operation_id=publish_op.id,
            prompt_snapshot_json=getattr(batch_product, "prompt_snapshot_json", None),
            created_by="publish",
            skip_duplicate_hash=False,
        )
        return published

    def search_products_with_versions(self, search: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        q = (
            self.db.query(Product, ProductMediaVersion)
            .join(
                ProductMediaVersion,
                (ProductMediaVersion.product_id == Product.id)
                & (ProductMediaVersion.shop_id == Product.shop_id)
                & (ProductMediaVersion.is_active.is_(True)),
            )
            .filter(Product.shop_id == self.shop.id)
        )
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(
                (Product.title.ilike(term))
                | (Product.handle.ilike(term))
                | (Product.shopify_product_gid.ilike(term))
            )
        rows = q.order_by(Product.title.asc()).limit(limit).all()
        out: list[dict[str, Any]] = []
        for product, active in rows:
            media = (active.items_json or {}).get("media") or []
            out.append(
                {
                    "productId": str(product.id),
                    "shopifyProductGid": product.shopify_product_gid,
                    "title": product.title,
                    "handle": product.handle,
                    "productType": product.product_type,
                    "activeVersionId": str(active.id),
                    "activeVersionNumber": active.version_number,
                    "activeVersionType": active.version_type.value,
                    "imageCount": len(media),
                }
            )
        return out

    def mark_unavailable(self, version: ProductMediaVersion, reason: str) -> None:
        version.rollback_eligible = False
        version.unavailable_reason = reason[:2000]
        self.db.flush()

    def mark_eligible(self, version: ProductMediaVersion) -> None:
        version.rollback_eligible = True
        version.unavailable_reason = None
        self.db.flush()

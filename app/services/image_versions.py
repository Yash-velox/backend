"""Normalized per-image Shopify CDN version records (under product-level snapshots)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ImageVersion,
    ImageVersionEvent,
    ImageVersionEventType,
    ImageVersionType,
    Product,
    ProductMedia,
    Shop,
)

logger = logging.getLogger("app.services.image_versions")


class ImageVersionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _short_gid(gid: str | None, fallback: str = "x") -> str:
    if not gid:
        return fallback
    tail = gid.rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", tail)[:16]
    return cleaned or fallback


def build_generated_filename(*, product_id: UUID, source_media_gid: str, version_number: int) -> str:
    return (
        f"product_{_short_gid(str(product_id))}"
        f"_media_{_short_gid(source_media_gid)}"
        f"_version_{version_number}.png"
    )


class ImageVersionsService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def _record_event(
        self,
        *,
        product_id: UUID,
        event_type: ImageVersionEventType,
        image_version_id: UUID | None = None,
        previous_version_id: UUID | None = None,
        new_version_id: UUID | None = None,
        batch_id: UUID | None = None,
        product_media_version_id: UUID | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        details_json: dict[str, Any] | None = None,
    ) -> ImageVersionEvent:
        event = ImageVersionEvent(
            shop_id=self.shop.id,
            product_id=product_id,
            image_version_id=image_version_id,
            event_type=event_type,
            previous_version_id=previous_version_id,
            new_version_id=new_version_id,
            batch_id=batch_id,
            product_media_version_id=product_media_version_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details_json=details_json,
        )
        self.db.add(event)
        return event

    def get_original(
        self,
        *,
        product_id: UUID,
        source_media_gid: str,
    ) -> ImageVersion | None:
        return (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == product_id,
                ImageVersion.source_media_gid == source_media_gid,
                ImageVersion.version_number == 0,
                ImageVersion.is_original.is_(True),
            )
            .one_or_none()
        )

    def current_for_source(
        self,
        *,
        product_id: UUID,
        source_media_gid: str,
    ) -> ImageVersion | None:
        return (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == product_id,
                ImageVersion.source_media_gid == source_media_gid,
                ImageVersion.is_current.is_(True),
            )
            .one_or_none()
        )

    def next_version_number(self, *, product_id: UUID, source_media_gid: str) -> int:
        current = (
            self.db.query(func.max(ImageVersion.version_number))
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == product_id,
                ImageVersion.source_media_gid == source_media_gid,
            )
            .scalar()
        )
        return int(current or 0) + 1

    def ensure_original_from_media(self, media: ProductMedia, *, actor_type: str = "catalog_sync") -> ImageVersion:
        """Idempotently register Version 0 for a catalog ProductMedia row."""
        existing = self.get_original(product_id=media.product_id, source_media_gid=media.shopify_media_gid)
        if existing:
            # Refresh mutable catalog metadata without creating duplicates.
            existing.shopify_file_gid = media.shopify_file_gid or existing.shopify_file_gid
            existing.shopify_media_gid = media.shopify_media_gid
            existing.shopify_cdn_url = media.cdn_url or existing.shopify_cdn_url
            existing.original_filename = media.original_filename or existing.original_filename
            existing.mime_type = media.mime_type or existing.mime_type
            existing.width = media.width if media.width is not None else existing.width
            existing.height = media.height if media.height is not None else existing.height
            meta = dict(existing.metadata_json or {})
            meta.update(
                {
                    "position": media.position,
                    "alt_text": media.alt_text,
                    "is_primary": media.is_primary,
                    "is_visible": media.is_visible,
                }
            )
            existing.metadata_json = meta
            return existing

        has_generated_current = (
            self.db.query(ImageVersion.id)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == media.product_id,
                ImageVersion.source_media_gid == media.shopify_media_gid,
                ImageVersion.is_current.is_(True),
                ImageVersion.is_original.is_(False),
            )
            .first()
            is not None
        )

        version = ImageVersion(
            shop_id=self.shop.id,
            product_id=media.product_id,
            source_media_gid=media.shopify_media_gid,
            version_number=0,
            version_type=ImageVersionType.ORIGINAL,
            shopify_file_gid=media.shopify_file_gid,
            shopify_media_gid=media.shopify_media_gid,
            shopify_cdn_url=media.cdn_url,
            original_filename=media.original_filename,
            stored_filename=media.original_filename,
            mime_type=media.mime_type,
            width=media.width,
            height=media.height,
            is_current=not has_generated_current,
            is_published=bool(media.is_active and media.is_visible),
            is_original=True,
            is_protected=True,
            metadata_json={
                "position": media.position,
                "alt_text": media.alt_text,
                "is_primary": media.is_primary,
                "is_visible": media.is_visible,
            },
        )
        self.db.add(version)
        self.db.flush()
        self._record_event(
            product_id=media.product_id,
            event_type=ImageVersionEventType.ORIGINAL_REGISTERED,
            image_version_id=version.id,
            new_version_id=version.id,
            actor_type=actor_type,
            details_json={"source_media_gid": media.shopify_media_gid},
        )
        return version

    def ensure_originals_for_product(self, product_id: UUID, *, actor_type: str = "catalog_sync") -> int:
        media_rows = (
            self.db.query(ProductMedia)
            .filter(
                ProductMedia.shop_id == self.shop.id,
                ProductMedia.product_id == product_id,
                ProductMedia.is_active.is_(True),
            )
            .all()
        )
        created = 0
        for media in media_rows:
            before = self.get_original(product_id=product_id, source_media_gid=media.shopify_media_gid)
            self.ensure_original_from_media(media, actor_type=actor_type)
            if before is None:
                created += 1
        return created

    def find_by_idempotency_key(self, key: str) -> ImageVersion | None:
        return (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.upload_idempotency_key == key,
            )
            .one_or_none()
        )

    def find_by_file_gid(self, file_gid: str) -> ImageVersion | None:
        return (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.shopify_file_gid == file_gid,
            )
            .one_or_none()
        )

    def create_generated_after_upload(
        self,
        *,
        product_id: UUID,
        source_media_gid: str,
        shopify_file_gid: str,
        shopify_cdn_url: str | None,
        width: int | None,
        height: int | None,
        file_size_bytes: int | None,
        checksum: str | None,
        mime_type: str = "image/png",
        original_filename: str | None = None,
        stored_filename: str | None = None,
        upload_idempotency_key: str,
        batch_id: UUID | None = None,
        batch_image_id: UUID | None = None,
        attempt_id: UUID | None = None,
        metadata_json: dict[str, Any] | None = None,
        actor_type: str = "processing",
    ) -> ImageVersion:
        existing = self.find_by_idempotency_key(upload_idempotency_key)
        if existing:
            return existing
        by_file = self.find_by_file_gid(shopify_file_gid)
        if by_file:
            return by_file

        # Ensure ORIGINAL exists when catalog product is known.
        media = (
            self.db.query(ProductMedia)
            .filter(
                ProductMedia.shop_id == self.shop.id,
                ProductMedia.product_id == product_id,
                ProductMedia.shopify_media_gid == source_media_gid,
            )
            .one_or_none()
        )
        if media:
            self.ensure_original_from_media(media, actor_type="processing")

        previous = self.current_for_source(product_id=product_id, source_media_gid=source_media_gid)
        version_number = self.next_version_number(product_id=product_id, source_media_gid=source_media_gid)
        now = datetime.now(timezone.utc)

        if previous:
            previous.is_current = False
            previous.superseded_at = now
            self._record_event(
                product_id=product_id,
                event_type=ImageVersionEventType.VERSION_SUPERSEDED,
                image_version_id=previous.id,
                previous_version_id=previous.id,
                batch_id=batch_id,
                actor_type=actor_type,
            )

        filename = stored_filename or build_generated_filename(
            product_id=product_id,
            source_media_gid=source_media_gid,
            version_number=version_number,
        )
        version = ImageVersion(
            shop_id=self.shop.id,
            product_id=product_id,
            source_media_gid=source_media_gid,
            parent_version_id=previous.id if previous else None,
            version_number=version_number,
            version_type=ImageVersionType.GENERATED,
            shopify_file_gid=shopify_file_gid,
            shopify_cdn_url=shopify_cdn_url,
            original_filename=original_filename,
            stored_filename=filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            width=width,
            height=height,
            checksum=checksum,
            is_current=True,
            is_published=False,
            is_original=False,
            is_protected=False,
            created_by_batch_id=batch_id,
            created_by_batch_image_id=batch_image_id,
            created_by_attempt_id=attempt_id,
            upload_idempotency_key=upload_idempotency_key,
            metadata_json=metadata_json,
        )
        self.db.add(version)
        self.db.flush()
        self._record_event(
            product_id=product_id,
            event_type=ImageVersionEventType.VERSION_GENERATED,
            image_version_id=version.id,
            previous_version_id=previous.id if previous else None,
            new_version_id=version.id,
            batch_id=batch_id,
            actor_type=actor_type,
        )
        self._record_event(
            product_id=product_id,
            event_type=ImageVersionEventType.VERSION_UPLOADED,
            image_version_id=version.id,
            new_version_id=version.id,
            batch_id=batch_id,
            actor_type=actor_type,
            details_json={"shopify_file_gid": shopify_file_gid},
        )
        return version

    def record_upload_failed(
        self,
        *,
        product_id: UUID,
        source_media_gid: str,
        batch_id: UUID | None = None,
        batch_image_id: UUID | None = None,
        error_code: str,
        error_message: str,
    ) -> None:
        self._record_event(
            product_id=product_id,
            event_type=ImageVersionEventType.UPLOAD_FAILED,
            batch_id=batch_id,
            actor_type="processing",
            details_json={
                "source_media_gid": source_media_gid,
                "batch_image_id": str(batch_image_id) if batch_image_id else None,
                "error_code": error_code,
                "error_message": error_message,
            },
        )

    def mark_published_for_product_version(
        self,
        *,
        product_id: UUID,
        product_media_version_id: UUID,
        media_items: list[dict[str, Any]],
        actor_type: str = "publish",
    ) -> None:
        """Link image versions included in a product-level snapshot and update publish flags."""
        now = datetime.now(timezone.utc)
        file_gids = [
            m.get("file_gid")
            for m in media_items
            if isinstance(m, dict) and m.get("file_gid")
        ]
        # Clear published flag for previously published versions of this product.
        prior = (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == product_id,
                ImageVersion.is_published.is_(True),
            )
            .all()
        )
        for row in prior:
            row.is_published = False

        for item in media_items:
            if not isinstance(item, dict):
                continue
            file_gid = item.get("file_gid")
            media_gid = item.get("media_gid")
            version: ImageVersion | None = None
            if file_gid:
                version = self.find_by_file_gid(str(file_gid))
            if version is None and media_gid:
                # Prefer original matching media gid when file mapping missing.
                version = self.get_original(product_id=product_id, source_media_gid=str(media_gid))
            if version is None:
                continue
            version.is_published = True
            version.published_at = now
            version.product_media_version_id = product_media_version_id
            if media_gid and not version.shopify_media_gid:
                version.shopify_media_gid = str(media_gid)
            self._record_event(
                product_id=product_id,
                event_type=ImageVersionEventType.VERSION_PUBLISHED,
                image_version_id=version.id,
                product_media_version_id=product_media_version_id,
                actor_type=actor_type,
                details_json={"file_gid": file_gid, "media_gid": media_gid},
            )
            self._record_event(
                product_id=product_id,
                event_type=ImageVersionEventType.VERSION_INCLUDED_IN_PRODUCT_SNAPSHOT,
                image_version_id=version.id,
                product_media_version_id=product_media_version_id,
                actor_type=actor_type,
            )

        logger.info(
            "Linked image versions to product snapshot | product=%s version=%s files=%s",
            product_id,
            product_media_version_id,
            len(file_gids),
        )

    def list_for_product(
        self,
        product_id: UUID,
        *,
        source_media_gid: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ImageVersion], int]:
        q = self.db.query(ImageVersion).filter(
            ImageVersion.shop_id == self.shop.id,
            ImageVersion.product_id == product_id,
        )
        if source_media_gid:
            q = q.filter(ImageVersion.source_media_gid == source_media_gid)
        total = q.count()
        rows = (
            q.order_by(ImageVersion.source_media_gid.asc(), ImageVersion.version_number.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total

    def get_version(self, product_id: UUID, version_id: UUID) -> ImageVersion:
        version = (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_id == product_id,
                ImageVersion.id == version_id,
            )
            .one_or_none()
        )
        if not version:
            raise ImageVersionError("VERSION_NOT_FOUND", "Image version not found")
        return version

    def list_events(
        self,
        product_id: UUID,
        version_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ImageVersionEvent], int]:
        self.get_version(product_id, version_id)
        q = self.db.query(ImageVersionEvent).filter(
            ImageVersionEvent.shop_id == self.shop.id,
            ImageVersionEvent.product_id == product_id,
            ImageVersionEvent.image_version_id == version_id,
        )
        total = q.count()
        rows = q.order_by(ImageVersionEvent.created_at.desc()).offset(offset).limit(limit).all()
        return rows, total

    def storage_summary(self) -> dict[str, Any]:
        total_versions = (
            self.db.query(func.count(ImageVersion.id))
            .filter(ImageVersion.shop_id == self.shop.id)
            .scalar()
            or 0
        )
        total_bytes = (
            self.db.query(func.coalesce(func.sum(ImageVersion.file_size_bytes), 0))
            .filter(ImageVersion.shop_id == self.shop.id)
            .scalar()
            or 0
        )
        original_bytes = (
            self.db.query(func.coalesce(func.sum(ImageVersion.file_size_bytes), 0))
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.is_original.is_(True),
            )
            .scalar()
            or 0
        )
        generated_count = (
            self.db.query(func.count(ImageVersion.id))
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.version_type == ImageVersionType.GENERATED,
            )
            .scalar()
            or 0
        )
        generated_bytes = (
            self.db.query(func.coalesce(func.sum(ImageVersion.file_size_bytes), 0))
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.version_type == ImageVersionType.GENERATED,
            )
            .scalar()
            or 0
        )
        avg_generated = (float(generated_bytes) / generated_count) if generated_count else 0.0

        per_product = (
            self.db.query(
                ImageVersion.product_id,
                func.count(ImageVersion.id).label("cnt"),
            )
            .filter(ImageVersion.shop_id == self.shop.id)
            .group_by(ImageVersion.product_id)
            .all()
        )
        max_versions_product = max((int(r.cnt) for r in per_product), default=0)
        products_with_versions = len(per_product)

        warnings: list[dict[str, Any]] = []
        if total_versions >= settings.image_storage_warn_total_versions:
            warnings.append(
                {
                    "code": "HIGH_TOTAL_VERSIONS",
                    "message": "Estimated total image versions is high. No automatic deletion is performed.",
                    "value": total_versions,
                }
            )
        avg_mb = avg_generated / (1024 * 1024)
        if generated_count and avg_mb >= settings.image_storage_warn_avg_generated_mb:
            warnings.append(
                {
                    "code": "HIGH_AVG_GENERATED_SIZE",
                    "message": "Average generated image size is high (estimate from stored metadata).",
                    "valueMb": round(avg_mb, 2),
                }
            )
        if max_versions_product >= settings.image_storage_warn_versions_per_product:
            warnings.append(
                {
                    "code": "HIGH_VERSIONS_PER_PRODUCT",
                    "message": "One product has an unusually high number of image versions.",
                    "value": max_versions_product,
                }
            )

        return {
            "estimateOnly": True,
            "note": "Totals are estimates from stored file_size_bytes metadata; not Shopify account usage.",
            "totalVersions": int(total_versions),
            "totalRecordedFileSizeBytes": int(total_bytes),
            "originalFileStorageBytes": int(original_bytes),
            "generatedVersionStorageBytes": int(generated_bytes),
            "generatedVersionCount": int(generated_count),
            "averageGeneratedFileSizeBytes": avg_generated,
            "productsWithVersions": products_with_versions,
            "maxVersionsForOneProduct": max_versions_product,
            "warnings": warnings,
        }

    def versions_for_product_media_version(self, product_media_version_id: UUID) -> list[ImageVersion]:
        return (
            self.db.query(ImageVersion)
            .filter(
                ImageVersion.shop_id == self.shop.id,
                ImageVersion.product_media_version_id == product_media_version_id,
            )
            .order_by(ImageVersion.source_media_gid.asc(), ImageVersion.version_number.asc())
            .all()
        )


def backfill_originals_for_shop(db: Session, shop: Shop) -> dict[str, int]:
    """Repeatable backfill of ORIGINAL image versions from product_media."""
    service = ImageVersionsService(db, shop)
    products = db.query(Product).filter(Product.shop_id == shop.id).all()
    created = 0
    scanned = 0
    for product in products:
        media_rows = (
            db.query(ProductMedia)
            .filter(
                ProductMedia.shop_id == shop.id,
                ProductMedia.product_id == product.id,
                ProductMedia.is_active.is_(True),
            )
            .all()
        )
        for media in media_rows:
            scanned += 1
            if not media.shopify_media_gid:
                logger.warning(
                    "Skip original backfill — missing media gid | product=%s media=%s",
                    product.id,
                    media.id,
                )
                continue
            before = service.get_original(product_id=product.id, source_media_gid=media.shopify_media_gid)
            service.ensure_original_from_media(media, actor_type="backfill")
            if before is None:
                created += 1
    db.commit()
    return {"products": len(products), "mediaScanned": scanned, "originalsCreated": created}

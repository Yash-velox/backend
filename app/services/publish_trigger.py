"""Enqueue and trigger publish operations after batch processing completes."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    ProcessingBatch,
    ProductPublishOperation,
    PublishStatus,
    PublishTriggerSource,
    Shop,
    ShopSettings,
)
from app.services.output_storage import checksum_sha256, get_output_storage
from app.services.shopify_file_upload import (
    PublishUploadError,
    sanitize_png_filename,
    validate_png_file,
)
logger = logging.getLogger("app.services.publish_trigger")

TERMINAL_BATCH_STATUSES = {
    BatchStatus.COMPLETED,
    BatchStatus.PARTIALLY_COMPLETED,
    BatchStatus.FAILED,
    BatchStatus.CANCELLED,
}

ACTIVE_PUBLISH_STATUSES = {PublishStatus.QUEUED, PublishStatus.PUBLISHING}
RETRYABLE_PUBLISH_STATUSES = {
    PublishStatus.PUBLISH_FAILED,
    PublishStatus.PUBLISH_CONFLICT,
    PublishStatus.RESTORE_FAILED,
}


class PublishEnqueueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_batch_processing_terminal(batch: ProcessingBatch) -> bool:
    if batch.status in TERMINAL_BATCH_STATUSES:
        return True
    # Also treat as terminal when no active processing products remain.
    active = (
        (batch.pending_product_count or 0)
        + (batch.processing_product_count or 0)
        + (batch.retrying_product_count or 0)
    )
    return active == 0 and (batch.product_count or 0) > 0


def build_output_set_checksum(images: list[BatchImage], storage=None) -> str:
    storage = storage or get_output_storage()
    hashes: list[str] = []
    for image in sorted(images, key=lambda i: (i.id.hex if i.id else "")):
        if image.output_checksum:
            hashes.append(image.output_checksum)
            continue
        if image.generated_shopify_file_gid:
            hashes.append(f"shopify-file:{image.generated_shopify_file_gid}")
            continue
        key = image.output_storage_key
        if not key:
            hashes.append("MISSING")
            continue
        path = storage.resolve_path(key)
        data = path.read_bytes()
        hashes.append(checksum_sha256(data))
    raw = "|".join(hashes)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_idempotency_key(
    *,
    shop_id: UUID,
    batch_product_id: UUID,
    shopify_product_gid: str,
    output_set_checksum: str,
) -> str:
    raw = f"{shop_id}:{batch_product_id}:{shopify_product_gid}:{output_set_checksum}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ordered_completed_images(db: Session, batch_product: BatchProduct) -> list[BatchImage]:
    images = (
        db.query(BatchImage)
        .filter(BatchImage.batch_product_id == batch_product.id)
        .order_by(BatchImage.created_at.asc())
        .all()
    )
    # Prefer baseline media order when available.
    baseline = batch_product.baseline_snapshot_json or {}
    media = baseline.get("media") if isinstance(baseline, dict) else None
    if isinstance(media, list) and media:
        order_map = {
            (m.get("media_gid") or m.get("shopify_media_gid")): idx
            for idx, m in enumerate(media)
            if isinstance(m, dict)
        }
        images = sorted(
            images,
            key=lambda img: (order_map.get(img.shopify_media_gid, 10_000), str(img.id)),
        )
    return images


def validate_publishable_outputs(db: Session, batch_product: BatchProduct) -> tuple[list[BatchImage], str, list[dict[str, Any]]]:
    if batch_product.status != BatchProductStatus.COMPLETED:
        raise PublishEnqueueError("PUBLISH_PRODUCT_NOT_PROCESSED", "Product processing did not complete successfully")

    images = _ordered_completed_images(db, batch_product)
    completed = [i for i in images if i.status == BatchImageStatus.COMPLETED]
    if not completed:
        raise PublishEnqueueError("PUBLISH_OUTPUT_MISSING", "No completed processed images")
    if len(completed) != len(images):
        raise PublishEnqueueError("PUBLISH_OUTPUT_INCOMPLETE", "Not all batch images completed processing")

    storage = get_output_storage()
    assets: list[dict[str, Any]] = []
    hashes: list[str] = []
    baseline = batch_product.baseline_snapshot_json or {}
    media_meta: dict[str, dict] = {}
    if isinstance(baseline, dict):
        for m in baseline.get("media") or []:
            if isinstance(m, dict) and (m.get("media_gid") or m.get("shopify_media_gid")):
                gid = m.get("media_gid") or m.get("shopify_media_gid")
                media_meta[gid] = m

    for position, image in enumerate(completed):
        meta = media_meta.get(image.shopify_media_gid) or {}
        alt = meta.get("alt_text") or meta.get("alt")
        filename = sanitize_png_filename(image.original_filename or meta.get("filename"))
        file_gid = image.generated_shopify_file_gid
        cdn_url = image.generated_shopify_cdn_url
        digest = image.output_checksum
        size = None
        local_key = image.output_storage_key

        if file_gid:
            # Preferred path: durable Shopify Files / CDN already uploaded during processing.
            if not digest:
                digest = f"shopify-file:{file_gid}"
            size = 0
            upload_status = "READY"
        elif local_key and storage.exists(local_key):
            # Legacy fallback only — new COMPLETED images must have Shopify Files.
            path = storage.resolve_path(local_key)
            try:
                size, data = validate_png_file(path)
            except PublishUploadError as exc:
                raise PublishEnqueueError(exc.code, str(exc)) from exc
            digest = checksum_sha256(data)
            upload_status = "PENDING"
        else:
            raise PublishEnqueueError(
                "PUBLISH_OUTPUT_MISSING",
                f"Missing Shopify CDN file for media {image.shopify_media_gid}",
            )

        hashes.append(digest or "")
        assets.append(
            {
                "batch_image_id": str(image.id),
                "image_version_id": str(image.generated_image_version_id)
                if image.generated_image_version_id
                else None,
                "source_media_gid": image.shopify_media_gid,
                "source_file_gid": image.shopify_file_gid,
                "source_position": meta.get("position", position),
                "source_alt_text": alt,
                "source_filename": image.original_filename,
                "processed_output_key": local_key,
                "processed_filename": filename,
                "processed_sha256": digest,
                "processed_size_bytes": size,
                "shopify_file_gid": file_gid,
                "shopify_media_gid": None,
                "shopify_file_status": "READY" if file_gid else None,
                "shopify_cdn_url": cdn_url,
                "upload_status": upload_status,
                "association_status": "PENDING",
                "target_position": position,
                "target_alt_text": alt,
            }
        )

    checksum = hashlib.sha256("|".join(hashes).encode("utf-8")).hexdigest()
    return completed, checksum, assets


class PublishTriggerService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def on_batch_terminal(self, batch: ProcessingBatch, *, commit: bool = True) -> dict[str, int]:
        """Mark successful products READY_TO_PUBLISH; auto-enqueue when setting enabled."""
        if batch.shop_id != self.shop.id:
            raise PublishEnqueueError("PUBLISH_LOCKED", "Batch does not belong to shop")
        if not is_batch_processing_terminal(batch):
            return {"ready": 0, "queued": 0, "skipped": 0}

        products = (
            self.db.query(BatchProduct)
            .filter(BatchProduct.batch_id == batch.id, BatchProduct.shop_id == self.shop.id)
            .all()
        )
        ready = 0
        for product in products:
            if product.status != BatchProductStatus.COMPLETED:
                continue
            if product.publish_status in {
                PublishStatus.PUBLISHED,
                PublishStatus.QUEUED,
                PublishStatus.PUBLISHING,
            }:
                continue
            if product.publish_status is None or product.publish_status == PublishStatus.READY_TO_PUBLISH:
                try:
                    validate_publishable_outputs(self.db, product)
                except PublishEnqueueError:
                    continue
                product.publish_status = PublishStatus.READY_TO_PUBLISH
                ready += 1

        self.db.flush()

        settings_row = (
            self.db.query(ShopSettings).filter(ShopSettings.shop_id == self.shop.id).one_or_none()
        )
        queued = 0
        if settings_row and settings_row.auto_publish_processed_images:
            result = self.enqueue_ready_for_batch(
                batch.id,
                trigger=PublishTriggerSource.AUTO,
                commit=False,
            )
            queued = int(result.get("queued") or 0)

        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return {"ready": ready, "queued": queued, "skipped": 0}

    def enqueue_ready_for_batch(
        self,
        batch_id: UUID,
        *,
        trigger: PublishTriggerSource = PublishTriggerSource.MANUAL,
        commit: bool = True,
    ) -> dict[str, int]:
        batch = (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == batch_id, ProcessingBatch.shop_id == self.shop.id)
            .one_or_none()
        )
        if not batch:
            raise PublishEnqueueError("PUBLISH_BATCH_NOT_TERMINAL", "Batch not found")
        if not is_batch_processing_terminal(batch):
            raise PublishEnqueueError("PUBLISH_BATCH_NOT_TERMINAL", "Batch processing is not finished")

        products = (
            self.db.query(BatchProduct)
            .filter(
                BatchProduct.batch_id == batch.id,
                BatchProduct.shop_id == self.shop.id,
                BatchProduct.status == BatchProductStatus.COMPLETED,
            )
            .all()
        )
        counts = {
            "ready": 0,
            "queued": 0,
            "alreadyPublished": 0,
            "alreadyQueued": 0,
            "failedValidation": 0,
        }
        for product in products:
            if product.publish_status == PublishStatus.READY_TO_PUBLISH or product.publish_status is None:
                counts["ready"] += 1
            try:
                result = self.enqueue_product(product.id, trigger=trigger, commit=False)
                status = result.get("status")
                if status == PublishStatus.PUBLISHED.value:
                    counts["alreadyPublished"] += 1
                elif status == PublishStatus.PUBLISHING.value:
                    counts["alreadyQueued"] += 1
                elif status == PublishStatus.QUEUED.value:
                    if "already" in (result.get("message") or "").lower():
                        counts["alreadyQueued"] += 1
                    else:
                        counts["queued"] += 1
            except PublishEnqueueError:
                counts["failedValidation"] += 1
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return counts

    def enqueue_product(
        self,
        batch_product_id: UUID,
        *,
        trigger: PublishTriggerSource = PublishTriggerSource.MANUAL,
        force_retry: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        product = (
            self.db.query(BatchProduct)
            .filter(BatchProduct.id == batch_product_id, BatchProduct.shop_id == self.shop.id)
            .one_or_none()
        )
        if not product:
            raise PublishEnqueueError("PUBLISH_PRODUCT_NOT_PROCESSED", "Batch product not found")

        batch = (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == product.batch_id, ProcessingBatch.shop_id == self.shop.id)
            .one_or_none()
        )
        if not batch or not is_batch_processing_terminal(batch):
            raise PublishEnqueueError("PUBLISH_BATCH_NOT_TERMINAL", "Batch processing is not finished")

        _, checksum, assets = validate_publishable_outputs(self.db, product)
        key = build_idempotency_key(
            shop_id=self.shop.id,
            batch_product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            output_set_checksum=checksum,
        )

        existing = (
            self.db.query(ProductPublishOperation)
            .filter(
                ProductPublishOperation.shop_id == self.shop.id,
                ProductPublishOperation.idempotency_key == key,
            )
            .one_or_none()
        )
        if existing:
            if existing.status == PublishStatus.PUBLISHED:
                product.publish_status = PublishStatus.PUBLISHED
                self.db.flush()
                if commit:
                    self.db.commit()
                return {
                    "operationId": str(existing.id),
                    "status": PublishStatus.PUBLISHED.value,
                    "message": "Product already published for this output set.",
                }
            if existing.status in ACTIVE_PUBLISH_STATUSES:
                return {
                    "operationId": str(existing.id),
                    "status": existing.status.value,
                    "message": "Product publishing is already queued or in progress.",
                }
            if force_retry or existing.status in RETRYABLE_PUBLISH_STATUSES:
                prior_assets = existing.assets_json or []
                gid_by_image = {
                    a.get("batch_image_id"): a
                    for a in prior_assets
                    if isinstance(a, dict) and a.get("batch_image_id")
                }
                for asset in assets:
                    prior = gid_by_image.get(asset["batch_image_id"]) or {}
                    if prior.get("shopify_file_gid") and prior.get("shopify_file_status") in {
                        "READY",
                        "PROCESSING",
                        "UPLOADED",
                        "REGISTERED",
                    }:
                        asset["shopify_file_gid"] = prior.get("shopify_file_gid")
                        asset["shopify_file_status"] = prior.get("shopify_file_status")
                        asset["shopify_cdn_url"] = prior.get("shopify_cdn_url")
                        asset["upload_status"] = prior.get("upload_status") or "READY"
                existing.status = PublishStatus.QUEUED
                existing.current_stage = "QUEUED"
                existing.trigger_source = PublishTriggerSource.RETRY if force_retry else trigger
                existing.attempt_number = int(existing.attempt_number or 1) + 1
                existing.assets_json = assets
                existing.output_set_checksum = checksum
                existing.last_error_code = None
                existing.last_error_message = None
                existing.conflict_details = None
                existing.locked_by = None
                existing.locked_at = None
                existing.queued_at = datetime.now(timezone.utc)
                existing.started_at = None
                existing.completed_at = None
                product.publish_status = PublishStatus.QUEUED
                self.db.flush()
                if commit:
                    self.db.commit()
                return {
                    "operationId": str(existing.id),
                    "status": PublishStatus.QUEUED.value,
                    "message": "Product publishing has been queued.",
                }

        active = (
            self.db.query(ProductPublishOperation)
            .filter(
                ProductPublishOperation.shop_id == self.shop.id,
                ProductPublishOperation.shopify_product_gid == product.shopify_product_gid,
                ProductPublishOperation.status.in_(list(ACTIVE_PUBLISH_STATUSES)),
            )
            .first()
        )
        if active:
            raise PublishEnqueueError("PUBLISH_ALREADY_QUEUED", "Another publish operation is active for this product")

        from app.services.media_versions import product_has_active_media_op

        media_lock = product_has_active_media_op(
            self.db, shop_id=self.shop.id, shopify_product_gid=product.shopify_product_gid
        )
        if media_lock == "ROLLBACK_ALREADY_ACTIVE":
            raise PublishEnqueueError(
                "ROLLBACK_ALREADY_ACTIVE",
                "A rollback is already active for this product",
            )

        now = datetime.now(timezone.utc)
        op = ProductPublishOperation(
            shop_id=self.shop.id,
            processing_batch_id=product.batch_id,
            batch_product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            status=PublishStatus.QUEUED,
            current_stage="QUEUED",
            trigger_source=trigger,
            idempotency_key=key,
            output_set_checksum=checksum,
            attempt_number=1,
            baseline_snapshot_json=product.baseline_snapshot_json,
            assets_json=assets,
            queued_at=now,
        )
        self.db.add(op)
        product.publish_status = PublishStatus.QUEUED
        self.db.flush()
        if commit:
            self.db.commit()
        return {
            "operationId": str(op.id),
            "status": PublishStatus.QUEUED.value,
            "message": "Product publishing has been queued.",
        }

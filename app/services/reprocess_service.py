"""Manual reprocess with optional one-time prompt overrides."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    ProcessingBatch,
    Product,
    PublishStatus,
    Shop,
)
from app.services.primary_batch import PrimaryBatchService
from app.services.prompt_resolver import PromptResolver, PromptResolverError
from app.services.prompt_variables import PromptVariableError, validate_prompt_variables
from app.services.state_machine import BATCH_IMAGE_TRANSITIONS, BATCH_PRODUCT_TRANSITIONS, assert_transition

logger = logging.getLogger("app.services.reprocess")

REPROCESSABLE_PRODUCT_STATUSES = frozenset(
    {
        BatchProductStatus.QUEUED,
        BatchProductStatus.RETRYING,
        BatchProductStatus.COMPLETED,
        BatchProductStatus.FAILED,
        BatchProductStatus.SKIPPED,
    }
)

REPROCESSABLE_IMAGE_STATUSES = frozenset(
    {
        BatchImageStatus.QUEUED,
        BatchImageStatus.RETRYING,
        BatchImageStatus.COMPLETED,
        BatchImageStatus.FAILED,
    }
)

BLOCKING_PUBLISH_STATUSES = frozenset(
    {
        PublishStatus.QUEUED,
        PublishStatus.PUBLISHING,
    }
)


class ReprocessError(Exception):
    def __init__(self, message: str, *, code: str = "REPROCESS_ERROR", status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def normalize_override_steps(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Validate and normalize optional one-time override steps."""
    if steps is None:
        return None
    if not isinstance(steps, list) or not steps:
        raise ReprocessError("At least one prompt step is required.", code="PROMPT_OVERRIDE_EMPTY")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise ReprocessError("Each prompt step must be an object.", code="PROMPT_OVERRIDE_INVALID")
        name = str(raw.get("name") or f"Step {index}").strip() or f"Step {index}"
        template = str(
            raw.get("promptTemplate")
            or raw.get("prompt_template")
            or raw.get("prompt")
            or ""
        ).strip()
        if not template:
            raise ReprocessError(
                f'Prompt step "{name}" cannot be empty.',
                code="PROMPT_OVERRIDE_EMPTY",
            )
        try:
            validate_prompt_variables(template)
        except PromptVariableError as exc:
            raise ReprocessError(str(exc), code=exc.code) from exc
        normalized.append(
            {
                "step": index,
                "name": name,
                "promptTemplate": template,
            }
        )
    return normalized


class ReprocessService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop
        self.primary = PrimaryBatchService(db, shop)
        self.resolver = PromptResolver(db, shop)

    def preview_for_batch(self, batch_id: UUID) -> dict[str, Any]:
        batch = self._get_batch(batch_id)
        products = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(BatchProduct.batch_id == batch.id, BatchProduct.shop_id == self.shop.id)
            .order_by(BatchProduct.created_at.asc())
            .all()
        )
        eligible = [p for p in products if self._product_reprocessable(p)]
        if not eligible:
            raise ReprocessError(
                "No products in this batch can be reprocessed right now.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        sample = eligible[0]
        steps, meta = self._resolve_preview_steps(sample, image=self._sample_image(sample))
        product_types = sorted(
            {
                str((p.product_snapshot_json or {}).get("product_type") or "").strip()
                or self._product_type_name(p)
                for p in eligible
            }
        )
        return {
            "scope": "batch",
            "batchId": str(batch.id),
            "productCount": len(eligible),
            "imageCount": sum(len(p.images) for p in eligible),
            "productTypes": [t for t in product_types if t],
            "oneTimeOverride": True,
            "note": (
                "Edited prompts apply to this reprocess only and do not change saved Prompt Configuration. "
                "The same step text is used for every product in the batch; {{variables}} still fill per image."
            ),
            "steps": steps,
            **meta,
        }

    def preview_for_product(self, product_id: UUID) -> dict[str, Any]:
        product = self._get_product(product_id)
        if not self._product_reprocessable(product):
            raise ReprocessError(
                "This product cannot be reprocessed while it is actively processing or publishing.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        steps, meta = self._resolve_preview_steps(product, image=self._sample_image(product))
        return {
            "scope": "product",
            "batchId": str(product.batch_id),
            "productId": str(product.id),
            "shopifyProductGid": product.shopify_product_gid,
            "imageCount": len(product.images),
            "oneTimeOverride": True,
            "note": (
                "Edited prompts apply to this product reprocess only and do not change saved Prompt Configuration."
            ),
            "steps": steps,
            **meta,
        }

    def preview_for_image(self, image_id: UUID) -> dict[str, Any]:
        image = self._get_image(image_id)
        product = self._get_product(image.batch_product_id)
        if not self._image_reprocessable(image) or not self._product_reprocessable(product):
            raise ReprocessError(
                "This image cannot be reprocessed while it is actively processing or publishing.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        steps, meta = self._resolve_preview_steps(product, image=image)
        return {
            "scope": "image",
            "batchId": str(product.batch_id),
            "productId": str(product.id),
            "imageId": str(image.id),
            "shopifyMediaGid": image.shopify_media_gid,
            "oneTimeOverride": True,
            "note": (
                "Edited prompts apply to this image reprocess only and do not change saved Prompt Configuration."
            ),
            "steps": steps,
            **meta,
        }

    def reprocess_batch(
        self,
        batch_id: UUID,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        batch = self._get_batch(batch_id)
        override = normalize_override_steps(steps)
        products = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(BatchProduct.batch_id == batch.id, BatchProduct.shop_id == self.shop.id)
            .all()
        )
        eligible = [p for p in products if self._product_reprocessable(p)]
        if not eligible:
            raise ReprocessError(
                "No products in this batch can be reprocessed right now.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        for product in eligible:
            self._queue_product(product, override=override, images=list(product.images))
        self.db.commit()
        self.primary.refresh_batch_counters(batch)
        self.db.commit()
        return {
            "scope": "batch",
            "batchId": str(batch.id),
            "retriedCount": len(eligible),
            "productIds": [str(p.id) for p in eligible],
            "usedPromptOverride": override is not None,
        }

    def reprocess_product(
        self,
        product_id: UUID,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        product = self._get_product(product_id)
        if not self._product_reprocessable(product):
            raise ReprocessError(
                "This product cannot be reprocessed while it is actively processing or publishing.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        override = normalize_override_steps(steps)
        self._queue_product(product, override=override, images=list(product.images))
        self.db.commit()
        batch = self._get_batch(product.batch_id)
        self.primary.refresh_batch_counters(batch)
        self.db.commit()
        self.db.refresh(product)
        return {
            "scope": "product",
            "batchId": str(product.batch_id),
            "productId": str(product.id),
            "usedPromptOverride": override is not None,
        }

    def reprocess_image(
        self,
        image_id: UUID,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        image = self._get_image(image_id)
        product = self._get_product(image.batch_product_id)
        if not self._image_reprocessable(image) or not self._product_reprocessable(product):
            raise ReprocessError(
                "This image cannot be reprocessed while it is actively processing or publishing.",
                code="REPROCESS_NOT_ELIGIBLE",
            )
        override = normalize_override_steps(steps)
        self._reset_image(image, override=override)
        self._queue_product_for_images(product, images_to_process=[image])
        self.db.commit()
        batch = self._get_batch(product.batch_id)
        self.primary.refresh_batch_counters(batch)
        self.db.commit()
        return {
            "scope": "image",
            "batchId": str(product.batch_id),
            "productId": str(product.id),
            "imageId": str(image.id),
            "usedPromptOverride": override is not None,
        }

    def _queue_product(
        self,
        product: BatchProduct,
        *,
        override: list[dict[str, Any]] | None,
        images: list[BatchImage],
    ) -> None:
        for image in images:
            if image.status not in REPROCESSABLE_IMAGE_STATUSES:
                raise ReprocessError(
                    f"Image {image.id} is actively processing and cannot be reprocessed yet.",
                    code="REPROCESS_NOT_ELIGIBLE",
                )
            self._reset_image(image, override=None)
        product.prompt_override_json = override
        self._queue_product_for_images(product, images_to_process=images)

    def _queue_product_for_images(
        self,
        product: BatchProduct,
        *,
        images_to_process: list[BatchImage],
    ) -> None:
        now = datetime.now(timezone.utc)
        if product.status != BatchProductStatus.QUEUED:
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                product.status,
                BatchProductStatus.QUEUED,
            )
            product.status = BatchProductStatus.QUEUED
        product.next_retry_at = now
        product.error_code = None
        product.error_message = None
        product.locked_by = None
        product.locked_at = None
        product.completed_at = None
        product.retry_count += 1
        if product.publish_status in BLOCKING_PUBLISH_STATUSES:
            raise ReprocessError(
                "Cannot reprocess while a publish operation is in progress.",
                code="REPROCESS_PUBLISH_ACTIVE",
            )
        if product.publish_status in {
            PublishStatus.READY_TO_PUBLISH,
            PublishStatus.PUBLISHED,
            PublishStatus.PUBLISH_FAILED,
            PublishStatus.PUBLISH_CONFLICT,
            PublishStatus.RESTORE_FAILED,
        }:
            product.publish_status = None

        # Ensure sibling completed images stay completed; only queued ones run.
        _ = images_to_process
        logger.info(
            "Queued reprocess | product=%s images=%s override=%s",
            product.id,
            [str(i.id) for i in images_to_process],
            bool(product.prompt_override_json)
            or any(getattr(i, "prompt_override_json", None) for i in images_to_process),
        )

    def _reset_image(self, image: BatchImage, *, override: list[dict[str, Any]] | None) -> None:
        if image.status != BatchImageStatus.QUEUED:
            assert_transition(
                "batch_image",
                BATCH_IMAGE_TRANSITIONS,
                image.status,
                BatchImageStatus.QUEUED,
            )
            image.status = BatchImageStatus.QUEUED
        image.prompt_override_json = override
        image.current_prompt_step = 0
        image.output_storage_key = None
        image.output_url = None
        image.output_mime_type = None
        image.output_checksum = None
        image.generated_shopify_file_gid = None
        image.generated_shopify_cdn_url = None
        image.generated_image_version_id = None
        image.error_code = None
        image.error_message = None
        image.completed_at = None
        image.started_at = None

    def _resolve_preview_steps(
        self,
        batch_product: BatchProduct,
        *,
        image: BatchImage | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        product = self._catalog_product(batch_product)
        product_type_override = None
        if product is None and isinstance(batch_product.product_snapshot_json, dict):
            product_type_override = batch_product.product_snapshot_json.get("product_type")
        elif product is not None and not (product.product_type or "").strip():
            if isinstance(batch_product.product_snapshot_json, dict):
                product_type_override = batch_product.product_snapshot_json.get("product_type")

        image_position = None
        if image is not None:
            images_sorted = sorted(batch_product.images, key=lambda i: i.created_at)
            try:
                image_position = images_sorted.index(image) + 1
            except ValueError:
                image_position = 1

        try:
            resolved = self.resolver.resolve_for_product(
                product,
                product_type_override=product_type_override,
                image=image,
                image_position=image_position,
            )
        except PromptResolverError as exc:
            raise ReprocessError(str(exc), code=exc.code) from exc

        steps = [
            {
                "step": s.step_order,
                "name": s.name,
                "promptTemplate": s.prompt_text,
                "renderedPrompt": s.rendered_prompt,
                "variables": s.variables,
            }
            for s in resolved
        ]
        meta = {
            "productType": product_type_override
            or (product.product_type if product else None)
            or self._product_type_name(batch_product),
            "sampleImageId": str(image.id) if image else None,
            "sampleImagePosition": image_position,
        }
        return steps, meta

    def _product_reprocessable(self, product: BatchProduct) -> bool:
        if product.status == BatchProductStatus.PROCESSING:
            return False
        if product.publish_status in BLOCKING_PUBLISH_STATUSES:
            return False
        if product.status not in REPROCESSABLE_PRODUCT_STATUSES:
            return False
        return True

    def _image_reprocessable(self, image: BatchImage) -> bool:
        return image.status in REPROCESSABLE_IMAGE_STATUSES

    def _sample_image(self, product: BatchProduct) -> BatchImage | None:
        images = sorted(product.images, key=lambda i: i.created_at)
        return images[0] if images else None

    def _catalog_product(self, batch_product: BatchProduct) -> Product | None:
        if not batch_product.product_id:
            return None
        return self.db.get(Product, batch_product.product_id)

    def _product_type_name(self, batch_product: BatchProduct) -> str:
        product = self._catalog_product(batch_product)
        if product and (product.product_type or "").strip():
            return product.product_type.strip()
        snap = batch_product.product_snapshot_json or {}
        return str(snap.get("product_type") or "").strip()

    def _get_batch(self, batch_id: UUID) -> ProcessingBatch:
        batch = self.primary.get_batch(batch_id)
        if batch is None:
            raise ReprocessError("Batch not found", code="BATCH_NOT_FOUND", status_code=404)
        return batch

    def _get_product(self, product_id: UUID) -> BatchProduct:
        product = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(BatchProduct.id == product_id, BatchProduct.shop_id == self.shop.id)
            .one_or_none()
        )
        if product is None:
            raise ReprocessError("Batch product not found", code="PRODUCT_NOT_FOUND", status_code=404)
        return product

    def _get_image(self, image_id: UUID) -> BatchImage:
        image = (
            self.db.query(BatchImage)
            .filter(BatchImage.id == image_id, BatchImage.shop_id == self.shop.id)
            .one_or_none()
        )
        if image is None:
            raise ReprocessError("Batch image not found", code="IMAGE_NOT_FOUND", status_code=404)
        return image

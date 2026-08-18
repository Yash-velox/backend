from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    AttemptStatus,
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    ImageVersion,
    ImageVersionType,
    ProcessingAttempt,
    ProcessingBatch,
    Shop,
)
from app.models.enums import ProcessingPhase
from app.services.primary_batch import PrimaryBatchService
from app.services.state_machine import BATCH_IMAGE_TRANSITIONS, BATCH_PRODUCT_TRANSITIONS, assert_transition

logger = logging.getLogger("app.services.retry")

# Phases where images may briefly be PROCESSING without a local output yet
# (OpenAI result download / import). Do not treat those as dead worker locks.
_OPENAI_IMPORT_PHASES = frozenset(
    {
        ProcessingPhase.WAITING_FOR_OPENAI.value,
        ProcessingPhase.COLLECTING_OPENAI_RESULTS.value,
        ProcessingPhase.IMPORTING_STAGE_RESULTS.value,
        ProcessingPhase.PREPARING_NEXT_STAGE.value,
        ProcessingPhase.AI_WORKFLOW_COMPLETE.value,
    }
)


def _image_awaiting_shopify_upload(image: BatchImage) -> bool:
    """True when OpenAI output exists locally but Shopify Files upload is not done yet."""
    if not image.output_storage_key or image.generated_shopify_file_gid:
        return False
    return image.status not in {BatchImageStatus.FAILED, BatchImageStatus.COMPLETED}


def next_processing_attempt_number(
    db: Session,
    batch_image_id: UUID,
    *,
    start_from: int = 1,
) -> int:
    """Return the next unused processing_attempts.attempt_number for this image."""
    existing = {
        row[0]
        for row in db.query(ProcessingAttempt.attempt_number)
        .filter(ProcessingAttempt.batch_image_id == batch_image_id)
        .all()
    }
    number = max(int(start_from), 1)
    while number in existing:
        number += 1
    return number


def find_generated_version_for_batch_image(db: Session, image: BatchImage) -> ImageVersion | None:
    """Latest GENERATED Shopify file created from this batch image, if any."""
    return (
        db.query(ImageVersion)
        .filter(
            ImageVersion.created_by_batch_image_id == image.id,
            ImageVersion.version_type == ImageVersionType.GENERATED,
            ImageVersion.shopify_file_gid.is_not(None),
        )
        .order_by(ImageVersion.version_number.desc())
        .first()
    )


_COMPLETE_FROM_STATUS: dict[BatchImageStatus, tuple[BatchImageStatus, ...]] = {
    BatchImageStatus.QUEUED: (BatchImageStatus.UPLOADING, BatchImageStatus.COMPLETED),
    BatchImageStatus.RETRYING: (BatchImageStatus.UPLOADING, BatchImageStatus.COMPLETED),
    BatchImageStatus.DOWNLOADING: (
        BatchImageStatus.PROCESSING,
        BatchImageStatus.UPLOADING,
        BatchImageStatus.COMPLETED,
    ),
    BatchImageStatus.PROCESSING: (BatchImageStatus.UPLOADING, BatchImageStatus.COMPLETED),
    BatchImageStatus.WAITING_FOR_PROVIDER: (BatchImageStatus.UPLOADING, BatchImageStatus.COMPLETED),
    BatchImageStatus.UPLOADING: (BatchImageStatus.COMPLETED,),
    BatchImageStatus.FAILED: (
        BatchImageStatus.QUEUED,
        BatchImageStatus.UPLOADING,
        BatchImageStatus.COMPLETED,
    ),
}


def complete_image_from_shopify_file(
    image: BatchImage,
    *,
    file_gid: str,
    cdn_url: str | None = None,
    version_id: UUID | None = None,
    prompt_step: int | None = None,
) -> bool:
    """Reattach a durable Shopify Files upload and mark the image completed.

    Used when upload succeeded but the batch_image row was later reset (stale lock /
    UniqueViolation retry). Returns True when status changed.
    """
    image.generated_shopify_file_gid = file_gid
    if cdn_url:
        image.generated_shopify_cdn_url = cdn_url
    if version_id is not None:
        image.generated_image_version_id = version_id
    if prompt_step is not None:
        image.current_prompt_step = max(image.current_prompt_step, prompt_step)
    elif image.current_prompt_step < 1:
        image.current_prompt_step = 1
    image.error_code = None
    image.error_message = None
    if image.status == BatchImageStatus.COMPLETED:
        if image.completed_at is None:
            image.completed_at = datetime.now(timezone.utc)
        return False
    hops = _COMPLETE_FROM_STATUS.get(image.status)
    if not hops:
        return False
    for target in hops:
        assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, target)
        image.status = target
    image.completed_at = datetime.now(timezone.utc)
    return True


class RetryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _primary_for_shop(self, shop_id: UUID) -> PrimaryBatchService | None:
        shop = self.db.get(Shop, shop_id)
        if shop is None:
            return None
        return PrimaryBatchService(self.db, shop)

    def _product_has_healthy_async_work(self, batch_product: BatchProduct) -> bool:
        """Skip stale recovery while OpenAI wait / result import / Shopify upload is in flight."""
        images = list(batch_product.images or [])
        if any(img.status == BatchImageStatus.WAITING_FOR_PROVIDER for img in images):
            return True
        if any(img.status == BatchImageStatus.UPLOADING for img in images):
            return True
        if any(_image_awaiting_shopify_upload(img) for img in images):
            return True
        batch = self.db.get(ProcessingBatch, batch_product.batch_id)
        if batch is not None and (batch.processing_phase or "") in _OPENAI_IMPORT_PHASES:
            # Import can leave images in PROCESSING for a short window before output_storage_key
            # is written; interrupting that window recreates the RETRYING/COMPLETED race.
            if any(img.status == BatchImageStatus.PROCESSING for img in images):
                return True
        return False

    def _complete_images_from_existing_versions(self, batch_product: BatchProduct) -> int:
        """Reuse unpublished GENERATED Shopify files. Retry-before-publish is not a new version."""
        completed = 0
        for image in list(batch_product.images or []):
            if image.status == BatchImageStatus.COMPLETED and image.generated_shopify_file_gid:
                continue
            version = find_generated_version_for_batch_image(self.db, image)
            if version is None or not version.shopify_file_gid:
                continue
            if complete_image_from_shopify_file(
                image,
                file_gid=version.shopify_file_gid,
                cdn_url=version.shopify_cdn_url,
                version_id=version.id,
            ) or image.status == BatchImageStatus.COMPLETED:
                completed += 1
                logger.info(
                    "Reused existing generated version before publish | image=%s version=%s file=%s",
                    image.id,
                    version.id,
                    version.shopify_file_gid,
                )
        return completed

    def complete_unpublished_generated_work(
        self,
        batch_product: BatchProduct,
        *,
        now: datetime | None = None,
    ) -> bool:
        """If this batch already uploaded Shopify Files, complete without a new version."""
        now = now or datetime.now(timezone.utc)
        self._complete_images_from_existing_versions(batch_product)
        return self._complete_product_if_images_done(batch_product, now=now)

    def _complete_product_if_images_done(self, batch_product: BatchProduct, *, now: datetime) -> bool:
        """Heal products whose images all finished while the product stayed locked/retrying."""
        images = list(batch_product.images or [])
        if not images or not all(img.status == BatchImageStatus.COMPLETED for img in images):
            return False
        if batch_product.status == BatchProductStatus.FAILED:
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                batch_product.status,
                BatchProductStatus.RETRYING,
            )
            batch_product.status = BatchProductStatus.RETRYING
        if batch_product.status == BatchProductStatus.QUEUED:
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                batch_product.status,
                BatchProductStatus.PROCESSING,
            )
            batch_product.status = BatchProductStatus.PROCESSING
        if batch_product.status not in {
            BatchProductStatus.PROCESSING,
            BatchProductStatus.RETRYING,
        }:
            return False
        assert_transition(
            "batch_product",
            BATCH_PRODUCT_TRANSITIONS,
            batch_product.status,
            BatchProductStatus.COMPLETED,
        )
        batch_product.status = BatchProductStatus.COMPLETED
        batch_product.completed_at = now
        batch_product.locked_by = None
        batch_product.locked_at = None
        batch_product.next_retry_at = None
        batch_product.error_code = None
        batch_product.error_message = None
        from app.services.image_processor import ImageProcessor

        ImageProcessor(self.db)._advance_baseline_on_success(batch_product)
        logger.info(
            "Stale recovery healed completed product | product=%s batch=%s",
            batch_product.id,
            batch_product.batch_id,
        )
        return True

    def schedule_image_retry(
        self,
        image: BatchImage,
        batch_product: BatchProduct,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> BatchImageStatus:
        max_attempts = settings.processing_max_attempts
        if retryable and image.attempt_count < max_attempts:
            delay = settings.processing_retry_delay_seconds * (2 ** max(image.attempt_count - 1, 0))
            delay = min(delay, settings.processing_retry_delay_seconds * 16)
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.RETRYING)
            image.status = BatchImageStatus.RETRYING
            image.error_code = error_code
            image.error_message = error_message
            batch_product.retry_count += 1
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                batch_product.status,
                BatchProductStatus.RETRYING,
            )
            batch_product.status = BatchProductStatus.RETRYING
            batch_product.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            batch_product.error_code = error_code
            batch_product.error_message = error_message
            batch_product.locked_by = None
            batch_product.locked_at = None
            logger.info(
                "Image retry scheduled | image=%s attempt=%s next_retry=%s",
                image.id,
                image.attempt_count,
                batch_product.next_retry_at.isoformat(),
            )
            return BatchImageStatus.RETRYING

        assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.FAILED)
        image.status = BatchImageStatus.FAILED
        image.error_code = error_code
        image.error_message = error_message
        image.completed_at = datetime.now(timezone.utc)
        logger.info(
            "Image permanent failure | image=%s attempt=%s code=%s",
            image.id,
            image.attempt_count,
            error_code,
        )
        return BatchImageStatus.FAILED

    def schedule_product_retry(
        self,
        batch_product: BatchProduct,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> BatchProductStatus:
        max_attempts = settings.processing_max_attempts
        if retryable and batch_product.retry_count < max_attempts:
            delay = settings.processing_retry_delay_seconds * (2 ** max(batch_product.retry_count, 0))
            delay = min(delay, settings.processing_retry_delay_seconds * 16)
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                batch_product.status,
                BatchProductStatus.RETRYING,
            )
            batch_product.status = BatchProductStatus.RETRYING
            batch_product.retry_count += 1
            batch_product.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            batch_product.error_code = error_code
            batch_product.error_message = error_message
            batch_product.locked_by = None
            batch_product.locked_at = None
            return BatchProductStatus.RETRYING

        assert_transition(
            "batch_product",
            BATCH_PRODUCT_TRANSITIONS,
            batch_product.status,
            BatchProductStatus.FAILED,
        )
        batch_product.status = BatchProductStatus.FAILED
        batch_product.error_code = error_code
        batch_product.error_message = error_message
        batch_product.completed_at = datetime.now(timezone.utc)
        batch_product.locked_by = None
        batch_product.locked_at = None
        return BatchProductStatus.FAILED

    def recover_stale_batch_products(
        self,
        *,
        worker_id: str,
        batch_id: UUID | None = None,
        force: bool = False,
    ) -> int:
        age_seconds = 0 if force else settings.processing_stale_lock_seconds
        threshold = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        q = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(
                BatchProduct.status.in_([BatchProductStatus.PROCESSING, BatchProductStatus.RETRYING]),
                BatchProduct.locked_at.is_not(None),
                BatchProduct.locked_at <= threshold,
            )
        )
        if batch_id is not None:
            q = q.filter(BatchProduct.batch_id == batch_id)
        stale = q.all()
        recovered = 0
        batch_ids: set[UUID] = set()
        now = datetime.now(timezone.utc)

        for batch_product in stale:
            # OpenAI Platform wait / import / Shopify upload can exceed the lock TTL.
            # Interrupting those races leaves images COMPLETED and products RETRYING forever.
            if self.complete_unpublished_generated_work(batch_product, now=now):
                batch_ids.add(batch_product.batch_id)
                recovered += 1
                continue

            if self._product_has_healthy_async_work(batch_product):
                logger.debug(
                    "Stale recovery skipped healthy async work | product=%s phase_or_images=in_flight",
                    batch_product.id,
                )
                continue

            if self._complete_product_if_images_done(batch_product, now=now):
                batch_ids.add(batch_product.batch_id)
                recovered += 1
                continue

            for image in batch_product.images:
                if image.status not in (BatchImageStatus.DOWNLOADING, BatchImageStatus.PROCESSING):
                    continue
                if _image_awaiting_shopify_upload(image):
                    continue
                # Prefer closing the open STARTED attempt over inserting a duplicate
                # (uq_attempt_image_number) - that UniqueViolation previously aborted
                # recovery and left products stuck in PROCESSING forever.
                open_attempt = (
                    self.db.query(ProcessingAttempt)
                    .filter(
                        ProcessingAttempt.batch_image_id == image.id,
                        ProcessingAttempt.status == AttemptStatus.STARTED,
                    )
                    .order_by(ProcessingAttempt.attempt_number.desc())
                    .first()
                )
                if open_attempt is not None:
                    open_attempt.status = AttemptStatus.INTERRUPTED
                    open_attempt.provider = open_attempt.provider or "system"
                    open_attempt.error_code = "STALE_LOCK"
                    open_attempt.error_message = (
                        "Previous worker did not finish before the stale lock timeout."
                    )
                    open_attempt.completed_at = now
                else:
                    next_number = next_processing_attempt_number(
                        self.db,
                        image.id,
                        start_from=max(image.attempt_count, 1),
                    )
                    self.db.add(
                        ProcessingAttempt(
                            batch_image_id=image.id,
                            batch_product_id=batch_product.id,
                            attempt_number=next_number,
                            status=AttemptStatus.INTERRUPTED,
                            provider="system",
                            shopify_source_url=image.cdn_url,
                            error_code="STALE_LOCK",
                            error_message="Previous worker did not finish before the stale lock timeout.",
                            started_at=image.started_at or batch_product.locked_at or now,
                            completed_at=now,
                        )
                    )
                if image.status != BatchImageStatus.PROCESSING:
                    image.status = BatchImageStatus.PROCESSING
                self.schedule_image_retry(
                    image,
                    batch_product,
                    error_code="STALE_LOCK",
                    error_message="Recovered stale processing lock after worker interruption.",
                    retryable=True,
                )

            # Image retry already moves the product to RETRYING when it can;
            # if status is still PROCESSING (no in-flight images, or image
            # permanently failed), recover at product level.
            if batch_product.status == BatchProductStatus.PROCESSING:
                self.schedule_product_retry(
                    batch_product,
                    error_code="STALE_LOCK",
                    error_message="Recovered stale product lock after worker interruption.",
                    retryable=True,
                )

            batch_ids.add(batch_product.batch_id)
            recovered += 1
            logger.warning(
                "Stale batch product recovered | product=%s previous_worker=%s recovery_worker=%s",
                batch_product.id,
                batch_product.locked_by,
                worker_id,
            )

        self.db.commit()
        for batch_id in batch_ids:
            batch = self.db.get(ProcessingBatch, batch_id)
            if batch:
                svc = self._primary_for_shop(batch.shop_id)
                if svc:
                    svc.refresh_batch_counters(batch)
                    self.db.commit()
        return recovered

    def manual_retry_failed_products(
        self,
        *,
        shop_id: UUID,
        batch_id: UUID | None = None,
        product_ids: list[UUID] | None = None,
    ) -> list[BatchProduct]:
        q = self.db.query(BatchProduct).filter(
            BatchProduct.shop_id == shop_id,
            BatchProduct.status == BatchProductStatus.FAILED,
        )
        if batch_id is not None:
            q = q.filter(BatchProduct.batch_id == batch_id)
        if product_ids is not None:
            q = q.filter(BatchProduct.id.in_(product_ids))
        elif batch_id is None:
            return []

        products = q.all()
        now = datetime.now(timezone.utc)
        batch_ids: set[UUID] = set()

        for batch_product in products:
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                batch_product.status,
                BatchProductStatus.QUEUED,
            )
            batch_product.status = BatchProductStatus.QUEUED
            batch_product.next_retry_at = now
            batch_product.error_code = None
            batch_product.error_message = None
            batch_product.locked_by = None
            batch_product.locked_at = None
            batch_product.completed_at = None

            images = (
                self.db.query(BatchImage)
                .filter(BatchImage.batch_product_id == batch_product.id)
                .all()
            )
            for image in images:
                version = find_generated_version_for_batch_image(self.db, image)
                if version is not None and version.shopify_file_gid:
                    complete_image_from_shopify_file(
                        image,
                        file_gid=version.shopify_file_gid,
                        cdn_url=version.shopify_cdn_url,
                        version_id=version.id,
                    )
                    continue
                if image.status != BatchImageStatus.FAILED:
                    continue
                # Preserve local output for Shopify upload-only retries when present.
                preserve_output = bool(image.output_storage_key) and (
                    (image.error_code or "").startswith("SHOPIFY_")
                    or (image.error_code or "")
                    in {
                        "GENERATED_IMAGE_TOO_LARGE",
                        "GENERATED_IMAGE_PIXEL_LIMIT",
                        "SHOPIFY_TOKEN_MISSING",
                        "PUBLISH_OUTPUT_MISSING",
                        "PUBLISH_OUTPUT_INVALID",
                        "PUBLISH_OUTPUT_NOT_PNG",
                    }
                )
                assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.QUEUED)
                image.status = BatchImageStatus.QUEUED
                if not preserve_output:
                    image.current_prompt_step = 0
                    image.output_storage_key = None
                    image.output_url = None
                    image.generated_shopify_file_gid = None
                    image.generated_shopify_cdn_url = None
                    image.generated_image_version_id = None
                    image.output_checksum = None
                image.error_code = None
                image.error_message = None
                image.completed_at = None

            if self._complete_product_if_images_done(batch_product, now=now):
                batch_ids.add(batch_product.batch_id)
                logger.info(
                    "Manual product retry reused existing version | product=%s shop=%s batch=%s",
                    batch_product.id,
                    shop_id,
                    batch_product.batch_id,
                )
                continue

            batch_ids.add(batch_product.batch_id)
            logger.info(
                "Manual product retry | product=%s shop=%s batch=%s",
                batch_product.id,
                shop_id,
                batch_product.batch_id,
            )

        self.db.commit()
        for bid in batch_ids:
            batch = self.db.get(ProcessingBatch, bid)
            if batch:
                svc = self._primary_for_shop(batch.shop_id)
                if svc:
                    svc.refresh_batch_counters(batch)
                    self.db.commit()

        for batch_product in products:
            self.db.refresh(batch_product)
        return products

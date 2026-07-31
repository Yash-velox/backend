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
    ProcessingAttempt,
    ProcessingBatch,
    Shop,
)
from app.services.primary_batch import PrimaryBatchService
from app.services.state_machine import BATCH_IMAGE_TRANSITIONS, BATCH_PRODUCT_TRANSITIONS, assert_transition

logger = logging.getLogger("app.services.retry")


class RetryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _primary_for_shop(self, shop_id: UUID) -> PrimaryBatchService | None:
        shop = self.db.get(Shop, shop_id)
        if shop is None:
            return None
        return PrimaryBatchService(self.db, shop)

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
            for image in batch_product.images:
                if image.status not in (BatchImageStatus.DOWNLOADING, BatchImageStatus.PROCESSING):
                    continue
                # Prefer closing the open STARTED attempt over inserting a duplicate
                # (uq_attempt_image_number) — that UniqueViolation previously aborted
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
                    next_number = max(image.attempt_count, 1)
                    existing_numbers = {
                        row[0]
                        for row in self.db.query(ProcessingAttempt.attempt_number)
                        .filter(ProcessingAttempt.batch_image_id == image.id)
                        .all()
                    }
                    while next_number in existing_numbers:
                        next_number += 1
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
                if image.status != BatchImageStatus.FAILED:
                    continue
                assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.QUEUED)
                image.status = BatchImageStatus.QUEUED
                image.current_prompt_step = 0
                image.error_code = None
                image.error_message = None
                image.completed_at = None
                image.output_storage_key = None
                image.output_url = None

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

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AttemptStatus, ProcessingAttempt, ProcessingQueueItem, QueueItemStatus
from app.services.batch_service import BatchService, assert_transition

logger = logging.getLogger("app.services.retry")


class RetryService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.batch_service = BatchService(db)

    def schedule_retry(
        self,
        item: ProcessingQueueItem,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> QueueItemStatus:
        now = datetime.now(timezone.utc)
        if retryable and item.attempt_count < item.max_attempts:
            delay = settings.processing_retry_delay_seconds * (2 ** max(item.attempt_count - 1, 0))
            delay = min(delay, settings.processing_retry_delay_seconds * 16)
            assert_transition(item.status, QueueItemStatus.RETRY_PENDING)
            item.status = QueueItemStatus.RETRY_PENDING
            item.next_retry_at = now + timedelta(seconds=delay)
            item.error_code = error_code
            item.error_message = error_message
            item.locked_by = None
            item.locked_at = None
            logger.info(
                "Retry scheduled | item_id=%s attempt=%s next_retry_at=%s",
                item.id,
                item.attempt_count,
                item.next_retry_at.isoformat(),
            )
            return QueueItemStatus.RETRY_PENDING

        assert_transition(item.status, QueueItemStatus.FAILED)
        item.status = QueueItemStatus.FAILED
        item.error_code = error_code
        item.error_message = error_message
        item.next_retry_at = None
        item.locked_by = None
        item.locked_at = None
        item.processing_completed_at = now
        logger.info(
            "Permanent failure | item_id=%s attempt=%s code=%s",
            item.id,
            item.attempt_count,
            error_code,
        )
        return QueueItemStatus.FAILED

    def manual_retry_items(
        self,
        *,
        shop_id: UUID,
        item_ids: list[UUID] | None = None,
        all_failed: bool = False,
    ) -> list[ProcessingQueueItem]:
        q = self.db.query(ProcessingQueueItem).filter(
            ProcessingQueueItem.shop_id == shop_id,
            ProcessingQueueItem.status == QueueItemStatus.FAILED,
        )
        if item_ids is not None:
            q = q.filter(ProcessingQueueItem.id.in_(item_ids))
        elif not all_failed:
            return []

        items = q.all()
        now = datetime.now(timezone.utc)
        for item in items:
            assert_transition(item.status, QueueItemStatus.RETRY_PENDING)
            item.status = QueueItemStatus.RETRY_PENDING
            item.next_retry_at = now
            item.batch_id = None
            item.error_code = None
            item.error_message = None
            item.locked_by = None
            item.locked_at = None
            logger.info("Manual retry | item_id=%s shop_id=%s", item.id, shop_id)

        self.db.commit()
        for item in items:
            self.db.refresh(item)
        return items

    def recover_stale_items(self, *, worker_id: str) -> int:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.processing_stale_lock_seconds)
        stale = (
            self.db.query(ProcessingQueueItem)
            .filter(
                ProcessingQueueItem.status.in_([QueueItemStatus.QUEUED, QueueItemStatus.PROCESSING]),
                ProcessingQueueItem.locked_at.is_not(None),
                ProcessingQueueItem.locked_at < threshold,
            )
            .all()
        )
        recovered = 0
        batch_ids: set[UUID] = set()
        now = datetime.now(timezone.utc)
        for item in stale:
            attempt = ProcessingAttempt(
                queue_item_id=item.id,
                batch_id=item.batch_id,
                attempt_number=max(item.attempt_count, 1),
                status=AttemptStatus.INTERRUPTED,
                provider="system",
                shopify_source_url=item.shopify_cdn_url,
                error_code="STALE_LOCK",
                error_message="Previous worker did not finish before the stale lock timeout.",
                started_at=item.processing_started_at or item.locked_at or now,
                completed_at=now,
            )
            self.db.add(attempt)

            # Ensure status is PROCESSING for transition rules when recovering QUEUED.
            if item.status == QueueItemStatus.QUEUED:
                item.status = QueueItemStatus.PROCESSING

            self.schedule_retry(
                item,
                error_code="STALE_LOCK",
                error_message="Recovered stale processing lock after worker interruption.",
                retryable=True,
            )
            if item.batch_id:
                batch_ids.add(item.batch_id)
            recovered += 1
            logger.warning(
                "Stale item recovered | item_id=%s previous_worker=%s recovery_worker=%s",
                item.id,
                item.locked_by,
                worker_id,
            )

        self.db.commit()
        for batch_id in batch_ids:
            self.batch_service.refresh_batch_summary(batch_id)
        return recovered

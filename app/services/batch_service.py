from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    AttemptStatus,
    BatchStatus,
    ProcessingAttempt,
    ProcessingBatch,
    ProcessingQueueItem,
    QueueItemStatus,
    TriggerType,
)

logger = logging.getLogger("app.services.batch")


ALLOWED_TRANSITIONS: dict[QueueItemStatus, set[QueueItemStatus]] = {
    QueueItemStatus.PENDING: {QueueItemStatus.QUEUED, QueueItemStatus.CANCELLED},
    QueueItemStatus.RETRY_PENDING: {QueueItemStatus.QUEUED, QueueItemStatus.CANCELLED},
    QueueItemStatus.QUEUED: {QueueItemStatus.PROCESSING, QueueItemStatus.CANCELLED, QueueItemStatus.RETRY_PENDING, QueueItemStatus.FAILED},
    QueueItemStatus.PROCESSING: {
        QueueItemStatus.COMPLETED,
        QueueItemStatus.RETRY_PENDING,
        QueueItemStatus.FAILED,
    },
    QueueItemStatus.FAILED: {QueueItemStatus.RETRY_PENDING},
    QueueItemStatus.COMPLETED: set(),
    QueueItemStatus.CANCELLED: set(),
}


def assert_transition(current: QueueItemStatus, new: QueueItemStatus) -> None:
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise ValueError(f"Invalid status transition {current.value} → {new.value}")


class BatchService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def claim_pending_batch(
        self,
        *,
        shop_id: UUID | None,
        trigger_type: TriggerType,
        worker_id: str,
        batch_size: int | None = None,
        started_by: str | None = None,
    ) -> ProcessingBatch | None:
        size = batch_size or settings.processing_batch_size
        now = datetime.now(timezone.utc)

        eligible = and_(
            or_(
                ProcessingQueueItem.status == QueueItemStatus.PENDING,
                and_(
                    ProcessingQueueItem.status == QueueItemStatus.RETRY_PENDING,
                    or_(
                        ProcessingQueueItem.next_retry_at.is_(None),
                        ProcessingQueueItem.next_retry_at <= now,
                    ),
                ),
            )
        )
        if shop_id is not None:
            eligible = and_(eligible, ProcessingQueueItem.shop_id == shop_id)

        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        stmt = (
            select(ProcessingQueueItem)
            .where(eligible)
            .order_by(ProcessingQueueItem.priority.desc(), ProcessingQueueItem.created_at.asc())
            .limit(size)
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        else:
            stmt = stmt.with_for_update()

        items = list(self.db.execute(stmt).scalars().all())
        if not items:
            return None

        claimed_shop_id = shop_id or items[0].shop_id
        # Keep a batch scoped to one shop even if auto worker claims globally.
        same_shop_items = [i for i in items if i.shop_id == claimed_shop_id]
        if not same_shop_items:
            return None

        batch = ProcessingBatch(
            shop_id=claimed_shop_id,
            trigger_type=trigger_type,
            status=BatchStatus.CREATED,
            batch_size=size,
            total_items=len(same_shop_items),
            pending_items=0,
            queued_items=len(same_shop_items),
            processing_items=0,
            completed_items=0,
            retry_pending_items=0,
            failed_items=0,
            cancelled_items=0,
            started_by=started_by or worker_id,
            started_at=now,
        )
        self.db.add(batch)
        self.db.flush()

        for item in same_shop_items:
            assert_transition(item.status, QueueItemStatus.QUEUED)
            item.status = QueueItemStatus.QUEUED
            item.batch_id = batch.id
            item.locked_by = worker_id
            item.locked_at = now
            item.error_code = None
            item.error_message = None
            item.next_retry_at = None

        batch.status = BatchStatus.PROCESSING
        self.db.commit()
        self.db.refresh(batch)

        logger.info(
            "Batch claimed | batch_id=%s shop_id=%s trigger=%s items=%s worker=%s",
            batch.id,
            claimed_shop_id,
            trigger_type.value,
            len(same_shop_items),
            worker_id,
        )
        return batch

    def refresh_batch_summary(self, batch_id: UUID) -> ProcessingBatch | None:
        batch = self.db.get(ProcessingBatch, batch_id)
        if not batch:
            return None

        rows = (
            self.db.query(ProcessingQueueItem.status, func.count(ProcessingQueueItem.id))
            .filter(ProcessingQueueItem.batch_id == batch_id)
            .group_by(ProcessingQueueItem.status)
            .all()
        )
        counts = {status: count for status, count in rows}
        batch.total_items = sum(counts.values())
        batch.pending_items = counts.get(QueueItemStatus.PENDING, 0)
        batch.queued_items = counts.get(QueueItemStatus.QUEUED, 0)
        batch.processing_items = counts.get(QueueItemStatus.PROCESSING, 0)
        batch.completed_items = counts.get(QueueItemStatus.COMPLETED, 0)
        batch.retry_pending_items = counts.get(QueueItemStatus.RETRY_PENDING, 0)
        batch.failed_items = counts.get(QueueItemStatus.FAILED, 0)
        batch.cancelled_items = counts.get(QueueItemStatus.CANCELLED, 0)

        active = batch.queued_items + batch.processing_items
        if active == 0 and batch.total_items > 0:
            completed = batch.completed_items
            failed = batch.failed_items
            cancelled = batch.cancelled_items
            retrying = batch.retry_pending_items
            if completed == batch.total_items:
                batch.status = BatchStatus.COMPLETED
            elif failed == batch.total_items:
                batch.status = BatchStatus.FAILED
            elif cancelled == batch.total_items:
                batch.status = BatchStatus.CANCELLED
            elif completed > 0 and (failed > 0 or cancelled > 0 or retrying > 0):
                batch.status = BatchStatus.PARTIALLY_COMPLETED
            elif retrying > 0 and completed == 0 and failed == 0:
                # Items moved back to retry outside the batch active set
                batch.status = BatchStatus.PARTIALLY_COMPLETED
            elif failed > 0 and completed == 0:
                batch.status = BatchStatus.FAILED
            else:
                batch.status = BatchStatus.PARTIALLY_COMPLETED
            batch.completed_at = datetime.now(timezone.utc)
        elif batch.status == BatchStatus.CREATED:
            batch.status = BatchStatus.PROCESSING

        self.db.commit()
        self.db.refresh(batch)
        return batch

    def list_batches(
        self,
        *,
        shop_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ProcessingBatch], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        q = self.db.query(ProcessingBatch).filter(ProcessingBatch.shop_id == shop_id)
        total = q.count()
        items = (
            q.order_by(ProcessingBatch.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_batch(self, *, shop_id: UUID, batch_id: UUID) -> ProcessingBatch | None:
        return (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == batch_id, ProcessingBatch.shop_id == shop_id)
            .one_or_none()
        )

    def get_batch_items(self, *, shop_id: UUID, batch_id: UUID) -> list[ProcessingQueueItem]:
        return (
            self.db.query(ProcessingQueueItem)
            .filter(
                ProcessingQueueItem.batch_id == batch_id,
                ProcessingQueueItem.shop_id == shop_id,
            )
            .order_by(ProcessingQueueItem.created_at.asc())
            .all()
        )

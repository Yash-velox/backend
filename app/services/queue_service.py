from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models import ProcessingQueueItem, QueueItemStatus
from app.services.batch_service import assert_transition

logger = logging.getLogger("app.services.queue")

SORT_WHITELIST = {
    "created_at": ProcessingQueueItem.created_at,
    "priority": ProcessingQueueItem.priority,
    "status": ProcessingQueueItem.status,
    "processing_started_at": ProcessingQueueItem.processing_started_at,
    "processing_completed_at": ProcessingQueueItem.processing_completed_at,
}


class QueueService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_items(
        self,
        *,
        shop_id: UUID,
        page: int = 1,
        page_size: int = 20,
        status: QueueItemStatus | None = None,
        batch_id: UUID | None = None,
        shopify_product_id: str | None = None,
        filename: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> tuple[list[ProcessingQueueItem], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        q = self.db.query(ProcessingQueueItem).filter(ProcessingQueueItem.shop_id == shop_id)
        if status:
            q = q.filter(ProcessingQueueItem.status == status)
        if batch_id:
            q = q.filter(ProcessingQueueItem.batch_id == batch_id)
        if shopify_product_id:
            q = q.filter(ProcessingQueueItem.shopify_product_id == shopify_product_id)
        if filename:
            q = q.filter(ProcessingQueueItem.original_filename.ilike(f"%{filename}%"))
        if created_from:
            q = q.filter(ProcessingQueueItem.created_at >= created_from)
        if created_to:
            q = q.filter(ProcessingQueueItem.created_at <= created_to)

        total = q.count()
        col = SORT_WHITELIST.get(sort_by, ProcessingQueueItem.created_at)
        order = col.asc() if sort_dir.lower() == "asc" else col.desc()
        items = q.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
        return items, total

    def get_item(self, *, shop_id: UUID, item_id: UUID) -> ProcessingQueueItem | None:
        return (
            self.db.query(ProcessingQueueItem)
            .options(selectinload(ProcessingQueueItem.attempts))
            .filter(ProcessingQueueItem.id == item_id, ProcessingQueueItem.shop_id == shop_id)
            .one_or_none()
        )

    def summary(self, *, shop_id: UUID) -> dict:
        from sqlalchemy import func

        from app.models import BatchStatus, ProcessingBatch

        rows = (
            self.db.query(ProcessingQueueItem.status, func.count(ProcessingQueueItem.id))
            .filter(ProcessingQueueItem.shop_id == shop_id)
            .group_by(ProcessingQueueItem.status)
            .all()
        )
        counts = {s.value: 0 for s in QueueItemStatus}
        total = 0
        for status, count in rows:
            counts[status.value] = count
            total += count

        active_batches = (
            self.db.query(func.count(ProcessingBatch.id))
            .filter(
                ProcessingBatch.shop_id == shop_id,
                ProcessingBatch.status.in_([BatchStatus.CREATED, BatchStatus.PROCESSING]),
            )
            .scalar()
            or 0
        )
        return {
            "total": total,
            "pending": counts[QueueItemStatus.PENDING.value],
            "queued": counts[QueueItemStatus.QUEUED.value],
            "processing": counts[QueueItemStatus.PROCESSING.value],
            "completed": counts[QueueItemStatus.COMPLETED.value],
            "failed": counts[QueueItemStatus.FAILED.value],
            "retryPending": counts[QueueItemStatus.RETRY_PENDING.value],
            "cancelled": counts[QueueItemStatus.CANCELLED.value],
            "activeBatchCount": int(active_batches),
        }

    def cancel_item(self, *, shop_id: UUID, item_id: UUID) -> ProcessingQueueItem:
        item = self.get_item(shop_id=shop_id, item_id=item_id)
        if not item:
            raise LookupError("Queue item not found")
        assert_transition(item.status, QueueItemStatus.CANCELLED)
        item.status = QueueItemStatus.CANCELLED
        item.locked_by = None
        item.locked_at = None
        self.db.commit()
        self.db.refresh(item)
        logger.info("Queue item cancelled | item_id=%s shop_id=%s", item.id, shop_id)
        return item

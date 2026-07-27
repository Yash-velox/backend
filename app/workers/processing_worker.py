from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import distinct, or_, select

from app.config import settings
from app.db.session import SessionLocal
from app.models import ProcessingQueueItem, QueueItemStatus, TriggerType
from app.services.batch_service import BatchService
from app.services.image_processor import ImageProcessor
from app.services.retry_service import RetryService

logger = logging.getLogger("app.workers.processing")


class ProcessingWorker:
    def __init__(self) -> None:
        self.worker_id = settings.effective_worker_id
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("Worker already running | worker_id=%s", self.worker_id)
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"processing-worker-{self.worker_id}")
        logger.info(
            "Processing worker started | worker_id=%s auto=%s poll=%ss batch_size=%s concurrency=%s",
            self.worker_id,
            settings.auto_processing_enabled,
            settings.processing_poll_interval_seconds,
            settings.processing_batch_size,
            settings.processing_batch_concurrency,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None
        logger.info("Processing worker stopped | worker_id=%s", self.worker_id)

    async def _run_loop(self) -> None:
        await asyncio.to_thread(self._recover_stale)
        while not self._stop.is_set():
            try:
                if settings.auto_processing_enabled:
                    await self._process_available_work()
                await asyncio.to_thread(self._recover_stale)
            except Exception:
                logger.exception("Worker loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.processing_poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _recover_stale(self) -> None:
        db = SessionLocal()
        try:
            recovered = RetryService(db).recover_stale_items(worker_id=self.worker_id)
            if recovered:
                logger.info("Stale recovery finished | recovered=%s worker_id=%s", recovered, self.worker_id)
        finally:
            db.close()

    async def _process_available_work(self) -> None:
        shop_ids = await asyncio.to_thread(self._shops_with_pending)
        for shop_id in shop_ids:
            if self._stop.is_set():
                return
            batch_id = await asyncio.to_thread(self._claim_for_shop, shop_id)
            if not batch_id:
                continue
            await self._run_batch(batch_id)

    def _shops_with_pending(self) -> list[UUID]:
        db = SessionLocal()
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            rows = db.execute(
                select(distinct(ProcessingQueueItem.shop_id)).where(
                    or_(
                        ProcessingQueueItem.status == QueueItemStatus.PENDING,
                        (
                            (ProcessingQueueItem.status == QueueItemStatus.RETRY_PENDING)
                            & (
                                (ProcessingQueueItem.next_retry_at.is_(None))
                                | (ProcessingQueueItem.next_retry_at <= now)
                            )
                        ),
                    )
                )
            ).all()
            return [row[0] for row in rows]
        finally:
            db.close()

    def _claim_for_shop(self, shop_id: UUID) -> UUID | None:
        db = SessionLocal()
        try:
            batch = BatchService(db).claim_pending_batch(
                shop_id=shop_id,
                trigger_type=TriggerType.AUTOMATIC,
                worker_id=self.worker_id,
            )
            return batch.id if batch else None
        finally:
            db.close()

    async def _run_batch(self, batch_id: UUID) -> None:
        item_ids = await asyncio.to_thread(self._batch_item_ids, batch_id)
        if not item_ids:
            return
        sem = asyncio.Semaphore(settings.processing_batch_concurrency)

        async def _one(item_id: UUID) -> None:
            async with sem:
                if self._stop.is_set():
                    return
                await asyncio.to_thread(self._process_item, item_id)

        await asyncio.gather(*[_one(i) for i in item_ids], return_exceptions=True)
        logger.info("Batch processing pass finished | batch_id=%s items=%s", batch_id, len(item_ids))

    def _batch_item_ids(self, batch_id: UUID) -> list[UUID]:
        db = SessionLocal()
        try:
            rows = (
                db.query(ProcessingQueueItem.id)
                .filter(
                    ProcessingQueueItem.batch_id == batch_id,
                    ProcessingQueueItem.status == QueueItemStatus.QUEUED,
                )
                .all()
            )
            return [r[0] for r in rows]
        finally:
            db.close()

    def _process_item(self, item_id: UUID) -> None:
        db = SessionLocal()
        try:
            ImageProcessor(db).process_queue_item(item_id, worker_id=self.worker_id)
        except Exception:
            logger.exception("Item processing crashed | item_id=%s worker_id=%s", item_id, self.worker_id)
        finally:
            db.close()


# Module-level singleton used by lifespan and manual start kickoff
processing_worker = ProcessingWorker()


async def kickoff_batch_processing(batch_id: UUID) -> None:
    """Process a manually claimed batch without blocking the HTTP response."""
    await processing_worker._run_batch(batch_id)

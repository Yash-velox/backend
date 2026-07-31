"""Database-backed Shopify publish worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select

from app.config import settings
from app.db.session import SessionLocal
from app.models import ProductPublishOperation, PublishStatus, Shop, ShopStatus
from app.services.product_publisher import ProductPublisher
from app.services.publish_trigger import PublishTriggerService

logger = logging.getLogger("app.workers.publish")


class PublishWorker:
    def __init__(self) -> None:
        self.worker_id = settings.effective_publish_worker_id
        self._task: asyncio.Task | None = None
        self._recover_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("Publish worker already running | worker_id=%s", self.worker_id)
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"publish-worker-{self.worker_id}")
        self._recover_task = asyncio.create_task(
            self._recover_loop(),
            name=f"publish-recover-{self.worker_id}",
        )
        logger.info(
            "Publish worker started | worker_id=%s poll=%ss concurrency=%s",
            self.worker_id,
            settings.publish_poll_interval_seconds,
            settings.publish_product_concurrency,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._recover_task):
            if not task:
                continue
            try:
                await asyncio.wait_for(task, timeout=15)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._recover_task = None
        logger.info("Publish worker stopped | worker_id=%s", self.worker_id)

    async def _recover_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._recover_stale)
            except Exception:
                logger.exception("Publish recover loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5, min(60, settings.publish_stale_lock_seconds // 4 or 15)),
                )
            except asyncio.TimeoutError:
                continue

    async def _run_loop(self) -> None:
        await asyncio.to_thread(self._recover_stale)
        while not self._stop.is_set():
            try:
                await self._process_available_work()
            except Exception:
                logger.exception("Publish worker loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.publish_poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _process_available_work(self) -> None:
        concurrency = max(1, settings.publish_product_concurrency)
        while not self._stop.is_set():
            op_ids = await asyncio.to_thread(self._claim_batch, concurrency)
            if not op_ids:
                return
            await asyncio.gather(*(asyncio.to_thread(self._run_one, op_id) for op_id in op_ids))

    def _claim_batch(self, limit: int) -> list[UUID]:
        db = SessionLocal()
        try:
            dialect = db.bind.dialect.name if db.bind is not None else ""
            stmt = (
                select(ProductPublishOperation)
                .where(ProductPublishOperation.status == PublishStatus.QUEUED)
                .order_by(ProductPublishOperation.queued_at.asc())
                .limit(limit)
            )
            if dialect == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            else:
                stmt = stmt.with_for_update()
            rows = list(db.execute(stmt).scalars().all())
            now = datetime.now(timezone.utc)
            ids: list[UUID] = []
            for op in rows:
                # Skip if another active op exists for same product gid (different output set)
                sibling = (
                    db.query(ProductPublishOperation)
                    .filter(
                        ProductPublishOperation.shop_id == op.shop_id,
                        ProductPublishOperation.shopify_product_gid == op.shopify_product_gid,
                        ProductPublishOperation.status == PublishStatus.PUBLISHING,
                        ProductPublishOperation.id != op.id,
                    )
                    .first()
                )
                if sibling:
                    continue
                op.status = PublishStatus.PUBLISHING
                op.current_stage = "CLAIMED"
                op.locked_by = self.worker_id
                op.locked_at = now
                if op.started_at is None:
                    op.started_at = now
                ids.append(op.id)
            db.commit()
            return ids
        finally:
            db.close()

    def _run_one(self, operation_id: UUID) -> None:
        db = SessionLocal()
        try:
            op = db.query(ProductPublishOperation).filter(ProductPublishOperation.id == operation_id).one_or_none()
            if not op:
                return
            shop = db.query(Shop).filter(Shop.id == op.shop_id, Shop.status == ShopStatus.ACTIVE).one_or_none()
            if not shop:
                op.status = PublishStatus.PUBLISH_FAILED
                op.last_error_code = "PUBLISH_LOCKED"
                op.last_error_message = "Shop inactive or missing"
                op.completed_at = datetime.now(timezone.utc)
                db.commit()
                return
            logger.info(
                "Publish starting | op=%s shop=%s batch_product=%s product=%s",
                op.id,
                shop.id,
                op.batch_product_id,
                op.shopify_product_gid,
            )
            ProductPublisher(db, shop).run(op.id)
        except Exception:
            logger.exception("Publish run failed | op=%s", operation_id)
            try:
                op = db.query(ProductPublishOperation).filter(ProductPublishOperation.id == operation_id).one_or_none()
                if op and op.status == PublishStatus.PUBLISHING:
                    op.status = PublishStatus.PUBLISH_FAILED
                    op.last_error_code = "PUBLISH_NETWORK_ERROR"
                    op.last_error_message = "Unexpected publish worker failure"
                    op.completed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                logger.exception("Failed to mark publish op failed | op=%s", operation_id)
        finally:
            db.close()

    def _recover_stale(self) -> None:
        db = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.publish_stale_lock_seconds)
            stale = (
                db.query(ProductPublishOperation)
                .filter(
                    ProductPublishOperation.status == PublishStatus.PUBLISHING,
                    or_(
                        ProductPublishOperation.locked_at.is_(None),
                        ProductPublishOperation.locked_at < cutoff,
                    ),
                )
                .all()
            )
            for op in stale:
                logger.warning("Requeueing stale publish op | op=%s locked_at=%s", op.id, op.locked_at)
                op.status = PublishStatus.QUEUED
                op.current_stage = "QUEUED"
                op.locked_by = None
                op.locked_at = None
                op.queued_at = datetime.now(timezone.utc)
            db.commit()

            # Soft recovery: ensure terminal batches with auto-publish get triggered
            shops = db.query(Shop).filter(Shop.status == ShopStatus.ACTIVE).all()
            for shop in shops:
                # no-op scan placeholder — enqueue is driven from primary_batch terminal hook
                _ = PublishTriggerService(db, shop)
        finally:
            db.close()


publish_worker = PublishWorker()

"""Database-backed Shopify publish + rollback media worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select

from app.config import settings
from app.db.session import SessionLocal
from app.models import (
    ProductPublishOperation,
    ProductRollbackOperation,
    PublishStatus,
    RollbackStatus,
    Shop,
    ShopStatus,
)
from app.services.product_publisher import ProductPublisher
from app.services.product_rollback import ProductRollbackService
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
            work = await asyncio.to_thread(self._claim_batch, concurrency)
            if not work:
                return
            await asyncio.gather(*(asyncio.to_thread(self._run_claimed, kind, op_id) for kind, op_id in work))

    def _claim_batch(self, limit: int) -> list[tuple[str, UUID]]:
        db = SessionLocal()
        try:
            dialect = db.bind.dialect.name if db.bind is not None else ""
            claimed: list[tuple[str, UUID]] = []
            now = datetime.now(timezone.utc)

            # Prefer publishing first, then rollback, within the concurrency budget.
            pub_stmt = (
                select(ProductPublishOperation)
                .where(ProductPublishOperation.status == PublishStatus.QUEUED)
                .order_by(ProductPublishOperation.queued_at.asc())
                .limit(limit)
            )
            if dialect == "postgresql":
                pub_stmt = pub_stmt.with_for_update(skip_locked=True)
            else:
                pub_stmt = pub_stmt.with_for_update()
            pubs = list(db.execute(pub_stmt).scalars().all())
            for op in pubs:
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
                rb_active = (
                    db.query(ProductRollbackOperation)
                    .filter(
                        ProductRollbackOperation.shop_id == op.shop_id,
                        ProductRollbackOperation.shopify_product_gid == op.shopify_product_gid,
                        ProductRollbackOperation.status.in_(
                            [RollbackStatus.QUEUED, RollbackStatus.ROLLING_BACK]
                        ),
                    )
                    .first()
                )
                if sibling or rb_active:
                    continue
                op.status = PublishStatus.PUBLISHING
                op.current_stage = "CLAIMED"
                op.locked_by = self.worker_id
                op.locked_at = now
                if op.started_at is None:
                    op.started_at = now
                claimed.append(("publish", op.id))
                if len(claimed) >= limit:
                    break

            remaining = limit - len(claimed)
            if remaining > 0:
                rb_stmt = (
                    select(ProductRollbackOperation)
                    .where(ProductRollbackOperation.status == RollbackStatus.QUEUED)
                    .order_by(ProductRollbackOperation.queued_at.asc())
                    .limit(remaining)
                )
                if dialect == "postgresql":
                    rb_stmt = rb_stmt.with_for_update(skip_locked=True)
                else:
                    rb_stmt = rb_stmt.with_for_update()
                rbs = list(db.execute(rb_stmt).scalars().all())
                for op in rbs:
                    pub_active = (
                        db.query(ProductPublishOperation)
                        .filter(
                            ProductPublishOperation.shop_id == op.shop_id,
                            ProductPublishOperation.shopify_product_gid == op.shopify_product_gid,
                            ProductPublishOperation.status.in_(
                                [PublishStatus.QUEUED, PublishStatus.PUBLISHING]
                            ),
                        )
                        .first()
                    )
                    sibling = (
                        db.query(ProductRollbackOperation)
                        .filter(
                            ProductRollbackOperation.shop_id == op.shop_id,
                            ProductRollbackOperation.shopify_product_gid == op.shopify_product_gid,
                            ProductRollbackOperation.status == RollbackStatus.ROLLING_BACK,
                            ProductRollbackOperation.id != op.id,
                        )
                        .first()
                    )
                    if pub_active or sibling:
                        continue
                    op.status = RollbackStatus.ROLLING_BACK
                    op.current_stage = "CLAIMED"
                    op.locked_by = self.worker_id
                    op.locked_at = now
                    if op.started_at is None:
                        op.started_at = now
                    claimed.append(("rollback", op.id))

            db.commit()
            return claimed
        finally:
            db.close()

    def _run_claimed(self, kind: str, operation_id: UUID) -> None:
        if kind == "publish":
            self._run_publish(operation_id)
        else:
            self._run_rollback(operation_id)

    def _run_publish(self, operation_id: UUID) -> None:
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

    def _run_rollback(self, operation_id: UUID) -> None:
        db = SessionLocal()
        try:
            op = (
                db.query(ProductRollbackOperation)
                .filter(ProductRollbackOperation.id == operation_id)
                .one_or_none()
            )
            if not op:
                return
            shop = db.query(Shop).filter(Shop.id == op.shop_id, Shop.status == ShopStatus.ACTIVE).one_or_none()
            if not shop:
                op.status = RollbackStatus.ROLLBACK_FAILED
                op.last_error_code = "ROLLBACK_FAILED"
                op.last_error_message = "Shop inactive or missing"
                op.completed_at = datetime.now(timezone.utc)
                db.commit()
                return
            logger.info(
                "Rollback starting | op=%s shop=%s product=%s target=%s",
                op.id,
                shop.id,
                op.shopify_product_gid,
                op.target_version_id,
            )
            ProductRollbackService(db, shop).run(op.id)
        except Exception:
            logger.exception("Rollback run failed | op=%s", operation_id)
            try:
                op = (
                    db.query(ProductRollbackOperation)
                    .filter(ProductRollbackOperation.id == operation_id)
                    .one_or_none()
                )
                if op and op.status == RollbackStatus.ROLLING_BACK:
                    op.status = RollbackStatus.ROLLBACK_FAILED
                    op.last_error_code = "ROLLBACK_FAILED"
                    op.last_error_message = "Unexpected rollback worker failure"
                    op.completed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                logger.exception("Failed to mark rollback op failed | op=%s", operation_id)
        finally:
            db.close()

    def _recover_stale(self) -> None:
        db = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.publish_stale_lock_seconds)
            stale_pub = (
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
            for op in stale_pub:
                logger.warning("Requeueing stale publish op | op=%s locked_at=%s", op.id, op.locked_at)
                op.status = PublishStatus.QUEUED
                op.current_stage = "QUEUED"
                op.locked_by = None
                op.locked_at = None
                op.queued_at = datetime.now(timezone.utc)

            stale_rb = (
                db.query(ProductRollbackOperation)
                .filter(
                    ProductRollbackOperation.status == RollbackStatus.ROLLING_BACK,
                    or_(
                        ProductRollbackOperation.locked_at.is_(None),
                        ProductRollbackOperation.locked_at < cutoff,
                    ),
                )
                .all()
            )
            for op in stale_rb:
                logger.warning("Requeueing stale rollback op | op=%s locked_at=%s", op.id, op.locked_at)
                op.status = RollbackStatus.QUEUED
                op.current_stage = "QUEUED"
                op.locked_by = None
                op.locked_at = None
                op.queued_at = datetime.now(timezone.utc)

            db.commit()

            shops = db.query(Shop).filter(Shop.status == ShopStatus.ACTIVE).all()
            for shop in shops:
                _ = PublishTriggerService(db, shop)
        finally:
            db.close()


publish_worker = PublishWorker()

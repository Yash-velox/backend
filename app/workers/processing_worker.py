from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import distinct, select

from app.config import settings
from app.db.session import SessionLocal
from app.models import (
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    ProcessingBatch,
    SecondaryQueueItem,
    SecondaryQueueStatus,
    Shop,
    ShopSettings,
    ShopStatus,
)
from app.services.image_processor import ImageProcessor
from app.services.primary_batch import PrimaryBatchService
from app.services.retry_service import RetryService
from app.services.secondary_queue import SecondaryQueueService

logger = logging.getLogger("app.workers.processing")


class ProcessingWorker:
    def __init__(self) -> None:
        self.worker_id = settings.effective_worker_id
        self._task: asyncio.Task | None = None
        self._recover_task: asyncio.Task | None = None
        self._token_task: asyncio.Task | None = None
        self._webhook_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("Worker already running | worker_id=%s", self.worker_id)
            return
        self._stop.clear()
        try:
            from app.services.output_storage import cleanup_expired_temp_outputs

            cleanup_stats = await asyncio.to_thread(cleanup_expired_temp_outputs)
            logger.info(
                "Startup temp output cleanup | scanned=%s deleted=%s",
                cleanup_stats.get("scanned"),
                cleanup_stats.get("deleted"),
            )
        except Exception:
            logger.exception("Startup temp output cleanup failed | worker_id=%s", self.worker_id)
        self._task = asyncio.create_task(self._run_loop(), name=f"processing-worker-{self.worker_id}")
        self._recover_task = asyncio.create_task(
            self._recover_loop(),
            name=f"processing-recover-{self.worker_id}",
        )
        self._token_task = asyncio.create_task(
            self._token_refresh_loop(),
            name=f"processing-token-refresh-{self.worker_id}",
        )
        self._webhook_task = asyncio.create_task(
            self._webhook_loop(),
            name=f"processing-webhooks-{self.worker_id}",
        )
        logger.info(
            "Processing worker started | worker_id=%s auto=%s poll=%ss concurrency=%s stale=%ss token_check=%ss webhook_concurrency=%s webhook_poll=%ss",
            self.worker_id,
            settings.auto_processing_enabled,
            settings.processing_poll_interval_seconds,
            settings.processing_batch_concurrency,
            settings.processing_stale_lock_seconds,
            settings.shopify_token_refresh_check_seconds,
            settings.webhook_process_concurrency,
            settings.webhook_poll_interval_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._recover_task, self._token_task, self._webhook_task):
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
        self._token_task = None
        self._webhook_task = None
        logger.info("Processing worker stopped | worker_id=%s", self.worker_id)

    async def _recover_loop(self) -> None:
        """Recover stale locks even while the main loop is blocked on OpenAI/CDN."""
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._recover_stale)
            except Exception:
                logger.exception("Recover loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5, min(30, settings.processing_stale_lock_seconds // 4 or 5)),
                )
            except asyncio.TimeoutError:
                continue

    async def _token_refresh_loop(self) -> None:
        """Scenario 2: every ~23h, refresh due Shopify Admin tokens into shops."""
        # Run once at startup so a stale UAT token is fixed without waiting an hour.
        try:
            await asyncio.to_thread(self._refresh_due_tokens)
        except Exception:
            logger.exception("Startup Shopify token refresh failed | worker_id=%s", self.worker_id)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(60, int(settings.shopify_token_refresh_check_seconds)),
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await asyncio.to_thread(self._refresh_due_tokens)
            except Exception:
                logger.exception("Shopify token refresh loop error | worker_id=%s", self.worker_id)

    async def _webhook_loop(self) -> None:
        """Drain queued products/update events without blocking OpenAI processing."""
        while not self._stop.is_set():
            try:
                claimed = await asyncio.to_thread(self._claim_webhooks)
                if claimed:
                    sem = asyncio.Semaphore(max(1, int(settings.webhook_process_concurrency)))

                    async def _one(event_id: UUID) -> None:
                        async with sem:
                            await asyncio.to_thread(self._process_webhook, event_id)

                    await asyncio.gather(*[_one(event_id) for event_id in claimed], return_exceptions=True)
            except Exception:
                logger.exception("Webhook loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.5, float(settings.webhook_poll_interval_seconds)),
                )
            except asyncio.TimeoutError:
                continue

    def _claim_webhooks(self) -> list[UUID]:
        from app.services.webhook_intake import WebhookIntakeService

        db = SessionLocal()
        try:
            return WebhookIntakeService(db).claim_queued(worker_id=self.worker_id)
        finally:
            db.close()

    def _process_webhook(self, event_id: UUID) -> None:
        from app.services.webhook_intake import WebhookIntakeService

        db = SessionLocal()
        try:
            event = WebhookIntakeService(db).process_event(event_id)
            logger.info(
                "Webhook processed | webhook_id=%s product=%s result=%s worker=%s",
                event.shopify_webhook_id,
                event.shopify_product_gid,
                event.processing_result.value,
                self.worker_id,
            )
        except Exception:
            logger.exception("Webhook event processing failed | event_id=%s worker=%s", event_id, self.worker_id)
        finally:
            db.close()

    def _refresh_due_tokens(self) -> None:
        from app.services.shopify_token_refresh import refresh_due_shop_tokens

        db = SessionLocal()
        try:
            refreshed = refresh_due_shop_tokens(db)
            if refreshed:
                logger.info(
                    "Proactive Shopify token refresh finished | refreshed=%s worker_id=%s",
                    refreshed,
                    self.worker_id,
                )
        finally:
            db.close()

    async def _run_loop(self) -> None:
        await asyncio.to_thread(self._recover_stale)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._convert_secondary_queues)
                if settings.auto_processing_enabled:
                    await self._process_available_work()
            except Exception:
                logger.exception("Worker loop error | worker_id=%s", self.worker_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.processing_poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _recover_stale(self) -> None:
        db = SessionLocal()
        try:
            recovered = RetryService(db).recover_stale_batch_products(worker_id=self.worker_id)
            if recovered:
                logger.info("Stale recovery finished | recovered=%s worker_id=%s", recovered, self.worker_id)
        finally:
            db.close()

    def _convert_secondary_queues(self) -> None:
        """When Auto Sync is on, claim Secondary Queue products into Primary batches."""
        db = SessionLocal()
        try:
            shops = (
                db.query(Shop)
                .join(ShopSettings, ShopSettings.shop_id == Shop.id)
                .filter(Shop.status == ShopStatus.ACTIVE, ShopSettings.auto_sync_enabled.is_(True))
                .all()
            )
            for shop in shops:
                settings_row = shop.settings
                if settings_row is None:
                    continue
                primary = PrimaryBatchService(db, shop)
                if not primary.should_create_automatic_batch(settings_row):
                    continue
                claimed = SecondaryQueueService(db, shop).claim_pending_for_conversion(
                    limit=settings.auto_batch_claim_limit,
                    worker_id=self.worker_id,
                )
                if not claimed:
                    continue
                batch = primary.convert_secondary_items(claimed)
                logger.info(
                    "Secondary conversion | shop=%s claimed=%s batch=%s",
                    shop.shop_domain,
                    len(claimed),
                    batch.id if batch else None,
                )
        finally:
            db.close()

    async def _process_available_work(self) -> None:
        from app.services.ai_provider import AiProviderError, require_openai_provider
        from app.services.openai_batch_orchestrator import (
            OpenAIBatchOrchestratorError,
            primary_queue_uses_openai_batch,
        )

        try:
            require_openai_provider()
        except AiProviderError:
            logger.exception("AI provider blocks Primary Queue work")
            return

        try:
            use_batch = primary_queue_uses_openai_batch()
        except OpenAIBatchOrchestratorError:
            logger.exception("OpenAI Batch configuration error - Primary Queue work blocked")
            return

        if use_batch:
            await asyncio.to_thread(self._run_openai_batch_tick)
            # OpenAI mode still must reclaim RETRYING/QUEUED products (stale recovery,
            # completed-image rollup). Previously we returned early and left products
            # stuck in RETRYING forever after a lock-timeout race.
            await self._claim_and_process_due_products()
            return

        await self._claim_and_process_due_products()

    async def _claim_and_process_due_products(self) -> None:
        shop_ids = await asyncio.to_thread(self._shops_with_work)
        for shop_id in shop_ids:
            if self._stop.is_set():
                return
            for _ in range(settings.processing_batch_concurrency):
                product_id = await asyncio.to_thread(self._claim_product, shop_id)
                if not product_id:
                    break
                await asyncio.to_thread(self._process_product, product_id)

    def _run_openai_batch_tick(self) -> None:
        from app.services.openai_batch_orchestrator import OpenAIBatchOrchestrator

        db = SessionLocal()
        try:
            stats = OpenAIBatchOrchestrator(db).tick(worker_id=self.worker_id)
            if any(stats.values()):
                logger.info("OpenAI Batch tick | %s worker_id=%s", stats, self.worker_id)
        except Exception:
            logger.exception("OpenAI Batch tick failed | worker_id=%s", self.worker_id)
        finally:
            db.close()

    def _shops_with_work(self) -> list[UUID]:
        db = SessionLocal()
        try:
            rows = db.execute(
                select(distinct(BatchProduct.shop_id)).where(
                    BatchProduct.status.in_(
                        [BatchProductStatus.QUEUED, BatchProductStatus.RETRYING]
                    )
                )
            ).all()
            # Also include shops with pending secondary when auto sync will convert
            return [row[0] for row in rows]
        finally:
            db.close()

    def _claim_product(self, shop_id: UUID) -> UUID | None:
        db = SessionLocal()
        try:
            shop = db.get(Shop, shop_id)
            if not shop:
                return None
            product = PrimaryBatchService(db, shop).claim_next_batch_product(self.worker_id)
            return product.id if product else None
        finally:
            db.close()

    def _process_product(self, batch_product_id: UUID) -> None:
        db = SessionLocal()
        try:
            ImageProcessor(db).process_batch_product(batch_product_id, worker_id=self.worker_id)
        except Exception:
            logger.exception(
                "Batch product processing crashed | product_id=%s worker_id=%s",
                batch_product_id,
                self.worker_id,
            )
        finally:
            db.close()


processing_worker = ProcessingWorker()


async def kickoff_batch_processing(batch_id: UUID) -> None:
    """Process all queued products in a batch after manual creation."""
    from datetime import datetime, timezone

    from app.services.state_machine import BATCH_PRODUCT_TRANSITIONS, BATCH_TRANSITIONS, assert_transition

    db = SessionLocal()
    product_ids: list[UUID] = []
    try:
        batch = db.get(ProcessingBatch, batch_id)
        if batch is None:
            logger.warning("Manual batch kickoff skipped - batch missing | batch_id=%s", batch_id)
            return
        shop = db.get(Shop, batch.shop_id)
        if shop is None:
            logger.warning("Manual batch kickoff skipped - shop missing | batch_id=%s", batch_id)
            return

        if batch.status == BatchStatus.QUEUED:
            assert_transition("batch", BATCH_TRANSITIONS, batch.status, BatchStatus.PROCESSING)
            batch.status = BatchStatus.PROCESSING
            batch.started_at = datetime.now(timezone.utc)
            db.commit()

        # Claim QUEUED rows into PROCESSING before process_batch_product - that
        # path only accepts PROCESSING and would otherwise no-op and leave work idle.
        primary = PrimaryBatchService(db, shop)
        dialect = db.bind.dialect.name if db.bind is not None else ""
        while True:
            q = (
                db.query(BatchProduct)
                .filter(
                    BatchProduct.batch_id == batch_id,
                    BatchProduct.status == BatchProductStatus.QUEUED,
                )
                .order_by(BatchProduct.created_at.asc())
            )
            if dialect == "postgresql":
                q = q.with_for_update(skip_locked=True)
            else:
                q = q.with_for_update()
            claimed = q.first()
            if claimed is None:
                break
            now = datetime.now(timezone.utc)
            assert_transition(
                "batch_product",
                BATCH_PRODUCT_TRANSITIONS,
                claimed.status,
                BatchProductStatus.PROCESSING,
            )
            claimed.status = BatchProductStatus.PROCESSING
            claimed.locked_by = processing_worker.worker_id
            claimed.locked_at = now
            claimed.claimed_at = claimed.claimed_at or now
            claimed.started_at = claimed.started_at or now
            product_ids.append(claimed.id)
            primary.refresh_batch_counters(batch)
            db.commit()
    finally:
        db.close()

    sem = asyncio.Semaphore(settings.processing_batch_concurrency)

    async def _one(pid: UUID) -> None:
        async with sem:
            await asyncio.to_thread(processing_worker._process_product, pid)

    if product_ids:
        await asyncio.gather(*[_one(i) for i in product_ids], return_exceptions=True)
    logger.info("Manual batch kickoff finished | batch_id=%s products=%s", batch_id, len(product_ids))

"""
Thin compatibility wrappers for the Week 2 PrimaryBatchService.

Legacy worker/API code can continue importing BatchService while the
product-based batch pipeline is adopted incrementally.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import BatchProduct, BatchProductStatus, ProcessingBatch, Shop, TriggerType
from app.services.primary_batch import PrimaryBatchService

logger = logging.getLogger("app.services.batch")


class BatchService:
    def __init__(self, db: Session, shop: Shop | None = None) -> None:
        self.db = db
        self.shop = shop

    def _primary(self, shop_id: UUID) -> PrimaryBatchService:
        shop = self.shop if self.shop and self.shop.id == shop_id else self.db.get(Shop, shop_id)
        if shop is None:
            raise ValueError(f"Shop not found: {shop_id}")
        return PrimaryBatchService(self.db, shop)

    def claim_next_batch_product(self, *, shop_id: UUID, worker_id: str) -> BatchProduct | None:
        return self._primary(shop_id).claim_next_batch_product(worker_id)

    def refresh_batch_summary(self, batch_id: UUID) -> ProcessingBatch | None:
        batch = self.db.get(ProcessingBatch, batch_id)
        if not batch:
            return None
        return self._primary(batch.shop_id).refresh_batch_counters(batch)

    def refresh_batch_counters(self, batch: ProcessingBatch) -> ProcessingBatch:
        return self._primary(batch.shop_id).refresh_batch_counters(batch)

    def list_batches(
        self,
        *,
        shop_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ProcessingBatch], int]:
        shop = self.shop if self.shop and self.shop.id == shop_id else self.db.get(Shop, shop_id)
        if shop is None:
            return [], 0
        return PrimaryBatchService(self.db, shop).list_batches(page=page, page_size=page_size)

    def get_batch(self, *, shop_id: UUID, batch_id: UUID) -> ProcessingBatch | None:
        return self._primary(shop_id).get_batch(batch_id)

    def get_batch_products(self, *, shop_id: UUID, batch_id: UUID) -> list[BatchProduct]:
        return self._primary(shop_id).get_batch_products(batch_id)

    def get_batch_items(self, *, shop_id: UUID, batch_id: UUID) -> list[BatchProduct]:
        """Legacy alias — batch items are now batch products."""
        return self.get_batch_products(shop_id=shop_id, batch_id=batch_id)

    def claim_pending_batch(
        self,
        *,
        shop_id: UUID | None,
        trigger_type: TriggerType,
        worker_id: str,
        batch_size: int | None = None,
        started_by: str | None = None,
    ) -> ProcessingBatch | None:
        """
        Legacy auto-batch entry point.

        Claims the next available batch product for processing when a shop is specified.
        Returns a synthetic batch handle when work was claimed.
        """
        if shop_id is None:
            return None
        batch_product = self.claim_next_batch_product(shop_id=shop_id, worker_id=worker_id)
        if batch_product is None:
            return None
        batch = self.db.get(ProcessingBatch, batch_product.batch_id)
        if batch and batch.started_at is None:
            batch.started_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(batch)
        logger.info(
            "Legacy claim_pending_batch -> batch product | batch=%s product=%s worker=%s",
            batch_product.batch_id if batch else None,
            batch_product.id,
            worker_id,
        )
        return batch

    def mark_batch_product_complete(self, batch_product_id: UUID) -> BatchProduct | None:
        batch_product = self.db.get(BatchProduct, batch_product_id)
        if not batch_product:
            return None
        if batch_product.status == BatchProductStatus.PROCESSING:
            batch_product.status = BatchProductStatus.COMPLETED
            batch_product.completed_at = datetime.now(timezone.utc)
            batch_product.locked_by = None
            batch_product.locked_at = None
            batch = self.db.get(ProcessingBatch, batch_product.batch_id)
            if batch:
                self.refresh_batch_counters(batch)
            self.db.commit()
            self.db.refresh(batch_product)
        return batch_product

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Product,
    SecondaryQueueItem,
    SecondaryQueueStatus,
    Shop,
)
from app.services.state_machine import SECONDARY_TRANSITIONS, assert_transition

logger = logging.getLogger("app.services.secondary_queue")


class SecondaryQueueService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def upsert_from_webhook(
        self,
        product_gid: str,
        product_snapshot: dict,
        media_snapshot: list[dict],
        webhook_id: str,
        *,
        previous_status: str | None = None,
        new_status: str | None = None,
        is_status_only: bool = False,
        is_draft_transition: bool = False,
    ) -> SecondaryQueueItem | None:
        if is_draft_transition:
            logger.info(
                "Secondary queue skip draft transition | shop=%s product=%s",
                self.shop.id,
                product_gid,
            )
            return None
        if is_status_only:
            logger.info(
                "Secondary queue skip status-only | shop=%s product=%s",
                self.shop.id,
                product_gid,
            )
            return None

        now = datetime.now(timezone.utc)
        product = (
            self.db.query(Product)
            .filter(
                Product.shop_id == self.shop.id,
                Product.shopify_product_gid == product_gid,
            )
            .one_or_none()
        )

        existing = (
            self.db.query(SecondaryQueueItem)
            .filter(
                SecondaryQueueItem.shop_id == self.shop.id,
                SecondaryQueueItem.shopify_product_gid == product_gid,
                SecondaryQueueItem.status == SecondaryQueueStatus.PENDING,
            )
            .one_or_none()
        )

        if existing:
            existing.queue_revision += 1
            existing.webhook_count += 1
            existing.eligible_product_snapshot_json = product_snapshot
            existing.eligible_media_snapshot_json = media_snapshot
            existing.latest_eligible_webhook_id = webhook_id
            existing.last_queued_at = now
            existing.product_id = product.id if product else existing.product_id
            existing.skip_reason = None
            existing.failure_reason = None
            self.db.commit()
            self.db.refresh(existing)
            logger.info(
                "Secondary queue updated | shop=%s product=%s revision=%s",
                self.shop.id,
                product_gid,
                existing.queue_revision,
            )
            return existing

        item = SecondaryQueueItem(
            shop_id=self.shop.id,
            shopify_product_gid=product_gid,
            product_id=product.id if product else None,
            queue_revision=1,
            status=SecondaryQueueStatus.PENDING,
            eligible_product_snapshot_json=product_snapshot,
            eligible_media_snapshot_json=media_snapshot,
            first_queued_at=now,
            last_queued_at=now,
            latest_eligible_webhook_id=webhook_id,
            webhook_count=1,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        logger.info(
            "Secondary queue created | shop=%s product=%s item=%s",
            self.shop.id,
            product_gid,
            item.id,
        )
        return item

    def list_items(
        self,
        *,
        status: SecondaryQueueStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[SecondaryQueueItem], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        q = self.db.query(SecondaryQueueItem).filter(SecondaryQueueItem.shop_id == self.shop.id)
        if status is not None:
            q = q.filter(SecondaryQueueItem.status == status)
        total = q.count()
        items = (
            q.order_by(SecondaryQueueItem.first_queued_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_item(self, item_id: UUID) -> SecondaryQueueItem | None:
        return (
            self.db.query(SecondaryQueueItem)
            .filter(
                SecondaryQueueItem.id == item_id,
                SecondaryQueueItem.shop_id == self.shop.id,
            )
            .one_or_none()
        )

    def summary(self) -> dict[str, int]:
        rows = (
            self.db.query(SecondaryQueueItem.status, func.count(SecondaryQueueItem.id))
            .filter(SecondaryQueueItem.shop_id == self.shop.id)
            .group_by(SecondaryQueueItem.status)
            .all()
        )
        counts = {status.value: count for status, count in rows}
        pending = counts.get(SecondaryQueueStatus.PENDING.value, 0)
        return {
            "pending": pending,
            "claimed": counts.get(SecondaryQueueStatus.CLAIMED.value, 0),
            "converted": counts.get(SecondaryQueueStatus.CONVERTED.value, 0),
            "skipped": counts.get(SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA.value, 0),
            "failed": counts.get(SecondaryQueueStatus.FAILED_CONVERSION.value, 0),
            "total": sum(counts.values()),
        }

    def claim_pending_for_conversion(self, *, limit: int, worker_id: str) -> list[SecondaryQueueItem]:
        hard_cap = max(settings.auto_batch_claim_limit, 1)
        limit = min(max(limit, 1), hard_cap)
        now = datetime.now(timezone.utc)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""

        stmt = (
            select(SecondaryQueueItem)
            .where(
                SecondaryQueueItem.shop_id == self.shop.id,
                SecondaryQueueItem.status == SecondaryQueueStatus.PENDING,
            )
            .order_by(SecondaryQueueItem.first_queued_at.asc())
            .limit(limit)
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        else:
            stmt = stmt.with_for_update()

        items = list(self.db.execute(stmt).scalars().all())
        for item in items:
            assert_transition("secondary_queue", SECONDARY_TRANSITIONS, item.status, SecondaryQueueStatus.CLAIMED)
            item.status = SecondaryQueueStatus.CLAIMED
            item.claimed_at = now
            item.claimed_by = worker_id
            item.conversion_attempted_at = now

        if items:
            self.db.commit()
            for item in items:
                self.db.refresh(item)
            logger.info(
                "Secondary items claimed | shop=%s count=%s worker=%s",
                self.shop.id,
                len(items),
                worker_id,
            )
        return items

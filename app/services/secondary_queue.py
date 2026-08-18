from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ProcessingBaseline,
    Product,
    ProductMediaVersion,
    ProductPublishOperation,
    ProductRollbackOperation,
    PublishStatus,
    RollbackStatus,
    SecondaryQueueItem,
    SecondaryQueueStatus,
    Shop,
)
from app.services.delta import cdn_path_identity
from app.services.prompt_resolver import PromptResolverError, assert_product_prompts_ready
from app.services.state_machine import SECONDARY_TRANSITIONS, assert_transition

logger = logging.getLogger("app.services.secondary_queue")

_ACTIVE_PUBLISH_STATUSES = (PublishStatus.QUEUED, PublishStatus.PUBLISHING)
_ACTIVE_ROLLBACK_STATUSES = (RollbackStatus.QUEUED, RollbackStatus.ROLLING_BACK)
# Shopify often delivers products/update after rollback status is already ROLLED_BACK.
_ROLLBACK_ECHO_WINDOW = timedelta(minutes=15)


def _media_identity_keys(media_rows: list[Any] | None) -> set[str]:
    keys: set[str] = set()
    for row in media_rows or []:
        if not isinstance(row, dict):
            continue
        for field in ("media_gid", "file_gid", "shopify_media_gid", "shopify_file_gid"):
            value = row.get(field)
            if value:
                keys.add(str(value))
        path = cdn_path_identity(row.get("cdn_url"))
        if path:
            keys.add(path)
    return keys


class SecondaryQueueService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def _prompt_failure_reason(
        self,
        product: Product | None,
        product_snapshot: dict,
        product_gid: str,
    ) -> str | None:
        type_override = None
        if product is None or not (product.product_type or "").strip():
            type_override = product_snapshot.get("product_type") or product_snapshot.get("productType")
        label = (
            (product.title if product else None)
            or product_snapshot.get("title")
            or product_gid
        )
        try:
            assert_product_prompts_ready(
                self.db,
                self.shop,
                product,
                product_type_override=str(type_override) if type_override else None,
                product_label=str(label) if label else None,
            )
            return None
        except PromptResolverError as exc:
            return str(exc)[:2000]

    def _apply_prompt_gate(
        self,
        item: SecondaryQueueItem,
        product: Product | None,
        product_snapshot: dict,
        product_gid: str,
    ) -> None:
        reason = self._prompt_failure_reason(product, product_snapshot, product_gid)
        if reason is None:
            item.failure_reason = None
            return
        if item.status != SecondaryQueueStatus.FAILED_CONVERSION:
            assert_transition(
                "secondary_queue",
                SECONDARY_TRANSITIONS,
                item.status,
                SecondaryQueueStatus.FAILED_CONVERSION,
            )
            item.status = SecondaryQueueStatus.FAILED_CONVERSION
        item.failure_reason = reason
        item.skip_reason = None

    def _has_active_publish(self, product_gid: str) -> bool:
        row = (
            self.db.query(ProductPublishOperation.id)
            .filter(
                ProductPublishOperation.shop_id == self.shop.id,
                ProductPublishOperation.shopify_product_gid == product_gid,
                ProductPublishOperation.status.in_(_ACTIVE_PUBLISH_STATUSES),
            )
            .first()
        )
        return row is not None

    def _has_active_rollback(self, product_gid: str) -> bool:
        row = (
            self.db.query(ProductRollbackOperation.id)
            .filter(
                ProductRollbackOperation.shop_id == self.shop.id,
                ProductRollbackOperation.shopify_product_gid == product_gid,
                ProductRollbackOperation.status.in_(_ACTIVE_ROLLBACK_STATUSES),
            )
            .first()
        )
        return row is not None

    def _recent_completed_rollback(self, product_gid: str) -> ProductRollbackOperation | None:
        cutoff = datetime.now(timezone.utc) - _ROLLBACK_ECHO_WINDOW
        return (
            self.db.query(ProductRollbackOperation)
            .filter(
                ProductRollbackOperation.shop_id == self.shop.id,
                ProductRollbackOperation.shopify_product_gid == product_gid,
                ProductRollbackOperation.status == RollbackStatus.ROLLED_BACK,
                ProductRollbackOperation.completed_at.isnot(None),
                ProductRollbackOperation.completed_at >= cutoff,
            )
            .order_by(ProductRollbackOperation.completed_at.desc())
            .first()
        )

    def _is_rollback_webhook_echo(self, product_gid: str, media_snapshot: list[dict]) -> bool:
        """True when this webhook is Shopify catching up after our own Revert.

        Late products/update can arrive after status is ROLLED_BACK, sometimes still
        showing a mix of pre-revert and restored media. Those must not enter the
        Secondary Queue. A media row whose GID/CDN is not in the revert set is treated
        as a real merchant edit and still enqueues.
        """
        op = self._recent_completed_rollback(product_gid)
        if op is None:
            return False

        known = set()
        pre = op.pre_rollback_snapshot_json if isinstance(op.pre_rollback_snapshot_json, dict) else {}
        known |= _media_identity_keys(pre.get("media") if isinstance(pre.get("media"), list) else None)

        target = (
            self.db.query(ProductMediaVersion)
            .filter(
                ProductMediaVersion.id == op.target_version_id,
                ProductMediaVersion.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if target and isinstance(target.items_json, dict):
            known |= _media_identity_keys(
                target.items_json.get("media") if isinstance(target.items_json.get("media"), list) else None
            )

        product = (
            self.db.query(Product)
            .filter(
                Product.shop_id == self.shop.id,
                Product.shopify_product_gid == product_gid,
            )
            .one_or_none()
        )
        if product is not None:
            baseline = (
                self.db.query(ProcessingBaseline)
                .filter(
                    ProcessingBaseline.shop_id == self.shop.id,
                    ProcessingBaseline.product_id == product.id,
                )
                .one_or_none()
            )
            if baseline is not None:
                known |= _media_identity_keys(
                    baseline.media_snapshot_json if isinstance(baseline.media_snapshot_json, list) else None
                )

        if not media_snapshot:
            return True
        if not known:
            return True
        for row in media_snapshot:
            if not isinstance(row, dict):
                continue
            row_keys = _media_identity_keys([row])
            if not row_keys:
                continue
            if row_keys.isdisjoint(known):
                return False
        return True

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

        if self._has_active_publish(product_gid):
            logger.info(
                "Secondary queue skip during active publish | shop=%s product=%s webhook=%s",
                self.shop.id,
                product_gid,
                webhook_id,
            )
            return None

        if self._has_active_rollback(product_gid):
            logger.info(
                "Secondary queue skip during active rollback | shop=%s product=%s webhook=%s",
                self.shop.id,
                product_gid,
                webhook_id,
            )
            return None

        if self._is_rollback_webhook_echo(product_gid, media_snapshot):
            logger.info(
                "Secondary queue skip rollback echo | shop=%s product=%s webhook=%s",
                self.shop.id,
                product_gid,
                webhook_id,
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

        if existing is None:
            # Re-open a prior prompt/config failure when Shopify sends another update.
            existing = (
                self.db.query(SecondaryQueueItem)
                .filter(
                    SecondaryQueueItem.shop_id == self.shop.id,
                    SecondaryQueueItem.shopify_product_gid == product_gid,
                    SecondaryQueueItem.status == SecondaryQueueStatus.FAILED_CONVERSION,
                )
                .order_by(SecondaryQueueItem.updated_at.desc())
                .first()
            )
            if existing is not None:
                assert_transition(
                    "secondary_queue",
                    SECONDARY_TRANSITIONS,
                    existing.status,
                    SecondaryQueueStatus.PENDING,
                )
                existing.status = SecondaryQueueStatus.PENDING
                existing.claimed_at = None
                existing.claimed_by = None

        # Media delta vs ProcessingBaseline is evaluated at Primary conversion
        # (convert_secondary_items), not at Secondary Queue enqueue.

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
            self._apply_prompt_gate(existing, product, product_snapshot, product_gid)
            self.db.commit()
            self.db.refresh(existing)
            logger.info(
                "Secondary queue updated | shop=%s product=%s revision=%s status=%s",
                self.shop.id,
                product_gid,
                existing.queue_revision,
                existing.status.value,
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
        self.db.flush()
        self._apply_prompt_gate(item, product, product_snapshot, product_gid)
        self.db.commit()
        self.db.refresh(item)
        logger.info(
            "Secondary queue created | shop=%s product=%s item=%s status=%s",
            self.shop.id,
            product_gid,
            item.id,
            item.status.value,
        )
        return item

    def list_items(
        self,
        *,
        status: SecondaryQueueStatus | None = None,
        page: int = 1,
        page_size: int = 7,
    ) -> tuple[list[SecondaryQueueItem], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        q = self.db.query(SecondaryQueueItem).filter(SecondaryQueueItem.shop_id == self.shop.id)
        if status is not None:
            q = q.filter(SecondaryQueueItem.status == status)
        total = q.count()
        items = (
            q.order_by(
                SecondaryQueueItem.last_queued_at.desc(),
                SecondaryQueueItem.created_at.desc(),
            )
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

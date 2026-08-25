from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core.shop_resolver import create_shopify_graphql_client, get_shop_by_domain
from app.models import Product, Shop, ShopSettings, WebhookEvent, WebhookProcessingResult
from app.services.catalog_sync import CatalogSyncService
from app.services.primary_batch import PrimaryBatchService
from app.services.secondary_queue import SecondaryQueueService
from app.services.shopify_graphql import ShopifyGraphQLError
from app.services.snapshot import (
    media_snapshots_from_shopify,
    normalize_shopify_product_node,
    product_snapshot_from_shopify,
)

logger = logging.getLogger("app.services.webhook_intake")

_NUMERIC_RE = re.compile(r"^\d+$")
_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/\d+$")


def product_gid_from_webhook_payload(payload: dict[str, Any]) -> str | None:
    admin_gid = payload.get("admin_graphql_api_id")
    if isinstance(admin_gid, str) and admin_gid.strip():
        return admin_gid.strip()
    numeric_id = payload.get("id")
    if numeric_id is not None:
        text = str(numeric_id).strip()
        if _NUMERIC_RE.match(text):
            return f"gid://shopify/Product/{text}"
    return None


def normalize_webhook_topic(topic: str | None) -> str:
    """Normalize Shopify topic headers (products/create, PRODUCTS_CREATE, etc.)."""
    raw = (topic or "").strip()
    if not raw:
        return "products/update"
    lowered = raw.lower().replace(".", "/")
    if lowered in {"products/create", "products_create"}:
        return "products/create"
    if lowered in {"products/update", "products_update"}:
        return "products/update"
    return lowered


def normalize_status(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value).strip().upper() or None


def is_draft_transition(
    *,
    previous_status: str | None,
    new_status: str | None,
) -> bool:
    """True when the update transitions the product to DRAFT."""
    prev = normalize_status(previous_status)
    new = normalize_status(new_status)
    return new == "DRAFT" and prev != "DRAFT"


def detect_status_only(old_product: Product | None, new_payload: dict[str, Any]) -> bool:
    """
    Return True when only product status changed between stored product and webhook payload.

    Non-status product fields compared when present on the webhook payload.
    """
    if old_product is None:
        return False

    new_status = normalize_status(new_payload.get("status"))
    old_status = normalize_status(old_product.status)
    if new_status == old_status:
        return False

    comparisons: list[tuple[Any, Any]] = [
        (new_payload.get("title"), old_product.title),
        (new_payload.get("body_html") or new_payload.get("descriptionHtml"), old_product.description_html),
        (new_payload.get("handle"), old_product.handle),
        (new_payload.get("product_type") or new_payload.get("productType"), old_product.product_type),
        (new_payload.get("vendor"), old_product.vendor),
    ]

    tags = new_payload.get("tags")
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)
    comparisons.append((tags, old_product.tags))

    for new_val, old_val in comparisons:
        if new_val is None:
            continue
        if str(new_val).strip() != str(old_val or "").strip():
            return False

    images = new_payload.get("images")
    if images is not None and len(images) > 0:
        return False

    image = new_payload.get("image")
    if image is not None:
        return False

    return True


def _product_snapshot_from_webhook(payload: dict[str, Any], product_gid: str) -> dict[str, Any]:
    tags = payload.get("tags")
    if isinstance(tags, list):
        tags_str = ", ".join(str(t) for t in tags)
    else:
        tags_str = tags
    numeric_id = None
    if payload.get("id") is not None:
        numeric_id = str(payload.get("id"))
    return {
        "product_gid": product_gid,
        "numeric_id": numeric_id,
        "title": payload.get("title"),
        "description_html": payload.get("body_html"),
        "handle": payload.get("handle"),
        "status": payload.get("status"),
        "product_type": payload.get("product_type"),
        "vendor": payload.get("vendor"),
        "tags": tags_str,
        "updated_at": payload.get("updated_at"),
    }


def _media_snapshot_from_webhook(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        featured = payload.get("image")
        if isinstance(featured, dict):
            images = [featured]
        else:
            return None

    media_rows: list[dict[str, Any]] = []
    for position, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        admin_gid = img.get("admin_graphql_api_id")
        media_gid = admin_gid
        if not media_gid and img.get("id") is not None:
            media_gid = f"gid://shopify/MediaImage/{img.get('id')}"
        src = img.get("src") or img.get("url")
        if not src:
            continue
        media_rows.append(
            {
                "media_gid": media_gid,
                "file_gid": None,
                "cdn_url": src,
                "filename": src.split("?", 1)[0].rsplit("/", 1)[-1],
                "width": img.get("width"),
                "height": img.get("height"),
                "mime_type": None,
                "alt_text": img.get("alt"),
                "position": position,
                "is_primary": position == 0,
                "is_visible": True,
                "updated_at": img.get("updated_at"),
            }
        )
    return media_snapshots_from_shopify(media_rows) if media_rows else None


class WebhookIntakeService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue_products_update(
        self,
        shop_domain: str,
        webhook_id: str,
        topic: str,
        payload: dict[str, Any],
        raw_hash: str,
    ) -> WebhookEvent:
        """Persist and dedupe the webhook, then return. No Shopify/GraphQL work."""
        existing = self._get_by_webhook_id(webhook_id)
        if existing:
            logger.info("Duplicate webhook ignored | webhook_id=%s shop=%s", webhook_id, shop_domain)
            return existing

        topic = normalize_webhook_topic(topic)
        shop = get_shop_by_domain(self.db, shop_domain)
        product_gid = product_gid_from_webhook_payload(payload)
        result = WebhookProcessingResult.QUEUED
        error_summary = None
        stored_payload: dict[str, Any] | None = payload if isinstance(payload, dict) else None

        if shop is None:
            result = WebhookProcessingResult.IGNORED
            error_summary = "Shop not found"
            stored_payload = None
        elif not self._shop_auto_sync_enabled(shop.id):
            # Merchant turned Auto Sync off: ACK but do not queue GraphQL / Secondary work.
            result = WebhookProcessingResult.IGNORED
            error_summary = "Auto Sync disabled"
            stored_payload = None
            logger.info(
                "Webhook ignored; Auto Sync off | webhook_id=%s shop=%s product=%s topic=%s",
                webhook_id,
                shop_domain,
                product_gid,
                topic,
            )
        elif not product_gid or not _GID_PRODUCT_RE.match(product_gid):
            result = WebhookProcessingResult.FAILED
            error_summary = "Missing or invalid product GID in webhook payload"
            stored_payload = None

        event = WebhookEvent(
            shop_id=shop.id if shop else None,
            shopify_webhook_id=webhook_id,
            topic=topic,
            shopify_product_gid=product_gid,
            payload_hash=raw_hash,
            payload_json=stored_payload,
            processing_result=result,
            error_summary=error_summary,
        )
        self.db.add(event)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            duplicate = self._get_by_webhook_id(webhook_id)
            if duplicate:
                logger.info(
                    "Duplicate webhook ignored after constraint | webhook_id=%s shop=%s",
                    webhook_id,
                    shop_domain,
                )
                return duplicate
            raise
        self.db.refresh(event)

        # Create + update often arrive together for a new product. Keep only the
        # newest QUEUED row per shop+product so we do not GraphQL/Secondary twice.
        superseded = 0
        if event.processing_result == WebhookProcessingResult.QUEUED:
            superseded = self._supersede_older_queued_for_product(event)

        logger.info(
            "Webhook enqueued | webhook_id=%s shop=%s product=%s topic=%s result=%s superseded=%s",
            webhook_id,
            shop_domain,
            product_gid,
            topic,
            event.processing_result.value,
            superseded,
        )
        return event

    def _supersede_older_queued_for_product(self, newest: WebhookEvent) -> int:
        """Mark older QUEUED events for the same shop+product as IGNORED."""
        if newest.shop_id is None or not newest.shopify_product_gid:
            return 0
        older_rows = (
            self.db.query(WebhookEvent)
            .filter(
                WebhookEvent.shop_id == newest.shop_id,
                WebhookEvent.shopify_product_gid == newest.shopify_product_gid,
                WebhookEvent.processing_result == WebhookProcessingResult.QUEUED,
                WebhookEvent.id != newest.id,
            )
            .all()
        )
        if not older_rows:
            return 0
        for older in older_rows:
            older.processing_result = WebhookProcessingResult.IGNORED
            older.error_summary = (
                f"Superseded by later webhook {newest.shopify_webhook_id} ({newest.topic})"
            )
            older.claimed_by = None
            older.claimed_at = None
            older.payload_json = None
        self.db.commit()
        self.db.refresh(newest)
        return len(older_rows)
    def queue_metrics(self) -> dict[str, Any]:
        """Counts and lag for ops dashboards. Does not scan historical ACCEPTED rows."""
        now = datetime.now(timezone.utc)
        rows = (
            self.db.query(
                WebhookEvent.processing_result,
                func.count(WebhookEvent.id),
                func.min(WebhookEvent.received_at),
                func.coalesce(func.sum(WebhookEvent.attempt_count), 0),
            )
            .filter(
                WebhookEvent.processing_result.in_(
                    [
                        WebhookProcessingResult.QUEUED,
                        WebhookProcessingResult.PROCESSING,
                        WebhookProcessingResult.FAILED,
                    ]
                )
            )
            .group_by(WebhookEvent.processing_result)
            .all()
        )
        counts = {"queued": 0, "processing": 0, "failed": 0}
        oldest_queued: datetime | None = None
        retry_attempts = 0
        for result, count, oldest, attempts in rows:
            key = result.value if isinstance(result, WebhookProcessingResult) else str(result)
            lowered = key.lower()
            if lowered in counts:
                counts[lowered] = int(count or 0)
            retry_attempts += int(attempts or 0)
            if lowered == "queued" and oldest is not None:
                oldest_queued = oldest

        retrying = (
            self.db.query(func.count(WebhookEvent.id))
            .filter(
                WebhookEvent.processing_result == WebhookProcessingResult.QUEUED,
                WebhookEvent.attempt_count > 0,
            )
            .scalar()
            or 0
        )

        lag_seconds = 0
        if oldest_queued is not None:
            received = oldest_queued
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            lag_seconds = max(0, int((now - received).total_seconds()))

        depth = counts["queued"] + counts["processing"]
        alerts: list[str] = []
        warn_depth = max(1, int(settings.webhook_queue_warn_depth))
        warn_lag = max(1, int(settings.webhook_lag_warn_seconds))
        if depth >= warn_depth:
            alerts.append(f"webhook_backlog={depth} (warn>={warn_depth})")
        if lag_seconds >= warn_lag and counts["queued"] > 0:
            alerts.append(f"webhook_lag_seconds={lag_seconds} (warn>={warn_lag})")
        if counts["failed"] > 0:
            alerts.append(f"webhook_failed={counts['failed']}")

        if alerts:
            logger.warning("Webhook queue alert | %s", "; ".join(alerts))

        return {
            "queued": counts["queued"],
            "processing": counts["processing"],
            "failed": counts["failed"],
            "retrying": int(retrying),
            "retryAttempts": retry_attempts,
            "oldestQueuedLagSeconds": lag_seconds,
            "alerts": alerts,
        }

    def record_and_process_products_update(
        self,
        shop_domain: str,
        webhook_id: str,
        topic: str,
        payload: dict[str, Any],
        raw_hash: str,
    ) -> WebhookEvent:
        """Enqueue then process inline. Tests and one-off scripts use this path."""
        event = self.enqueue_products_update(shop_domain, webhook_id, topic, payload, raw_hash)
        if event.processing_result != WebhookProcessingResult.QUEUED:
            return event
        return self.process_event(event.id)

    def recover_stale_processing(self, *, worker_id: str) -> int:
        stale_seconds = max(30, int(settings.webhook_stale_lock_seconds))
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        rows = (
            self.db.query(WebhookEvent)
            .filter(
                WebhookEvent.processing_result == WebhookProcessingResult.PROCESSING,
                WebhookEvent.claimed_at.is_not(None),
                WebhookEvent.claimed_at < cutoff,
            )
            .all()
        )
        for event in rows:
            event.processing_result = WebhookProcessingResult.QUEUED
            event.claimed_by = None
            event.claimed_at = None
            event.error_summary = f"Stale PROCESSING lock recovered by {worker_id}"
            logger.warning(
                "Stale webhook lock recovered | webhook_id=%s product=%s worker=%s",
                event.shopify_webhook_id,
                event.shopify_product_gid,
                worker_id,
            )
        if rows:
            self.db.commit()
        return len(rows)

    def claim_queued(self, *, worker_id: str) -> list[UUID]:
        """Claim a bounded set of QUEUED product updates. Remaining rows wait in the table."""
        self.recover_stale_processing(worker_id=worker_id)

        global_cap = max(1, int(settings.webhook_process_concurrency))
        per_shop_cap = max(1, int(settings.webhook_process_concurrency_per_shop))
        claim_limit = max(1, int(settings.webhook_claim_limit))
        scan_limit = max(claim_limit, int(settings.webhook_claim_scan_limit))

        processing_rows = (
            self.db.query(WebhookEvent)
            .filter(WebhookEvent.processing_result == WebhookProcessingResult.PROCESSING)
            .all()
        )
        processing_by_shop: dict[UUID, int] = defaultdict(int)
        busy_products: set[tuple[UUID | None, str | None]] = set()
        for row in processing_rows:
            if row.shop_id is not None:
                processing_by_shop[row.shop_id] += 1
            busy_products.add((row.shop_id, row.shopify_product_gid))

        slots = max(0, min(claim_limit, global_cap - len(processing_rows)))
        if slots <= 0:
            return []

        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        stmt = (
            select(WebhookEvent)
            .where(WebhookEvent.processing_result == WebhookProcessingResult.QUEUED)
            .order_by(WebhookEvent.received_at.asc())
            .limit(scan_limit)
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        else:
            stmt = stmt.with_for_update()

        queued = list(self.db.execute(stmt).scalars().all())
        if not queued:
            return []

        groups: dict[tuple[UUID | None, str | None], list[WebhookEvent]] = {}
        group_order: list[tuple[UUID | None, str | None]] = []
        for event in queued:
            key = (event.shop_id, event.shopify_product_gid)
            if key not in groups:
                groups[key] = []
                group_order.append(key)
            groups[key].append(event)

        now = datetime.now(timezone.utc)
        claimed_ids: list[UUID] = []
        superseded = 0
        for key in group_order:
            if len(claimed_ids) >= slots:
                break
            shop_id, _product_gid = key
            if shop_id is not None and processing_by_shop[shop_id] >= per_shop_cap:
                continue
            if key in busy_products:
                continue
            group = groups[key]
            newest = max(group, key=lambda row: (row.received_at or now, str(row.id)))
            for older in group:
                if older.id == newest.id:
                    continue
                older.processing_result = WebhookProcessingResult.IGNORED
                older.error_summary = f"Superseded by later webhook {newest.shopify_webhook_id}"
                older.claimed_by = None
                older.claimed_at = None
                superseded += 1
            newest.processing_result = WebhookProcessingResult.PROCESSING
            newest.claimed_by = worker_id
            newest.claimed_at = now
            newest.attempt_count = int(newest.attempt_count or 0) + 1
            newest.error_summary = None
            claimed_ids.append(newest.id)
            busy_products.add(key)
            if shop_id is not None:
                processing_by_shop[shop_id] += 1

        if claimed_ids or superseded:
            self.db.commit()
            logger.info(
                "Webhook claim finished | claimed=%s superseded=%s worker=%s in_flight=%s",
                len(claimed_ids),
                superseded,
                worker_id,
                len(processing_rows) + len(claimed_ids),
            )
        return claimed_ids

    def process_event(self, event_id: UUID) -> WebhookEvent:
        event = self.db.get(WebhookEvent, event_id)
        if event is None:
            raise ValueError(f"Webhook event not found: {event_id}")
        if event.processing_result in {
            WebhookProcessingResult.ACCEPTED,
            WebhookProcessingResult.DUPLICATE,
            WebhookProcessingResult.IGNORED,
        }:
            return event

        payload = event.payload_json if isinstance(event.payload_json, dict) else None
        if payload is None:
            return self._finish_event(
                event,
                WebhookProcessingResult.FAILED,
                "Missing webhook payload for async processing",
            )

        shop = self.db.get(Shop, event.shop_id) if event.shop_id else None
        product_gid = event.shopify_product_gid
        if shop is None:
            return self._finish_event(event, WebhookProcessingResult.IGNORED, "Shop not found")
        if not self._shop_auto_sync_enabled(shop.id):
            return self._finish_event(event, WebhookProcessingResult.IGNORED, "Auto Sync disabled")
        if not product_gid or not _GID_PRODUCT_RE.match(product_gid):
            return self._finish_event(
                event,
                WebhookProcessingResult.FAILED,
                "Missing or invalid product GID in webhook payload",
            )

        try:
            self._process_product_update(shop, product_gid, payload, event.shopify_webhook_id, event)
        except Exception as exc:
            logger.exception(
                "Webhook processing failed | shop=%s webhook=%s product=%s",
                shop.shop_domain,
                event.shopify_webhook_id,
                product_gid,
            )
            self.db.rollback()
            event = self.db.get(WebhookEvent, event_id)
            if event is None:
                raise
            return self._finish_failed_or_retry(event, str(exc)[:2000])

        if event.processing_result == WebhookProcessingResult.FAILED:
            return self._finish_failed_or_retry(event, event.error_summary or "Webhook processing failed")

        event.processing_result = WebhookProcessingResult.ACCEPTED
        event.error_summary = None
        event.claimed_by = None
        event.claimed_at = None
        # Drop stale create/update twins still sitting in QUEUED for this product.
        self._supersede_stale_queued_after_accept(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def _supersede_stale_queued_after_accept(self, accepted: WebhookEvent) -> int:
        """Ignore older QUEUED twins after a successful create/update for the same product."""
        if accepted.shop_id is None or not accepted.shopify_product_gid:
            return 0
        accepted_at = accepted.received_at or datetime.now(timezone.utc)
        older_rows = (
            self.db.query(WebhookEvent)
            .filter(
                WebhookEvent.shop_id == accepted.shop_id,
                WebhookEvent.shopify_product_gid == accepted.shopify_product_gid,
                WebhookEvent.processing_result == WebhookProcessingResult.QUEUED,
                WebhookEvent.id != accepted.id,
            )
            .all()
        )
        superseded = 0
        for older in older_rows:
            older_at = older.received_at or accepted_at
            if older_at > accepted_at:
                # Newer event may carry later media/title changes — keep it.
                continue
            older.processing_result = WebhookProcessingResult.IGNORED
            older.error_summary = (
                f"Superseded by accepted webhook {accepted.shopify_webhook_id} ({accepted.topic})"
            )
            older.claimed_by = None
            older.claimed_at = None
            older.payload_json = None
            superseded += 1
        if superseded:
            logger.info(
                "Webhook post-accept supersede | product=%s accepted=%s superseded=%s",
                accepted.shopify_product_gid,
                accepted.shopify_webhook_id,
                superseded,
            )
        return superseded
    def _shop_auto_sync_enabled(self, shop_id: UUID) -> bool:
        row = (
            self.db.query(ShopSettings)
            .filter(ShopSettings.shop_id == shop_id)
            .one_or_none()
        )
        return bool(row and row.auto_sync_enabled)

    def _get_by_webhook_id(self, webhook_id: str) -> WebhookEvent | None:
        return (
            self.db.query(WebhookEvent)
            .filter(WebhookEvent.shopify_webhook_id == webhook_id)
            .one_or_none()
        )

    def _finish_event(
        self,
        event: WebhookEvent,
        result: WebhookProcessingResult,
        error_summary: str | None,
    ) -> WebhookEvent:
        event.processing_result = result
        event.error_summary = error_summary
        event.claimed_by = None
        event.claimed_at = None
        self.db.commit()
        self.db.refresh(event)
        return event

    def _finish_failed_or_retry(self, event: WebhookEvent, error_summary: str) -> WebhookEvent:
        max_attempts = max(1, int(settings.webhook_max_attempts))
        # Inline record_and_process leaves attempt_count at 0; fail immediately.
        # Worker claims increment attempt_count, so those may requeue until the cap.
        if 0 < int(event.attempt_count or 0) < max_attempts:
            event.processing_result = WebhookProcessingResult.QUEUED
            event.claimed_by = None
            event.claimed_at = None
            event.error_summary = error_summary
            self.db.commit()
            self.db.refresh(event)
            logger.warning(
                "Webhook requeued after failure | webhook_id=%s attempt=%s/%s",
                event.shopify_webhook_id,
                event.attempt_count,
                max_attempts,
            )
            return event
        return self._finish_event(event, WebhookProcessingResult.FAILED, error_summary)

    def _process_product_update(
        self,
        shop,
        product_gid: str,
        payload: dict[str, Any],
        webhook_id: str,
        event: WebhookEvent,
    ) -> None:
        catalog = CatalogSyncService(self.db, shop)
        old_product = (
            self.db.query(Product)
            .options(selectinload(Product.media))
            .filter(Product.shop_id == shop.id, Product.shopify_product_gid == product_gid)
            .one_or_none()
        )
        previous_status = old_product.status if old_product else None
        new_status = payload.get("status")

        status_only = detect_status_only(old_product, payload)
        draft_transition = is_draft_transition(previous_status=previous_status, new_status=new_status)

        # REST webhook images use ProductImage GIDs; catalog/baselines use MediaImage.
        # Always refresh media via GraphQL so Secondary Queue delta identity matches.
        #
        # Freeze an empty ProcessingBaseline from *pre-sync* catalog media before upsert.
        # Conversion used to seed empty baselines from the post-upsert catalog, which
        # already included newly added images - Secondary Queue then skipped with
        # "No eligible new or replaced images detected".
        #
        # Failures here must not fail the webhook (Shopify would retry 5xx). Worst case
        # we keep the old skip behavior until the next update.
        if old_product is not None:
            try:
                PrimaryBatchService(self.db, shop).seed_empty_baseline_from_product_media(old_product)
            except Exception:
                logger.exception(
                    "Baseline pre-seed failed; continuing webhook | shop=%s product=%s",
                    shop.id,
                    product_gid,
                )

        product_snapshot = _product_snapshot_from_webhook(payload, product_gid)
        media_snapshot: list[dict[str, Any]] = []
        graphql_ok = False

        try:
            client = create_shopify_graphql_client(self.db, shop)
            node = client.fetch_product_by_gid(product_gid)
            if node:
                normalized = normalize_shopify_product_node(node)
                # Prefer GraphQL product fields when available; keep REST for status-only compare above.
                product_snapshot = product_snapshot_from_shopify(normalized["product"])
                media_snapshot = media_snapshots_from_shopify(normalized["media"])
                catalog.upsert_product_from_shopify_node(node)
                graphql_ok = True
            elif old_product is None:
                event.processing_result = WebhookProcessingResult.FAILED
                event.error_summary = "Product not found via GraphQL"
                return
        except ShopifyGraphQLError as exc:
            logger.error(
                "GraphQL refresh failed during webhook | shop=%s product=%s error=%s",
                shop.id,
                product_gid,
                exc,
            )
            if old_product is None:
                event.processing_result = WebhookProcessingResult.FAILED
                event.error_summary = str(exc)[:2000]
                return

        if not graphql_ok:
            # Known product: apply REST product fields; fall back to REST media only if needed.
            if old_product:
                old_product.title = product_snapshot.get("title") or old_product.title
                old_product.description_html = product_snapshot.get("description_html") or old_product.description_html
                old_product.handle = product_snapshot.get("handle") or old_product.handle
                old_product.status = product_snapshot.get("status") or old_product.status
                old_product.product_type = product_snapshot.get("product_type") or old_product.product_type
                old_product.vendor = product_snapshot.get("vendor") or old_product.vendor
                old_product.tags = product_snapshot.get("tags") or old_product.tags
            rest_media = _media_snapshot_from_webhook(payload)
            media_snapshot = rest_media if rest_media is not None else []

        if old_product and new_status is not None:
            old_product.status = str(new_status)

        SecondaryQueueService(self.db, shop).upsert_from_webhook(
            product_gid,
            product_snapshot,
            media_snapshot,
            webhook_id,
            previous_status=previous_status,
            new_status=new_status,
            is_status_only=status_only,
            is_draft_transition=draft_transition,
        )

        event.processing_result = WebhookProcessingResult.ACCEPTED

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core.shop_resolver import ensure_shop_settings, create_shopify_graphql_client
from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ProcessingBaseline,
    ProcessingBatch,
    Product,
    SecondaryQueueItem,
    SecondaryQueueStatus,
    Shop,
    ShopSettings,
    TriggerType,
)
from app.services.catalog_sync import CatalogSyncService
from app.services.delta import compare_media_snapshots
from app.services.prompt_resolver import PromptResolverError, assert_product_prompts_ready
from app.services.shopify_graphql import ShopifyGraphQLError
from app.services.snapshot import media_snapshots_from_models, product_snapshot_from_model
from app.services.state_machine import (
    BATCH_PRODUCT_TRANSITIONS,
    BATCH_TRANSITIONS,
    SECONDARY_TRANSITIONS,
    assert_transition,
)

logger = logging.getLogger("app.services.primary_batch")

_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/\d+$")


class PrimaryBatchError(ValueError):
    pass


class PrimaryBatchService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop
        self.catalog_sync = CatalogSyncService(db, shop)

    def _validate_product_gids(self, product_gids: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for raw in product_gids:
            gid = raw.strip()
            if not _GID_PRODUCT_RE.match(gid):
                raise PrimaryBatchError(f"Invalid Shopify product GID: {raw}")
            if gid not in seen:
                seen.add(gid)
                unique.append(gid)
        if not unique:
            raise PrimaryBatchError("At least one product GID is required")
        limit = settings.manual_batch_product_limit
        if len(unique) > limit:
            raise PrimaryBatchError(f"Manual batch exceeds product limit ({limit})")
        return unique

    def _get_or_create_baseline(self, product: Product) -> ProcessingBaseline:
        baseline = (
            self.db.query(ProcessingBaseline)
            .filter(
                ProcessingBaseline.shop_id == self.shop.id,
                ProcessingBaseline.product_id == product.id,
            )
            .one_or_none()
        )
        if baseline is None:
            baseline = ProcessingBaseline(shop_id=self.shop.id, product_id=product.id)
            self.db.add(baseline)
            self.db.flush()
        return baseline

    def seed_empty_baseline_from_product_media(self, product: Product) -> ProcessingBaseline:
        """Freeze pre-update catalog media into an empty ProcessingBaseline.

        Webhook intake must call this *before* catalog upsert. Otherwise conversion
        seeds the empty baseline from the already-updated catalog and newly added
        images look like "no delta" and get SKIPPED_NO_ELIGIBLE_IMAGE_DELTA.

        Uses a SAVEPOINT so a unique-constraint race cannot abort the outer webhook
        transaction.
        """
        try:
            with self.db.begin_nested():
                baseline = self._get_or_create_baseline(product)
                if baseline.media_snapshot_json is not None:
                    return baseline
                catalog_media = media_snapshots_from_models(
                    [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
                )
                baseline.media_snapshot_json = catalog_media
                baseline.product_snapshot_json = (
                    baseline.product_snapshot_json or product_snapshot_from_model(product)
                )
                baseline.evaluated_at = datetime.now(timezone.utc)
                self.db.flush()
                logger.info(
                    "Seeded empty ProcessingBaseline from pre-sync catalog | shop=%s product=%s media=%s",
                    self.shop.id,
                    product.shopify_product_gid,
                    len(catalog_media),
                )
                return baseline
        except IntegrityError:
            # Concurrent path inserted the same uq_baseline_shop_product row.
            baseline = (
                self.db.query(ProcessingBaseline)
                .filter(
                    ProcessingBaseline.shop_id == self.shop.id,
                    ProcessingBaseline.product_id == product.id,
                )
                .one()
            )
            if baseline.media_snapshot_json is not None:
                return baseline
            catalog_media = media_snapshots_from_models(
                [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
            )
            baseline.media_snapshot_json = catalog_media
            baseline.product_snapshot_json = (
                baseline.product_snapshot_json or product_snapshot_from_model(product)
            )
            baseline.evaluated_at = datetime.now(timezone.utc)
            self.db.flush()
            return baseline

    def _advance_evaluated_baseline(
        self,
        product: Product,
        *,
        product_snapshot: dict,
        media_snapshot: list[dict],
    ) -> None:
        baseline = self._get_or_create_baseline(product)
        baseline.product_snapshot_json = product_snapshot
        baseline.media_snapshot_json = media_snapshot
        baseline.evaluated_at = datetime.now(timezone.utc)

    def _load_or_refresh_product(self, product_gid: str) -> Product | None:
        product = (
            self.db.query(Product)
            .options(selectinload(Product.media))
            .filter(
                Product.shop_id == self.shop.id,
                Product.shopify_product_gid == product_gid,
            )
            .one_or_none()
        )
        if product is not None:
            return product
        try:
            client = create_shopify_graphql_client(self.db, self.shop)
            node = client.fetch_product_by_gid(product_gid)
            if not node:
                return None
            return self.catalog_sync.upsert_product_from_shopify_node(node)
        except ShopifyGraphQLError as exc:
            logger.error("Product refresh failed | shop=%s gid=%s error=%s", self.shop.id, product_gid, exc)
            return None

    def _require_prompts_ready(
        self,
        product: Product | None,
        *,
        product_type_override: str | None = None,
        product_label: str | None = None,
    ) -> None:
        try:
            assert_product_prompts_ready(
                self.db,
                self.shop,
                product,
                product_type_override=product_type_override,
                product_label=product_label,
            )
        except PromptResolverError as exc:
            raise PrimaryBatchError(str(exc)) from exc

    def create_manual_batch(self, product_gids: list[str]) -> ProcessingBatch:
        gids = self._validate_product_gids(product_gids)
        shop_settings = ensure_shop_settings(self.db, self.shop)
        now = datetime.now(timezone.utc)

        # Validate every product (media + prompts) before creating the batch row.
        prepared: list[tuple[str, Product, list]] = []
        for gid in gids:
            product = self._load_or_refresh_product(gid)
            if product is None:
                raise PrimaryBatchError(f"Product not found: {gid}")

            visible_media = [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
            if not visible_media:
                raise PrimaryBatchError(f"Product has no visible media: {gid}")

            self._require_prompts_ready(product)
            prepared.append((gid, product, visible_media))

        batch = ProcessingBatch(
            shop_id=self.shop.id,
            trigger_type=TriggerType.MANUAL,
            status=BatchStatus.QUEUED,
            settings_snapshot_json={
                "batch_interval_minutes": shop_settings.batch_interval_minutes,
                "manual_batch_product_limit": settings.manual_batch_product_limit,
            },
        )
        self.db.add(batch)
        self.db.flush()

        image_count = 0
        for gid, product, visible_media in prepared:
            product_snapshot = product_snapshot_from_model(product)
            media_snapshot = media_snapshots_from_models(visible_media)
            # Publish conflict baseline must be the live media set at enqueue time.
            # ProcessingBaseline may be empty on first run (delta tracking only) — do not
            # copy its null media into baseline_snapshot_json or publish always conflicts.
            self._get_or_create_baseline(product)

            batch_product = BatchProduct(
                batch_id=batch.id,
                shop_id=self.shop.id,
                shopify_product_gid=gid,
                product_id=product.id,
                product_snapshot_json=product_snapshot,
                prompt_snapshot_json=None,
                baseline_snapshot_json={
                    "product": product_snapshot,
                    "media": media_snapshot,
                },
                status=BatchProductStatus.QUEUED,
                image_count=len(visible_media),
            )
            self.db.add(batch_product)
            self.db.flush()

            for media in visible_media:
                self.db.add(
                    BatchImage(
                        batch_product_id=batch_product.id,
                        shop_id=self.shop.id,
                        shopify_media_gid=media.shopify_media_gid,
                        shopify_file_gid=media.shopify_file_gid,
                        cdn_url=media.cdn_url or "",
                        original_filename=media.original_filename,
                        width=media.width,
                        height=media.height,
                        mime_type=media.mime_type,
                        source_fingerprint=media.content_fingerprint,
                        delta_type=DeltaType.INITIAL,
                        status=BatchImageStatus.QUEUED,
                    )
                )
                image_count += 1

        batch.product_count = len(gids)
        batch.image_count = image_count
        batch.pending_product_count = len(gids)
        batch.started_at = now
        self.db.commit()
        self.db.refresh(batch)
        logger.info(
            "Manual batch created | shop=%s batch=%s products=%s images=%s",
            self.shop.id,
            batch.id,
            batch.product_count,
            batch.image_count,
        )
        return batch

    def convert_secondary_items(self, items: list[SecondaryQueueItem]) -> ProcessingBatch | None:
        if not items:
            return None

        shop_settings = ensure_shop_settings(self.db, self.shop)
        now = datetime.now(timezone.utc)
        batch: ProcessingBatch | None = None
        batch_products_created = 0
        batch_images_created = 0

        for item in items:
            try:
                product = None
                if item.product_id:
                    product = (
                        self.db.query(Product)
                        .options(selectinload(Product.media))
                        .filter(Product.id == item.product_id)
                        .one_or_none()
                    )
                if product is None:
                    product = (
                        self.db.query(Product)
                        .options(selectinload(Product.media))
                        .filter(
                            Product.shop_id == self.shop.id,
                            Product.shopify_product_gid == item.shopify_product_gid,
                        )
                        .one_or_none()
                    )

                eligible_product = item.eligible_product_snapshot_json or {}
                eligible_media = item.eligible_media_snapshot_json or []

                baseline_media: list[dict] | None = None
                if product:
                    baseline = self._get_or_create_baseline(product)
                    # Only auto-seed when media_snapshot_json is None (never frozen).
                    # An explicit [] means "no prior media" (seeded at webhook before upsert)
                    # and must not be replaced by the post-webhook catalog, or new images
                    # would be hidden. Title-only with None still seeds from catalog so
                    # existing images are not treated as first-seen NEW.
                    if baseline.media_snapshot_json is None:
                        catalog_media = media_snapshots_from_models(
                            [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
                        )
                        if catalog_media:
                            baseline.media_snapshot_json = catalog_media
                            baseline.product_snapshot_json = (
                                baseline.product_snapshot_json
                                or product_snapshot_from_model(product)
                            )
                            baseline.evaluated_at = now
                            self.db.flush()
                    baseline_media = baseline.media_snapshot_json or []
                else:
                    baseline = None

                delta = compare_media_snapshots(eligible_media, baseline_media)

                if delta["skip_reason"]:
                    item.status = SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA
                    item.skip_reason = delta["skip_reason"]
                    if product:
                        self._advance_evaluated_baseline(
                            product,
                            product_snapshot=eligible_product,
                            media_snapshot=eligible_media,
                        )
                    self.db.commit()
                    continue

                type_override = None
                if product is None or not (product.product_type or "").strip():
                    type_override = eligible_product.get("product_type") or eligible_product.get(
                        "productType"
                    )
                label = (
                    (product.title if product else None)
                    or eligible_product.get("title")
                    or item.shopify_product_gid
                )
                try:
                    self._require_prompts_ready(
                        product,
                        product_type_override=str(type_override) if type_override else None,
                        product_label=str(label) if label else None,
                    )
                except PrimaryBatchError as exc:
                    assert_transition(
                        "secondary_queue",
                        SECONDARY_TRANSITIONS,
                        item.status,
                        SecondaryQueueStatus.FAILED_CONVERSION,
                    )
                    item.status = SecondaryQueueStatus.FAILED_CONVERSION
                    item.failure_reason = str(exc)[:2000]
                    item.skip_reason = None
                    self.db.commit()
                    continue

                if batch is None:
                    batch = ProcessingBatch(
                        shop_id=self.shop.id,
                        trigger_type=TriggerType.AUTOMATIC,
                        status=BatchStatus.QUEUED,
                        settings_snapshot_json={
                            "batch_interval_minutes": shop_settings.batch_interval_minutes,
                        },
                        started_at=now,
                    )
                    self.db.add(batch)
                    self.db.flush()

                delta_images = delta["new"] + delta["replaced"]

                batch_product = BatchProduct(
                    batch_id=batch.id,
                    shop_id=self.shop.id,
                    shopify_product_gid=item.shopify_product_gid,
                    product_id=product.id if product else None,
                    product_snapshot_json=eligible_product,
                    prompt_snapshot_json=None,
                    # Conflict check compares against media at process start (eligible),
                    # not prior ProcessingBaseline (used only for delta detection above).
                    baseline_snapshot_json={
                        "product": eligible_product,
                        "media": eligible_media,
                    },
                    status=BatchProductStatus.QUEUED,
                    image_count=len(delta_images),
                )
                self.db.add(batch_product)
                self.db.flush()

                for media in delta_images:
                    delta_type = DeltaType(media.get("delta_type", DeltaType.NEW.value))
                    self.db.add(
                        BatchImage(
                            batch_product_id=batch_product.id,
                            shop_id=self.shop.id,
                            shopify_media_gid=media.get("media_gid") or "",
                            shopify_file_gid=media.get("file_gid"),
                            cdn_url=media.get("cdn_url") or "",
                            original_filename=media.get("filename"),
                            width=media.get("width"),
                            height=media.get("height"),
                            mime_type=media.get("mime_type"),
                            source_fingerprint=media.get("fingerprint"),
                            delta_type=delta_type,
                            status=BatchImageStatus.QUEUED,
                        )
                    )
                    batch_images_created += 1

                item.status = SecondaryQueueStatus.CONVERTED
                item.converted_batch_id = batch.id
                item.skip_reason = None
                batch_products_created += 1
                self.db.commit()
            except Exception as exc:
                logger.exception(
                    "Secondary conversion failed | shop=%s item=%s",
                    self.shop.id,
                    item.id,
                )
                item.status = SecondaryQueueStatus.FAILED_CONVERSION
                item.failure_reason = str(exc)[:2000]
                self.db.commit()

        if batch is None:
            return None

        batch.product_count = batch_products_created
        batch.image_count = batch_images_created
        batch.pending_product_count = batch_products_created
        self.refresh_batch_counters(batch)
        self.db.commit()
        self.db.refresh(batch)
        logger.info(
            "Automatic batch converted | shop=%s batch=%s products=%s images=%s",
            self.shop.id,
            batch.id,
            batch.product_count,
            batch.image_count,
        )
        return batch

    def refresh_batch_counters(self, batch: ProcessingBatch) -> ProcessingBatch:
        # Session uses autoflush=False; flush so in-transaction status changes are
        # visible to the aggregate queries below. Without this, counters stay stale
        # (e.g. product COMPLETED while batch still shows Processing / completed=0).
        self.db.flush()

        rows = (
            self.db.query(BatchProduct.status, func.count(BatchProduct.id))
            .filter(BatchProduct.batch_id == batch.id)
            .group_by(BatchProduct.status)
            .all()
        )
        counts = {status: count for status, count in rows}
        batch.product_count = sum(counts.values())
        batch.pending_product_count = counts.get(BatchProductStatus.QUEUED, 0)
        batch.processing_product_count = counts.get(BatchProductStatus.PROCESSING, 0)
        batch.completed_product_count = counts.get(BatchProductStatus.COMPLETED, 0)
        batch.failed_product_count = counts.get(BatchProductStatus.FAILED, 0)
        batch.retrying_product_count = counts.get(BatchProductStatus.RETRYING, 0)

        image_count = (
            self.db.query(func.count(BatchImage.id))
            .join(BatchProduct, BatchImage.batch_product_id == BatchProduct.id)
            .filter(BatchProduct.batch_id == batch.id)
            .scalar()
            or 0
        )
        batch.image_count = int(image_count)

        active = (
            batch.pending_product_count
            + batch.processing_product_count
            + batch.retrying_product_count
        )
        if active > 0:
            batch.completed_at = None
            if batch.status in {
                BatchStatus.COMPLETED,
                BatchStatus.PARTIALLY_COMPLETED,
                BatchStatus.FAILED,
            }:
                assert_transition("batch", BATCH_TRANSITIONS, batch.status, BatchStatus.PROCESSING)
                batch.status = BatchStatus.PROCESSING
                if batch.started_at is None:
                    batch.started_at = datetime.now(timezone.utc)
            elif batch.status == BatchStatus.QUEUED and batch.processing_product_count > 0:
                assert_transition("batch", BATCH_TRANSITIONS, batch.status, BatchStatus.PROCESSING)
                batch.status = BatchStatus.PROCESSING
                if batch.started_at is None:
                    batch.started_at = datetime.now(timezone.utc)
        elif active == 0 and batch.product_count > 0:
            completed = batch.completed_product_count
            failed = batch.failed_product_count
            if completed == batch.product_count:
                new_status = BatchStatus.COMPLETED
            elif failed == batch.product_count:
                new_status = BatchStatus.FAILED
            elif completed > 0 and failed > 0:
                new_status = BatchStatus.PARTIALLY_COMPLETED
            elif completed > 0:
                new_status = BatchStatus.PARTIALLY_COMPLETED
            else:
                new_status = BatchStatus.FAILED
            became_terminal = batch.status not in {
                BatchStatus.COMPLETED,
                BatchStatus.PARTIALLY_COMPLETED,
                BatchStatus.FAILED,
                BatchStatus.CANCELLED,
            }
            if batch.status != new_status:
                assert_transition("batch", BATCH_TRANSITIONS, batch.status, new_status)
                batch.status = new_status
            batch.completed_at = datetime.now(timezone.utc)
            self.db.flush()
            if became_terminal:
                try:
                    from app.services.publish_trigger import PublishTriggerService

                    PublishTriggerService(self.db, self.shop).on_batch_terminal(batch, commit=False)
                except Exception:
                    logger.exception(
                        "Publish trigger after batch terminal failed | batch=%s",
                        batch.id,
                    )

        self.db.flush()
        return batch

    def claim_next_batch_product(self, worker_id: str) -> BatchProduct | None:
        now = datetime.now(timezone.utc)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""

        eligible = and_(
            BatchProduct.shop_id == self.shop.id,
            or_(
                BatchProduct.status == BatchProductStatus.QUEUED,
                and_(
                    BatchProduct.status == BatchProductStatus.RETRYING,
                    or_(
                        BatchProduct.next_retry_at.is_(None),
                        BatchProduct.next_retry_at <= now,
                    ),
                ),
            ),
        )

        stmt = (
            select(BatchProduct)
            .where(eligible)
            .order_by(BatchProduct.created_at.asc())
            .limit(1)
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        else:
            stmt = stmt.with_for_update()

        batch_product = self.db.execute(stmt).scalar_one_or_none()
        if batch_product is None:
            return None

        assert_transition(
            "batch_product",
            BATCH_PRODUCT_TRANSITIONS,
            batch_product.status,
            BatchProductStatus.PROCESSING,
        )
        batch_product.status = BatchProductStatus.PROCESSING
        batch_product.locked_by = worker_id
        batch_product.locked_at = now
        batch_product.claimed_at = batch_product.claimed_at or now
        batch_product.started_at = batch_product.started_at or now
        batch_product.next_retry_at = None

        batch = self.db.get(ProcessingBatch, batch_product.batch_id)
        if batch:
            self.refresh_batch_counters(batch)

        self.db.commit()
        self.db.refresh(batch_product)
        logger.info(
            "Batch product claimed | shop=%s product=%s batch=%s worker=%s",
            self.shop.id,
            batch_product.id,
            batch_product.batch_id,
            worker_id,
        )
        return batch_product

    def list_batches(
        self,
        *,
        status: BatchStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ProcessingBatch], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        q = self.db.query(ProcessingBatch).filter(ProcessingBatch.shop_id == self.shop.id)
        if status is not None:
            q = q.filter(ProcessingBatch.status == status)
        total = q.count()
        items = (
            q.order_by(ProcessingBatch.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_batch(self, batch_id: UUID) -> ProcessingBatch | None:
        return (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == batch_id, ProcessingBatch.shop_id == self.shop.id)
            .one_or_none()
        )

    def get_batch_products(self, batch_id: UUID) -> list[BatchProduct]:
        return (
            self.db.query(BatchProduct)
            .filter(BatchProduct.batch_id == batch_id, BatchProduct.shop_id == self.shop.id)
            .order_by(BatchProduct.created_at.asc())
            .all()
        )

    def get_batch_images(self, batch_id: UUID) -> list[BatchImage]:
        return (
            self.db.query(BatchImage)
            .join(BatchProduct, BatchImage.batch_product_id == BatchProduct.id)
            .filter(BatchProduct.batch_id == batch_id, BatchProduct.shop_id == self.shop.id)
            .order_by(BatchProduct.created_at.asc(), BatchImage.created_at.asc())
            .all()
        )

    def should_create_automatic_batch(self, shop_settings: ShopSettings) -> bool:
        """True when Auto Sync is on and the oldest pending Secondary Queue item
        has waited at least ``batch_interval_minutes`` (time-only trigger)."""
        if not shop_settings.auto_sync_enabled:
            return False

        oldest = (
            self.db.query(func.min(SecondaryQueueItem.first_queued_at))
            .filter(
                SecondaryQueueItem.shop_id == self.shop.id,
                SecondaryQueueItem.status == SecondaryQueueStatus.PENDING,
            )
            .scalar()
        )
        if oldest is None:
            return False

        interval = timedelta(minutes=shop_settings.batch_interval_minutes)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= oldest + interval

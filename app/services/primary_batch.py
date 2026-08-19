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

    def _refresh_product_from_shopify(self, product_gid: str) -> Product | None:
        """Fetch the latest product and media from Shopify and upsert into the catalog."""
        try:
            client = create_shopify_graphql_client(self.db, self.shop)
            node = client.fetch_product_by_gid(product_gid)
            if not node:
                return None
            return self.catalog_sync.upsert_product_from_shopify_node(node)
        except Exception as exc:
            logger.warning(
                "Product refresh failed | shop=%s gid=%s error=%s",
                self.shop.id,
                product_gid,
                exc,
            )
            return None

    def refresh_catalog_product(self, product_id: UUID) -> Product | None:
        product = (
            self.db.query(Product)
            .filter(Product.id == product_id, Product.shop_id == self.shop.id)
            .one_or_none()
        )
        if product is None:
            return None
        refreshed = self._refresh_product_from_shopify(product.shopify_product_gid)
        return refreshed or product

    def _load_or_refresh_product(
        self, product_gid: str, *, force_refresh: bool = False
    ) -> Product | None:
        if force_refresh:
            refreshed = self._refresh_product_from_shopify(product_gid)
            if refreshed is not None:
                return (
                    self.db.query(Product)
                    .options(selectinload(Product.media))
                    .filter(Product.id == refreshed.id)
                    .one()
                )
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
        return self._refresh_product_from_shopify(product_gid)

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

    def create_manual_batch(self, product_gids: list[str]) -> tuple[ProcessingBatch, list[str]]:
        gids = self._validate_product_gids(product_gids)
        shop_settings = ensure_shop_settings(self.db, self.shop)
        now = datetime.now(timezone.utc)

        # Validate products before creating the batch row. Skip products with no
        # live attached images instead of failing the whole selection.
        prepared: list[tuple[str, Product, list]] = []
        warnings: list[str] = []
        for gid in gids:
            product = self._load_or_refresh_product(gid, force_refresh=True)
            if product is None:
                raise PrimaryBatchError(f"Product not found: {gid}")

            visible_media = [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
            if not visible_media:
                label = product.title or gid
                warnings.append(
                    f"{label} has no images attached on Shopify and was skipped."
                )
                logger.warning(
                    "Manual batch skipped product with no attached media | shop=%s product=%s",
                    self.shop.id,
                    gid,
                )
                continue

            self._require_prompts_ready(product)
            prepared.append((gid, product, visible_media))

        if not prepared:
            raise PrimaryBatchError(
                "None of the selected products have images attached on Shopify."
            )

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
            # ProcessingBaseline may be empty on first run (delta tracking only) - do not
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

        batch.product_count = len(prepared)
        batch.image_count = image_count
        batch.pending_product_count = len(prepared)
        batch.started_at = now
        self.db.commit()
        self.db.refresh(batch)
        logger.info(
            "Manual batch created | shop=%s batch=%s products=%s images=%s skipped=%s",
            self.shop.id,
            batch.id,
            batch.product_count,
            batch.image_count,
            len(warnings),
        )
        return batch, warnings

    def create_selective_manual_batch(
        self,
        product_gid: str,
        media_gids: list[str],
        *,
        prompt_override: list[dict] | None = None,
        settings_extra: dict | None = None,
    ) -> tuple[ProcessingBatch, list[str]]:
        """Create a one-product manual batch for selected live images only.

        Baseline snapshot is the full live gallery so publish conflict checks stay
        accurate. Only the selected images are queued for AI work.

        Returns the batch and any warnings (e.g. selected media no longer on the product).
        """
        product = self._load_or_refresh_product(product_gid, force_refresh=True)
        if product is None:
            raise PrimaryBatchError(f"Product not found: {product_gid}")
        self._require_prompts_ready(product)

        wanted: list[str] = []
        seen: set[str] = set()
        for raw in media_gids:
            gid = str(raw or "").strip()
            if gid and gid not in seen:
                seen.add(gid)
                wanted.append(gid)
        if not wanted:
            raise PrimaryBatchError("Select at least one live image to reprocess.")

        visible = [m for m in product.media if m.is_visible and m.is_active and m.cdn_url]
        by_media = {m.shopify_media_gid: m for m in visible}
        by_file = {m.shopify_file_gid: m for m in visible if m.shopify_file_gid}
        selected = []
        missing: list[str] = []
        for gid in wanted:
            media = by_media.get(gid) or by_file.get(gid)
            if media is None:
                missing.append(gid)
            else:
                selected.append(media)
        warnings: list[str] = []
        if missing:
            if not selected:
                raise PrimaryBatchError(
                    "None of the selected images are on the live product. "
                    "Refresh the page and pick images currently attached in Shopify."
                )
            skipped = len(missing)
            warnings.append(
                f"{skipped} selected image{'s' if skipped != 1 else ''} "
                "not on the live product and skipped. "
                f"Enhancement continues with {len(selected)} remaining image"
                f"{'s' if len(selected) != 1 else ''}."
            )
            logger.warning(
                "Live reprocess skipped missing media | shop=%s product=%s skipped=%s remaining=%s",
                self.shop.id,
                product_gid,
                skipped,
                len(selected),
            )

        inflight = (
            self.db.query(BatchProduct)
            .filter(
                BatchProduct.shop_id == self.shop.id,
                BatchProduct.shopify_product_gid == product.shopify_product_gid,
                BatchProduct.status.in_(
                    (
                        BatchProductStatus.QUEUED,
                        BatchProductStatus.PROCESSING,
                        BatchProductStatus.RETRYING,
                    )
                ),
            )
            .first()
        )
        if inflight:
            raise PrimaryBatchError(
                "This product already has processing in progress. Wait for it to finish."
            )

        shop_settings = ensure_shop_settings(self.db, self.shop)
        now = datetime.now(timezone.utc)
        snap: dict = {
            "batch_interval_minutes": shop_settings.batch_interval_minutes,
            "manual_batch_product_limit": settings.manual_batch_product_limit,
        }
        if settings_extra:
            snap.update(settings_extra)

        batch = ProcessingBatch(
            shop_id=self.shop.id,
            trigger_type=TriggerType.MANUAL,
            status=BatchStatus.QUEUED,
            settings_snapshot_json=snap,
        )
        self.db.add(batch)
        self.db.flush()

        product_snapshot = product_snapshot_from_model(product)
        full_media_snapshot = media_snapshots_from_models(visible)
        batch_product = BatchProduct(
            batch_id=batch.id,
            shop_id=self.shop.id,
            shopify_product_gid=product.shopify_product_gid,
            product_id=product.id,
            product_snapshot_json=product_snapshot,
            prompt_snapshot_json=None,
            prompt_override_json=prompt_override,
            baseline_snapshot_json={
                "product": product_snapshot,
                "media": full_media_snapshot,
            },
            status=BatchProductStatus.QUEUED,
            image_count=len(selected),
        )
        self.db.add(batch_product)
        self.db.flush()

        for media in selected:
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
                    delta_type=DeltaType.REPLACED,
                    status=BatchImageStatus.QUEUED,
                )
            )

        batch.product_count = 1
        batch.image_count = len(selected)
        batch.pending_product_count = 1
        batch.started_at = now
        self._get_or_create_baseline(product)
        self.db.commit()
        self.db.refresh(batch)
        logger.info(
            "Live reprocess batch created | shop=%s batch=%s product=%s images=%s skipped=%s",
            self.shop.id,
            batch.id,
            product.shopify_product_gid,
            batch.image_count,
            len(missing),
        )
        return batch, warnings

    def _resolve_product_for_secondary(self, item: SecondaryQueueItem) -> Product | None:
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
        return product

    def _prepare_baseline_media(
        self, product: Product | None, *, now: datetime
    ) -> list[dict]:
        """Freeze / read ProcessingBaseline media used for delta detection."""
        if product is None:
            return []
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
                    baseline.product_snapshot_json or product_snapshot_from_model(product)
                )
                baseline.evaluated_at = now
                self.db.flush()
        return list(baseline.media_snapshot_json or [])

    def _inflight_media_snapshots(self, product_gid: str) -> list[dict]:
        """Media already represented by in-flight automatic Primary work for this product.

        Used so a new QUEUED generation does not re-queue images already covered by a
        PROCESSING/RETRYING BatchProduct.
        """
        images = (
            self.db.query(BatchImage)
            .join(BatchProduct, BatchImage.batch_product_id == BatchProduct.id)
            .join(ProcessingBatch, BatchProduct.batch_id == ProcessingBatch.id)
            .filter(
                BatchProduct.shop_id == self.shop.id,
                BatchProduct.shopify_product_gid == product_gid,
                BatchProduct.status.in_(
                    (BatchProductStatus.PROCESSING, BatchProductStatus.RETRYING)
                ),
                ProcessingBatch.trigger_type == TriggerType.AUTOMATIC,
            )
            .all()
        )
        snapshots: list[dict] = []
        for image in images:
            snapshots.append(
                {
                    "media_gid": image.shopify_media_gid,
                    "file_gid": image.shopify_file_gid,
                    "cdn_url": image.cdn_url,
                    "filename": image.original_filename,
                    "width": image.width,
                    "height": image.height,
                    "mime_type": image.mime_type,
                    "fingerprint": image.source_fingerprint,
                }
            )
        return snapshots

    def _effective_baseline_media(
        self, product: Product | None, product_gid: str, *, now: datetime
    ) -> list[dict]:
        baseline = self._prepare_baseline_media(product, now=now)
        inflight = self._inflight_media_snapshots(product_gid)
        if not inflight:
            return baseline
        seen: set[str] = set()
        merged: list[dict] = []
        for entry in baseline + inflight:
            gid = str(entry.get("media_gid") or entry.get("shopify_media_gid") or "")
            if gid and gid in seen:
                continue
            if gid:
                seen.add(gid)
            merged.append(entry)
        return merged

    def _find_queued_automatic_batch_product(
        self, product_gid: str, *, for_update: bool = True
    ) -> BatchProduct | None:
        """Find the single automatic QUEUED BatchProduct generation for this product.

        Batch may already be PROCESSING (other products started) - the product row is
        still refreshable until *this* BatchProduct leaves QUEUED. We never use that
        batch for capacity fills of *other* products once the batch is PROCESSING.
        """
        stmt = (
            select(BatchProduct)
            .join(ProcessingBatch, BatchProduct.batch_id == ProcessingBatch.id)
            .where(
                BatchProduct.shop_id == self.shop.id,
                BatchProduct.shopify_product_gid == product_gid,
                BatchProduct.status == BatchProductStatus.QUEUED,
                ProcessingBatch.trigger_type == TriggerType.AUTOMATIC,
            )
            .order_by(BatchProduct.created_at.asc(), BatchProduct.id.asc())
            .limit(1)
        )
        if for_update:
            dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
            if dialect == "postgresql":
                stmt = stmt.with_for_update(of=BatchProduct)
            else:
                stmt = stmt.with_for_update()
        return self.db.execute(stmt).scalars().first()

    def _batch_product_count(self, batch_id: UUID) -> int:
        return int(
            self.db.query(func.count(BatchProduct.id))
            .filter(BatchProduct.batch_id == batch_id)
            .scalar()
            or 0
        )

    def _lock_oldest_fillable_automatic_batch(
        self, *, capacity: int
    ) -> ProcessingBatch | None:
        """Oldest AUTOMATIC + QUEUED batch with free product capacity, row-locked."""
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        stmt = (
            select(ProcessingBatch)
            .where(
                ProcessingBatch.shop_id == self.shop.id,
                ProcessingBatch.trigger_type == TriggerType.AUTOMATIC,
                ProcessingBatch.status == BatchStatus.QUEUED,
            )
            .order_by(ProcessingBatch.created_at.asc(), ProcessingBatch.id.asc())
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(of=ProcessingBatch)
        else:
            stmt = stmt.with_for_update()

        candidates = list(self.db.execute(stmt).scalars().all())
        for batch in candidates:
            if self._batch_product_count(batch.id) < capacity:
                return batch
        return None

    def _create_automatic_batch(self, shop_settings: ShopSettings, *, now: datetime) -> ProcessingBatch:
        batch = ProcessingBatch(
            shop_id=self.shop.id,
            trigger_type=TriggerType.AUTOMATIC,
            status=BatchStatus.QUEUED,
            settings_snapshot_json={
                "batch_interval_minutes": shop_settings.batch_interval_minutes,
                "auto_batch_product_limit": settings.auto_batch_product_limit,
            },
            started_at=now,
        )
        self.db.add(batch)
        self.db.flush()
        logger.info(
            "Created new automatic batch because no eligible queued capacity remained | shop=%s batch=%s",
            self.shop.id,
            batch.id,
        )
        return batch

    def _allocate_automatic_batch_for_insert(
        self, shop_settings: ShopSettings, *, now: datetime, capacity: int
    ) -> ProcessingBatch:
        batch = self._lock_oldest_fillable_automatic_batch(capacity=capacity)
        if batch is not None:
            logger.info(
                "Added product to existing automatic queued batch | shop=%s batch=%s capacity=%s",
                self.shop.id,
                batch.id,
                capacity,
            )
            return batch
        return self._create_automatic_batch(shop_settings, now=now)

    def _replace_batch_images(self, batch_product: BatchProduct, delta_images: list[dict]) -> int:
        """Replace QUEUED BatchImage rows for a still-QUEUED BatchProduct."""
        if batch_product.status != BatchProductStatus.QUEUED:
            raise PrimaryBatchError("Cannot rebuild images for a non-QUEUED BatchProduct")
        (
            self.db.query(BatchImage)
            .filter(BatchImage.batch_product_id == batch_product.id)
            .delete(synchronize_session=False)
        )
        self.db.flush()
        created = 0
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
            created += 1
        batch_product.image_count = created
        self.db.flush()
        return created

    def _refresh_queued_batch_product(
        self,
        batch_product: BatchProduct,
        *,
        eligible_product: dict,
        eligible_media: list[dict],
        delta_images: list[dict],
        product: Product | None,
    ) -> ProcessingBatch:
        # Re-check immutability under lock: never mutate started work.
        self.db.refresh(batch_product)
        if batch_product.status != BatchProductStatus.QUEUED:
            raise PrimaryBatchError(
                f"BatchProduct {batch_product.id} is no longer QUEUED; refusing refresh"
            )
        batch = (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == batch_product.batch_id)
            .one()
        )
        if batch.trigger_type != TriggerType.AUTOMATIC:
            raise PrimaryBatchError(
                f"Batch {batch.id} is not automatic; refusing refresh"
            )

        batch_product.product_id = product.id if product else batch_product.product_id
        batch_product.product_snapshot_json = eligible_product
        batch_product.prompt_snapshot_json = None
        batch_product.baseline_snapshot_json = {
            "product": eligible_product,
            "media": eligible_media,
        }
        self._replace_batch_images(batch_product, delta_images)
        self.refresh_batch_counters(batch)
        logger.info(
            "Refreshed queued primary product with latest secondary snapshot | shop=%s product=%s batch=%s batch_product=%s images=%s",
            self.shop.id,
            batch_product.shopify_product_gid,
            batch.id,
            batch_product.id,
            len(delta_images),
        )
        return batch

    def _remove_queued_batch_product_no_delta(self, batch_product: BatchProduct) -> ProcessingBatch | None:
        """Drop a QUEUED product that no longer has an eligible delta after refresh."""
        batch = (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.id == batch_product.batch_id)
            .one()
        )
        self.db.delete(batch_product)
        self.db.flush()
        self.refresh_batch_counters(batch)
        if (
            batch.product_count == 0
            and batch.status == BatchStatus.QUEUED
            and batch.trigger_type == TriggerType.AUTOMATIC
        ):
            logger.info(
                "Deleted empty automatic queued batch after no-delta refresh | shop=%s batch=%s",
                self.shop.id,
                batch.id,
            )
            self.db.delete(batch)
            self.db.flush()
            return None
        return batch

    def _insert_queued_batch_product(
        self,
        batch: ProcessingBatch,
        *,
        product_gid: str,
        eligible_product: dict,
        eligible_media: list[dict],
        delta_images: list[dict],
        product: Product | None,
        capacity: int,
    ) -> BatchProduct:
        # Capacity re-check while batch row is locked by caller.
        if batch.status != BatchStatus.QUEUED or batch.trigger_type != TriggerType.AUTOMATIC:
            raise PrimaryBatchError("Cannot insert into a non-automatic QUEUED batch")
        if self._batch_product_count(batch.id) >= capacity:
            raise PrimaryBatchError(f"Automatic batch {batch.id} is at capacity ({capacity})")

        batch_product = BatchProduct(
            batch_id=batch.id,
            shop_id=self.shop.id,
            shopify_product_gid=product_gid,
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
        self._replace_batch_images(batch_product, delta_images)
        self.refresh_batch_counters(batch)
        return batch_product

    def _mark_secondary_converted(
        self, item: SecondaryQueueItem, batch: ProcessingBatch | None
    ) -> None:
        assert_transition(
            "secondary_queue",
            SECONDARY_TRANSITIONS,
            item.status,
            SecondaryQueueStatus.CONVERTED,
        )
        item.status = SecondaryQueueStatus.CONVERTED
        item.converted_batch_id = batch.id if batch else None
        item.skip_reason = None
        item.failure_reason = None

    def _mark_secondary_skipped_no_delta(
        self,
        item: SecondaryQueueItem,
        *,
        product: Product | None,
        eligible_product: dict,
        eligible_media: list[dict],
        skip_reason: str,
    ) -> None:
        assert_transition(
            "secondary_queue",
            SECONDARY_TRANSITIONS,
            item.status,
            SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA,
        )
        item.status = SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA
        item.skip_reason = skip_reason
        if product:
            self._advance_evaluated_baseline(
                product,
                product_snapshot=eligible_product,
                media_snapshot=eligible_media,
            )

    def convert_secondary_items(self, items: list[SecondaryQueueItem]) -> ProcessingBatch | None:
        """Convert claimed Secondary Queue items into automatic Primary work.

        Rules:
        - Refresh an existing automatic QUEUED BatchProduct for the same product (no new slot).
        - Otherwise insert into the oldest automatic QUEUED batch with free capacity.
        - Never fill MANUAL or PROCESSING batches.
        - Exclude in-flight PROCESSING/RETRYING media from the new delta.
        """
        if not items:
            return None

        shop_settings = ensure_shop_settings(self.db, self.shop)
        now = datetime.now(timezone.utc)
        capacity = max(int(settings.auto_batch_product_limit), 1)
        touched_batches: dict[UUID, ProcessingBatch] = {}
        first_batch: ProcessingBatch | None = None

        for item in items:
            try:
                product = self._resolve_product_for_secondary(item)
                eligible_product = item.eligible_product_snapshot_json or {}
                eligible_media = item.eligible_media_snapshot_json or []
                product_gid = item.shopify_product_gid

                effective_baseline = self._effective_baseline_media(
                    product, product_gid, now=now
                )
                delta = compare_media_snapshots(eligible_media, effective_baseline)
                delta_images = delta["new"] + delta["replaced"]

                type_override = None
                if product is None or not (product.product_type or "").strip():
                    type_override = eligible_product.get("product_type") or eligible_product.get(
                        "productType"
                    )
                label = (
                    (product.title if product else None)
                    or eligible_product.get("title")
                    or product_gid
                )

                queued_bp = self._find_queued_automatic_batch_product(product_gid, for_update=True)

                if queued_bp is not None:
                    # Prompt gate only when we still have work to keep/refresh.
                    if not delta["skip_reason"]:
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

                    if delta["skip_reason"]:
                        removed_batch = self._remove_queued_batch_product_no_delta(queued_bp)
                        self._mark_secondary_skipped_no_delta(
                            item,
                            product=product,
                            eligible_product=eligible_product,
                            eligible_media=eligible_media,
                            skip_reason=delta["skip_reason"],
                        )
                        if removed_batch is not None:
                            touched_batches[removed_batch.id] = removed_batch
                        self.db.commit()
                        continue

                    batch = self._refresh_queued_batch_product(
                        queued_bp,
                        eligible_product=eligible_product,
                        eligible_media=eligible_media,
                        delta_images=delta_images,
                        product=product,
                    )
                    self._mark_secondary_converted(item, batch)
                    touched_batches[batch.id] = batch
                    if first_batch is None:
                        first_batch = batch
                    self.db.commit()
                    continue

                if delta["skip_reason"]:
                    self._mark_secondary_skipped_no_delta(
                        item,
                        product=product,
                        eligible_product=eligible_product,
                        eligible_media=eligible_media,
                        skip_reason=delta["skip_reason"],
                    )
                    self.db.commit()
                    continue

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

                processing_exists = (
                    self.db.query(BatchProduct.id)
                    .join(ProcessingBatch, BatchProduct.batch_id == ProcessingBatch.id)
                    .filter(
                        BatchProduct.shop_id == self.shop.id,
                        BatchProduct.shopify_product_gid == product_gid,
                        BatchProduct.status.in_(
                            (BatchProductStatus.PROCESSING, BatchProductStatus.RETRYING)
                        ),
                        ProcessingBatch.trigger_type == TriggerType.AUTOMATIC,
                    )
                    .first()
                    is not None
                )
                if processing_exists:
                    logger.info(
                        "Existing product processing; created new queued generation | shop=%s product=%s",
                        self.shop.id,
                        product_gid,
                    )

                # Insert with savepoint so a unique-QUEUED race can fall back to refresh.
                batch: ProcessingBatch | None = None
                try:
                    with self.db.begin_nested():
                        batch = self._allocate_automatic_batch_for_insert(
                            shop_settings, now=now, capacity=capacity
                        )
                        # Re-lock capacity path: if allocate created new batch while another
                        # fillable batch appeared, prefer fillable oldest under lock again.
                        if self._batch_product_count(batch.id) >= capacity:
                            batch = self._allocate_automatic_batch_for_insert(
                                shop_settings, now=now, capacity=capacity
                            )
                        self._insert_queued_batch_product(
                            batch,
                            product_gid=product_gid,
                            eligible_product=eligible_product,
                            eligible_media=eligible_media,
                            delta_images=delta_images,
                            product=product,
                            capacity=capacity,
                        )
                except IntegrityError:
                    # Concurrent converter created the QUEUED generation first.
                    self.db.expire_all()
                    raced = self._find_queued_automatic_batch_product(
                        product_gid, for_update=True
                    )
                    if raced is None:
                        raise
                    logger.info(
                        "Concurrent queued product insert raced; refreshing winner | shop=%s product=%s",
                        self.shop.id,
                        product_gid,
                    )
                    batch = self._refresh_queued_batch_product(
                        raced,
                        eligible_product=eligible_product,
                        eligible_media=eligible_media,
                        delta_images=delta_images,
                        product=product,
                    )
                except PrimaryBatchError:
                    # Capacity race: retry allocate once more outside the failed path.
                    batch = self._allocate_automatic_batch_for_insert(
                        shop_settings, now=now, capacity=capacity
                    )
                    self._insert_queued_batch_product(
                        batch,
                        product_gid=product_gid,
                        eligible_product=eligible_product,
                        eligible_media=eligible_media,
                        delta_images=delta_images,
                        product=product,
                        capacity=capacity,
                    )

                assert batch is not None
                self._mark_secondary_converted(item, batch)
                touched_batches[batch.id] = batch
                if first_batch is None:
                    first_batch = batch
                self.db.commit()
            except Exception as exc:
                logger.exception(
                    "Secondary conversion failed | shop=%s item=%s",
                    self.shop.id,
                    item.id,
                )
                try:
                    self.db.rollback()
                except Exception:
                    logger.exception("Rollback after conversion failure failed")
                # Re-bind item after rollback if needed
                item = self.db.merge(item)
                try:
                    if item.status in {
                        SecondaryQueueStatus.CLAIMED,
                        SecondaryQueueStatus.PENDING,
                    }:
                        assert_transition(
                            "secondary_queue",
                            SECONDARY_TRANSITIONS,
                            item.status,
                            SecondaryQueueStatus.FAILED_CONVERSION,
                        )
                        item.status = SecondaryQueueStatus.FAILED_CONVERSION
                        item.failure_reason = str(exc)[:2000]
                        self.db.commit()
                except Exception:
                    logger.exception(
                        "Failed to mark secondary item FAILED_CONVERSION | item=%s", item.id
                    )
                    self.db.rollback()

        for batch in touched_batches.values():
            # Row may have been deleted (empty after no-delta refresh).
            still = (
                self.db.query(ProcessingBatch)
                .filter(ProcessingBatch.id == batch.id)
                .one_or_none()
            )
            if still is None:
                continue
            self.refresh_batch_counters(still)
        if touched_batches:
            self.db.commit()

        if first_batch is not None:
            first_batch = (
                self.db.query(ProcessingBatch)
                .filter(ProcessingBatch.id == first_batch.id)
                .one_or_none()
            )
        if first_batch is not None:
            self.db.refresh(first_batch)
            logger.info(
                "Automatic batch conversion complete | shop=%s primary_batch=%s touched=%s",
                self.shop.id,
                first_batch.id,
                len(touched_batches),
            )
        return first_batch

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

        try:
            from app.services.publish_trigger import PublishTriggerService

            PublishTriggerService(self.db, self.shop).maybe_auto_publish_completed_products(
                batch, commit=False
            )
        except Exception:
            logger.exception(
                "Auto-publish of completed products failed | batch=%s",
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
        page_size: int = 7,
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
        has waited at least ``batch_interval_minutes`` (time-only trigger).

        ``0`` means no wait: any PENDING item is eligible on the next worker tick.
        """
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

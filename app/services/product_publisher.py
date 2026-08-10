"""Product-level Shopify publish workflow (validate → upload → attach → verify → detach)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.shop_resolver import create_shopify_graphql_client
from app.models import BatchProduct, ProductPublishOperation, PublishStatus, Shop
from app.services.output_storage import get_output_storage
from app.services.publish_compensation import PublishCompensationError, PublishCompensationService
from app.services.publish_conflict import compare_publish_snapshots, heal_empty_publish_baseline
from app.services.publish_snapshot import (
    normalize_publish_snapshot,
    snapshot_from_baseline,
    snapshot_hash,
)
from app.services.shopify_file_upload import (
    PublishUploadError,
    ShopifyFileUploadService,
    poll_reorder_job,
    validate_png_file,
)
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError

logger = logging.getLogger("app.services.product_publisher")


class ProductPublisherError(RuntimeError):
    def __init__(self, code: str, message: str, *, conflict: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.conflict = conflict


class ProductPublisher:
    def __init__(self, db: Session, shop: Shop, client: ShopifyGraphQLClient | None = None) -> None:
        self.db = db
        self.shop = shop
        if client is not None:
            self.client = client
        else:
            try:
                self.client = create_shopify_graphql_client(db, shop)
            except RuntimeError as exc:
                raise ProductPublisherError("SHOPIFY_SCOPE_MISSING", str(exc)) from exc
        self.uploader = ShopifyFileUploadService(self.client)
        self.compensation = PublishCompensationService(self.client)

    def run(self, operation_id: UUID) -> ProductPublishOperation:
        op = (
            self.db.query(ProductPublishOperation)
            .filter(
                ProductPublishOperation.id == operation_id,
                ProductPublishOperation.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if not op:
            raise ProductPublisherError("PUBLISH_LOCKED", "Publish operation not found")

        product = (
            self.db.query(BatchProduct)
            .filter(BatchProduct.id == op.batch_product_id, BatchProduct.shop_id == self.shop.id)
            .one_or_none()
        )
        if not product:
            return self._fail(op, "PUBLISH_PRODUCT_NOT_PROCESSED", "Batch product missing")

        attached_new = False
        detached_old = False
        new_file_gids: list[str] = []
        original_snapshot: dict[str, Any] | None = None

        try:
            self._set_stage(op, product, PublishStatus.PUBLISHING, "VALIDATING")
            assets = list(op.assets_json or [])
            if not assets:
                raise ProductPublisherError("PUBLISH_OUTPUT_MISSING", "No publish assets on operation")

            storage = get_output_storage()
            for asset in assets:
                if asset.get("shopify_file_gid") and (asset.get("upload_status") or "").upper() == "READY":
                    continue
                key = asset.get("processed_output_key")
                if not key:
                    if asset.get("shopify_file_gid"):
                        continue
                    raise ProductPublisherError("PUBLISH_OUTPUT_MISSING", "Missing processed output key")
                path = storage.resolve_path(key)
                validate_png_file(path)

            self._set_stage(op, product, PublishStatus.PUBLISHING, "CHECKING_CONFLICT")
            baseline = snapshot_from_baseline(op.baseline_snapshot_json or product.baseline_snapshot_json)
            live_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            if not live_raw:
                raise ProductPublisherError("PUBLISH_BASELINE_MISSING", "Live Shopify product not found")
            live = normalize_publish_snapshot(live_raw)
            op.pre_publish_snapshot_json = live
            original_snapshot = live
            baseline, healed = heal_empty_publish_baseline(baseline, live, assets)
            if healed:
                op.baseline_snapshot_json = baseline
                product.baseline_snapshot_json = {
                    **(product.baseline_snapshot_json or {}),
                    "media": baseline.get("media") or [],
                    "product": (product.baseline_snapshot_json or {}).get("product"),
                    "product_gid": baseline.get("product_gid"),
                    "featured_media_gid": baseline.get("featured_media_gid"),
                    "variants": baseline.get("variants") or [],
                }
                self.db.flush()
            conflict = compare_publish_snapshots(baseline, live)
            if conflict.get("hasConflict"):
                op.conflict_details = conflict
                op.status = PublishStatus.PUBLISH_CONFLICT
                op.current_stage = "PUBLISH_CONFLICT"
                op.completed_at = datetime.now(timezone.utc)
                op.last_error_code = "PUBLISH_CONFLICT"
                op.last_error_message = "Shopify product media changed during processing"
                product.publish_status = PublishStatus.PUBLISH_CONFLICT
                self.db.commit()
                return op

            # Upload (or verify READY) PNGs
            self._set_stage(op, product, PublishStatus.PUBLISHING, "UPLOADING")
            for asset in assets:
                existing_gid = asset.get("shopify_file_gid")
                if existing_gid and (asset.get("upload_status") or "").upper() == "READY":
                    verified = self.uploader.poll_until_ready(existing_gid)
                    asset["shopify_file_gid"] = verified["file_gid"]
                    asset["shopify_file_status"] = verified["file_status"]
                    asset["shopify_cdn_url"] = verified.get("cdn_url") or asset.get("shopify_cdn_url")
                    asset["upload_status"] = "READY"
                    op.assets_json = assets
                    self.db.flush()
                    continue

                key = asset.get("processed_output_key")
                if not key:
                    raise ProductPublisherError(
                        "PUBLISH_OUTPUT_MISSING",
                        "Missing local output and no READY Shopify file for asset",
                    )
                path = storage.resolve_path(key)
                result = self.uploader.upload_png(
                    path=path,
                    filename=asset.get("processed_filename") or "image.png",
                    existing_file_gid=existing_gid,
                )
                asset["shopify_file_gid"] = result["file_gid"]
                asset["shopify_file_status"] = result["file_status"]
                asset["shopify_cdn_url"] = result.get("cdn_url")
                asset["upload_status"] = "READY"
                op.assets_json = assets
                self.db.flush()

            new_file_gids = [a["shopify_file_gid"] for a in assets if a.get("shopify_file_gid")]
            self._set_stage(op, product, PublishStatus.PUBLISHING, "WAITING_FOR_SHOPIFY")
            # Already READY from uploader; second conflict check before association
            live_raw2 = self.client.get_product_media_snapshot(op.shopify_product_gid)
            if not live_raw2:
                raise ProductPublisherError("SHOPIFY_VERIFICATION_FAILED", "Product disappeared before attach")
            live2 = normalize_publish_snapshot(live_raw2)
            if snapshot_hash(live2) != snapshot_hash(live):
                conflict2 = compare_publish_snapshots(baseline, live2)
                # Compare against pre-publish live snapshot for late edits
                late = compare_publish_snapshots(live, live2)
                if late.get("hasConflict"):
                    op.conflict_details = {**conflict2, "lateConflict": late}
                    op.status = PublishStatus.PUBLISH_CONFLICT
                    op.current_stage = "PUBLISH_CONFLICT"
                    op.completed_at = datetime.now(timezone.utc)
                    op.last_error_code = "PUBLISH_CONFLICT"
                    op.last_error_message = "Shopify product media changed during upload"
                    product.publish_status = PublishStatus.PUBLISH_CONFLICT
                    self.db.commit()
                    return op

            # Attach all new files
            self._set_stage(op, product, PublishStatus.PUBLISHING, "ATTACHING")
            self.client.add_file_product_references(
                file_gids=new_file_gids,
                product_gid=op.shopify_product_gid,
            )
            attached_new = True
            for asset in assets:
                asset["association_status"] = "ATTACHED"
            op.assets_json = assets
            self.db.flush()

            after_attach_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            after_attach = normalize_publish_snapshot(after_attach_raw or {})
            file_to_media = {m["media_gid"]: m["media_gid"] for m in (after_attach.get("media") or [])}
            for m in after_attach.get("media") or []:
                if m.get("file_gid"):
                    file_to_media[m["file_gid"]] = m["media_gid"]
            # Newly created MediaImage GIDs equal file GIDs in unified API.
            for asset in assets:
                gid = asset.get("shopify_file_gid")
                media_gid = file_to_media.get(gid) or gid
                asset["shopify_media_gid"] = media_gid
            op.assets_json = assets
            self.db.flush()

            # Alt text
            self._set_stage(op, product, PublishStatus.PUBLISHING, "UPDATING_ALT")
            for asset in assets:
                self.client.update_file_alt_text(
                    file_gid=asset["shopify_file_gid"],
                    alt=asset.get("target_alt_text"),
                )

            # Variants: map old source media → new media
            self._set_stage(op, product, PublishStatus.PUBLISHING, "UPDATING_VARIANTS")
            source_to_new = {
                a["source_media_gid"]: a.get("shopify_media_gid") or a.get("shopify_file_gid")
                for a in assets
                if a.get("source_media_gid")
            }
            variant_inputs: list[dict[str, Any]] = []
            for v in live.get("variants") or []:
                old_media = v.get("media_gid")
                new_media = source_to_new.get(old_media) if old_media else None
                if v.get("variant_gid") and new_media:
                    variant_inputs.append({"id": v["variant_gid"], "mediaId": new_media})
            if variant_inputs:
                self.client.associate_media_to_variants(
                    product_gid=op.shopify_product_gid,
                    variants=variant_inputs,
                )

            def _asset_target_position(asset: dict[str, Any]) -> int:
                # Prefer original gallery slot (delta-safe). Fall back to enumerate index.
                for key in ("source_position", "target_position"):
                    raw = asset.get(key)
                    if raw is None:
                        continue
                    try:
                        return int(raw)
                    except (TypeError, ValueError):
                        continue
                return 0

            # Place each generated image at its source position (keep untouched images).
            self._set_stage(op, product, PublishStatus.PUBLISHING, "REORDERING")
            ordered_new = sorted(assets, key=_asset_target_position)
            moves = []
            for asset in ordered_new:
                mid = asset.get("shopify_media_gid") or asset.get("shopify_file_gid")
                if mid:
                    moves.append({"id": mid, "newPosition": str(_asset_target_position(asset))})
            if moves:
                job = self.client.reorder_product_media(product_gid=op.shopify_product_gid, moves=moves)
                poll_reorder_job(self.client, job.get("id"))

            self._set_stage(op, product, PublishStatus.PUBLISHING, "VERIFYING_NEW_SET")
            verify_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            verify = normalize_publish_snapshot(verify_raw or {})
            verify_ids = {m["media_gid"] for m in (verify.get("media") or [])}
            for asset in assets:
                mid = asset.get("shopify_media_gid") or asset.get("shopify_file_gid")
                if mid not in verify_ids and asset.get("shopify_file_gid") not in verify_ids:
                    raise ProductPublisherError(
                        "SHOPIFY_VERIFICATION_FAILED",
                        f"New media missing after attach: {mid}",
                    )

            # Detach only the sources we replaced — never wipe the rest of the gallery.
            # Delta/auto batches process NEW/REPLACED images only; other live images must stay.
            self._set_stage(op, product, PublishStatus.PUBLISHING, "DETACHING_OLD_SET")
            live_by_media = {
                str(m.get("media_gid")): m
                for m in (live.get("media") or [])
                if isinstance(m, dict) and m.get("media_gid")
            }
            live_by_file = {
                str(m.get("file_gid") or m.get("media_gid")): m
                for m in (live.get("media") or [])
                if isinstance(m, dict) and (m.get("file_gid") or m.get("media_gid"))
            }
            new_set = set(new_file_gids)
            source_ids = {
                str(a.get("source_media_gid") or a.get("source_file_gid"))
                for a in assets
                if a.get("source_media_gid") or a.get("source_file_gid")
            }
            old_only: list[str] = []
            seen_detach: set[str] = set()
            for source_id in source_ids:
                row = live_by_media.get(source_id) or live_by_file.get(source_id)
                if row is None:
                    # Source may already be gone from live; still try detaching the id itself.
                    candidate = source_id
                else:
                    candidate = str(row.get("file_gid") or row.get("media_gid") or source_id)
                if not candidate or candidate in new_set or candidate in seen_detach:
                    continue
                seen_detach.add(candidate)
                old_only.append(candidate)
            if old_only:
                self.client.remove_file_product_references(
                    file_gids=old_only,
                    product_gid=op.shopify_product_gid,
                )
            detached_old = True

            # Final reorder of generated images into their target slots
            self._set_stage(op, product, PublishStatus.PUBLISHING, "REORDERING")
            final_moves = []
            for asset in ordered_new:
                mid = asset.get("shopify_media_gid") or asset.get("shopify_file_gid")
                if mid:
                    final_moves.append({"id": mid, "newPosition": str(_asset_target_position(asset))})
            if final_moves:
                job = self.client.reorder_product_media(product_gid=op.shopify_product_gid, moves=final_moves)
                poll_reorder_job(self.client, job.get("id"))

            self._set_stage(op, product, PublishStatus.PUBLISHING, "VERIFYING_FINAL_SET")
            final_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            final = normalize_publish_snapshot(final_raw or {})
            final_ids = {m["media_gid"] for m in (final.get("media") or [])}
            expected_new = {a.get("shopify_media_gid") or a.get("shopify_file_gid") for a in assets}
            if not expected_new.issubset(final_ids | {a.get("shopify_file_gid") for a in assets}):
                # Accept if file GIDs match media list
                final_files = {m.get("file_gid") or m.get("media_gid") for m in (final.get("media") or [])}
                if not set(new_file_gids).issubset(final_files):
                    raise ProductPublisherError(
                        "SHOPIFY_VERIFICATION_FAILED",
                        "Final product media does not match published set",
                    )
            # Only the replaced sources must be gone — untouched gallery images stay.
            leftover_sources = source_ids & final_ids - {x for x in expected_new if x}
            if leftover_sources:
                # Also treat file-gid overlap as still attached
                final_files = {m.get("file_gid") or m.get("media_gid") for m in (final.get("media") or [])}
                still = {s for s in leftover_sources if s in final_ids or s in final_files}
                if still:
                    raise ProductPublisherError(
                        "SHOPIFY_VERIFICATION_FAILED",
                        f"Replaced source media still associated after detach: {sorted(still)[:3]}",
                    )

            now = datetime.now(timezone.utc)
            op.status = PublishStatus.PUBLISHED
            op.current_stage = "PUBLISHED"
            op.completed_at = now
            op.published_at = now
            op.last_error_code = None
            op.last_error_message = None
            product.publish_status = PublishStatus.PUBLISHED

            try:
                from app.services.image_versions import ImageVersionsService
                from app.services.media_versions import MediaVersionsService

                published = MediaVersionsService(self.db, self.shop).record_publish_success(
                    batch_product=product,
                    publish_op=op,
                    pre_publish_snapshot=original_snapshot,
                    final_snapshot=final,
                )
                if published is not None:
                    media_items = list((published.items_json or {}).get("media") or [])
                    ImageVersionsService(self.db, self.shop).mark_published_for_product_version(
                        product_id=published.product_id,
                        product_media_version_id=published.id,
                        media_items=media_items,
                        actor_type="publish",
                    )
            except Exception:
                logger.exception(
                    "Failed to record media versions after publish | op=%s product=%s",
                    op.id,
                    op.shopify_product_gid,
                )

            self.db.commit()
            logger.info(
                "Publish succeeded | op=%s product=%s files=%s",
                op.id,
                op.shopify_product_gid,
                len(new_file_gids),
            )
            return op

        except ProductPublisherError as exc:
            if attached_new or detached_old:
                return self._compensate_and_fail(op, product, original_snapshot, new_file_gids, detached_old, exc)
            return self._fail(op, exc.code, str(exc), conflict=exc.conflict)
        except PublishUploadError as exc:
            return self._fail(op, exc.code, str(exc))
        except ShopifyGraphQLError as exc:
            if attached_new or detached_old:
                return self._compensate_and_fail(
                    op,
                    product,
                    original_snapshot,
                    new_file_gids,
                    detached_old,
                    ProductPublisherError("SHOPIFY_FILE_ASSOCIATION_FAILED", str(exc)),
                )
            code = "PUBLISH_RATE_LIMITED" if exc.retryable else "SHOPIFY_FILE_ASSOCIATION_FAILED"
            return self._fail(op, code, str(exc))
        except PublishCompensationError as exc:
            return self._fail_restore(op, product, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected publish failure | op=%s", operation_id)
            if attached_new or detached_old:
                return self._compensate_and_fail(
                    op,
                    product,
                    original_snapshot,
                    new_file_gids,
                    detached_old,
                    ProductPublisherError("PUBLISH_COMPENSATION_FAILED", str(exc)),
                )
            return self._fail(op, "PUBLISH_NETWORK_ERROR", str(exc))

    def _compensate_and_fail(
        self,
        op: ProductPublishOperation,
        product: BatchProduct,
        original_snapshot: dict[str, Any] | None,
        new_file_gids: list[str],
        detached_old: bool,
        exc: ProductPublisherError,
    ) -> ProductPublishOperation:
        self._set_stage(op, product, PublishStatus.PUBLISHING, "RESTORING_ORIGINAL")
        try:
            if original_snapshot is None:
                raise PublishCompensationError("PUBLISH_RESTORE_FAILED", "No original snapshot for restore")
            if detached_old:
                self.compensation.restore_original(
                    product_gid=op.shopify_product_gid,
                    original_snapshot=original_snapshot,
                    new_file_gids=new_file_gids,
                )
            else:
                self.compensation.detach_new_only(
                    product_gid=op.shopify_product_gid,
                    new_file_gids=new_file_gids,
                )
            return self._fail(op, exc.code, str(exc))
        except (PublishCompensationError, ShopifyGraphQLError) as restore_exc:
            code = getattr(restore_exc, "code", "PUBLISH_RESTORE_FAILED")
            return self._fail_restore(op, product, code, str(restore_exc))

    def _set_stage(
        self,
        op: ProductPublishOperation,
        product: BatchProduct,
        status: PublishStatus,
        stage: str,
    ) -> None:
        op.status = status
        op.current_stage = stage
        product.publish_status = status
        if op.started_at is None:
            op.started_at = datetime.now(timezone.utc)
        self.db.flush()

    def _fail(
        self,
        op: ProductPublishOperation,
        code: str,
        message: str,
        *,
        conflict: dict | None = None,
    ) -> ProductPublishOperation:
        op.status = PublishStatus.PUBLISH_FAILED
        op.current_stage = "PUBLISH_FAILED"
        op.last_error_code = code
        op.last_error_message = message[:2000]
        op.completed_at = datetime.now(timezone.utc)
        if conflict:
            op.conflict_details = conflict
        product = (
            self.db.query(BatchProduct)
            .filter(BatchProduct.id == op.batch_product_id)
            .one_or_none()
        )
        if product:
            product.publish_status = PublishStatus.PUBLISH_FAILED
        self.db.commit()
        return op

    def _fail_restore(
        self,
        op: ProductPublishOperation,
        product: BatchProduct,
        code: str,
        message: str,
    ) -> ProductPublishOperation:
        op.status = PublishStatus.RESTORE_FAILED
        op.current_stage = "RESTORE_FAILED"
        op.last_error_code = code
        op.last_error_message = message[:2000]
        op.completed_at = datetime.now(timezone.utc)
        product.publish_status = PublishStatus.RESTORE_FAILED
        self.db.commit()
        return op

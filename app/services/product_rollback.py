"""Full product-level media rollback to a historical complete version."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.shop_resolver import create_shopify_graphql_client
from app.models import (
    ProductMediaVersion,
    ProductRollbackOperation,
    RollbackStatus,
    Shop,
)
from app.services.media_versions import MediaVersionsService, product_has_active_media_op
from app.services.publish_compensation import PublishCompensationError, PublishCompensationService
from app.services.publish_conflict import compare_publish_snapshots
from app.services.publish_snapshot import normalize_publish_snapshot
from app.services.shopify_file_upload import poll_reorder_job
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError

logger = logging.getLogger("app.services.product_rollback")

RETRYABLE_ROLLBACK_STATUSES = {
    RollbackStatus.ROLLBACK_FAILED,
    RollbackStatus.ROLLBACK_CONFLICT,
    RollbackStatus.RESTORE_FAILED,
}


class RollbackError(RuntimeError):
    def __init__(self, code: str, message: str, *, conflict: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.conflict = conflict


def build_rollback_idempotency_key(
    *,
    shop_id: UUID,
    shopify_product_gid: str,
    from_version_id: UUID,
    target_version_id: UUID,
) -> str:
    raw = f"{shop_id}:{shopify_product_gid}:{from_version_id}:{target_version_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cdn_identity(url: str | None) -> str | None:
    """Stable CDN key: path only (strip host/query — MediaImage GIDs rematerialize, ?v= changes)."""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        path = (parsed.path or "").rstrip("/").lower()
        return path or None
    except Exception:
        path = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
        return path or None


def _file_keys(snapshot: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for m in snapshot.get("media") or []:
        if m.get("file_gid"):
            keys.add(m["file_gid"])
        if m.get("media_gid"):
            keys.add(m["media_gid"])
    return keys


def _is_distinct_file_gid(file_gid: str | None, media_gid: str | None) -> bool:
    """True when file_gid is a real File identity, not the MediaImage fallback copy."""
    if not file_gid:
        return False
    if media_gid and file_gid == media_gid:
        return False
    # Unified Files API often returns MediaImage GIDs for files; treat as non-distinct.
    if str(file_gid).startswith("gid://shopify/MediaImage/"):
        return False
    return True


def _media_identity(m: dict[str, Any]) -> str | None:
    """Best-effort stable identity across detach/reattach rematerialization."""
    if not isinstance(m, dict):
        return None
    media_gid = m.get("media_gid")
    file_gid = m.get("file_gid")
    if _is_distinct_file_gid(file_gid, media_gid):
        return f"file:{file_gid}"
    cdn = _cdn_identity(m.get("cdn_url"))
    if cdn:
        return f"cdn:{cdn}"
    if media_gid:
        return f"media:{media_gid}"
    if file_gid:
        return f"file:{file_gid}"
    return None


def _identity_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in snapshot.get("media") or []:
        key = _media_identity(m)
        if key and key not in out:
            out[key] = m
    return out


def _gid_to_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for m in snapshot.get("media") or []:
        key = _media_identity(m)
        if not key:
            continue
        for gid in (m.get("media_gid"), m.get("file_gid")):
            if gid:
                mapping[str(gid)] = key
    return mapping


def _ordered_identities(snapshot: dict[str, Any]) -> list[str]:
    rows = sorted(snapshot.get("media") or [], key=lambda x: x.get("position") or 0)
    return [key for m in rows if (key := _media_identity(m))]


def _compare_by_files(expected: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Conflict check resilient to MediaImage GID rematerialization after attach/detach.

    Snapshot `file_gid` often equals `media_gid` (GraphQL has no separate File id), so
    GID-only fallbacks false-conflict when Shopify rematerializes MediaImage nodes for
    the same CDN file. Prefer distinct File GIDs, then CDN path, then media GID.
    """
    media_conflict = compare_publish_snapshots(expected, live)
    if not media_conflict.get("hasConflict"):
        return media_conflict

    exp_map = _identity_index(expected)
    live_map = _identity_index(live)
    exp_ids = set(exp_map)
    live_ids = set(live_map)
    added = sorted(live_ids - exp_ids)
    removed = sorted(exp_ids - live_ids)
    membership_changed = bool(added or removed)

    exp_order = _ordered_identities(expected)
    live_order = _ordered_identities(live)
    shared_exp = [i for i in exp_order if i in live_ids]
    shared_live = [i for i in live_order if i in exp_ids]
    order_changed = shared_exp != shared_live

    alt_changes: list[dict[str, Any]] = []
    for key in sorted(exp_ids & live_ids):
        b_alt = exp_map[key].get("alt_text") or ""
        l_alt = live_map[key].get("alt_text") or ""
        if b_alt != l_alt:
            alt_changes.append({"identity": key, "baseline": b_alt, "live": l_alt})

    exp_gids = _gid_to_identity(expected)
    live_gids = _gid_to_identity(live)
    featured_baseline = expected.get("featured_media_gid")
    featured_live = live.get("featured_media_gid")
    featured_b_id = exp_gids.get(str(featured_baseline)) if featured_baseline else None
    featured_l_id = live_gids.get(str(featured_live)) if featured_live else None
    featured_changed = bool(featured_b_id and featured_l_id and featured_b_id != featured_l_id)

    exp_variants = {
        v["variant_gid"]: v.get("media_gid")
        for v in (expected.get("variants") or [])
        if isinstance(v, dict) and v.get("variant_gid")
    }
    live_variants = {
        v["variant_gid"]: v.get("media_gid")
        for v in (live.get("variants") or [])
        if isinstance(v, dict) and v.get("variant_gid")
    }
    variant_changes: list[dict[str, Any]] = []
    for vgid in sorted(set(exp_variants) | set(live_variants)):
        b_media = exp_variants.get(vgid)
        l_media = live_variants.get(vgid)
        b_id = exp_gids.get(str(b_media)) if b_media else None
        l_id = live_gids.get(str(l_media)) if l_media else None
        if b_id != l_id:
            variant_changes.append(
                {
                    "variant_gid": vgid,
                    "baseline_identity": b_id,
                    "live_identity": l_id,
                    "baseline_media_gid": b_media,
                    "live_media_gid": l_media,
                }
            )

    has_conflict = bool(
        membership_changed or order_changed or alt_changes or featured_changed or variant_changes
    )
    return {
        "hasConflict": has_conflict,
        "membershipChanged": membership_changed,
        "addedMediaIds": added,
        "removedMediaIds": removed,
        "orderChanged": order_changed,
        "altChanges": alt_changes,
        "featuredMediaChanged": featured_changed,
        "featuredBaseline": featured_baseline,
        "featuredLive": featured_live,
        "variantChanges": variant_changes,
        "reprocessRequired": membership_changed,
    }


def _enrich_media_cdn_from_file_nodes(
    media: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Copy CDN URLs from Shopify Files nodes onto snapshot media rows."""
    enriched: list[dict[str, Any]] = []
    changed = False
    for m in media:
        if not isinstance(m, dict):
            continue
        row = dict(m)
        key = row.get("file_gid") or row.get("media_gid")
        node = by_id.get(key or "") or {}
        image_node = node.get("image") or {}
        live_cdn = image_node.get("url")
        if live_cdn and row.get("cdn_url") != live_cdn:
            row["cdn_url"] = live_cdn
            changed = True
        if not row.get("file_gid") and key:
            row["file_gid"] = key
            changed = True
        enriched.append(row)
    return enriched, changed


class ProductRollbackService:
    def __init__(self, db: Session, shop: Shop, client: ShopifyGraphQLClient | None = None) -> None:
        self.db = db
        self.shop = shop
        self.versions = MediaVersionsService(db, shop)
        if client is not None:
            self.client = client
        else:
            try:
                self.client = create_shopify_graphql_client(db, shop)
            except RuntimeError as exc:
                raise RollbackError("SHOPIFY_SCOPE_MISSING", str(exc)) from exc
        self.compensation = PublishCompensationService(self.client)

    def enqueue(
        self,
        *,
        product_id: UUID,
        target_version_id: UUID,
        confirm: bool,
    ) -> dict[str, Any]:
        if not confirm:
            raise RollbackError("ROLLBACK_CONFIRM_REQUIRED", "Explicit confirmation is required")

        product = self.versions.get_product(product_id)
        target = self.versions.get_version(product_id, target_version_id)
        active = self.versions.active_version(product_id)
        if not active:
            raise RollbackError("VERSION_NOT_FOUND", "No active version for this product")
        if target.id == active.id or target.is_active:
            raise RollbackError("VERSION_ALREADY_ACTIVE", "Target version is already active")
        if not target.rollback_eligible:
            raise RollbackError(
                "VERSION_NOT_ROLLBACK_ELIGIBLE",
                target.unavailable_reason or "Target version is not rollback eligible",
            )
        items = target.items_json or {}
        if not (items.get("media") or []):
            raise RollbackError("VERSION_INCOMPLETE", "Target version has no media items")

        lock = product_has_active_media_op(
            self.db, shop_id=self.shop.id, shopify_product_gid=product.shopify_product_gid
        )
        if lock:
            raise RollbackError(lock, "Another media operation is already active for this product")

        key = build_rollback_idempotency_key(
            shop_id=self.shop.id,
            shopify_product_gid=product.shopify_product_gid,
            from_version_id=active.id,
            target_version_id=target.id,
        )
        existing = (
            self.db.query(ProductRollbackOperation)
            .filter(ProductRollbackOperation.idempotency_key == key)
            .one_or_none()
        )
        if existing:
            if existing.status == RollbackStatus.ROLLED_BACK:
                return {
                    "operationId": str(existing.id),
                    "status": existing.status.value,
                    "message": "Rollback already completed",
                    "alreadyCompleted": True,
                }
            if existing.status in ACTIVE_ROLLBACK_STATUSES_LOCAL:
                return {
                    "operationId": str(existing.id),
                    "status": existing.status.value,
                    "message": "Rollback already queued",
                    "alreadyQueued": True,
                }
            if existing.status in RETRYABLE_ROLLBACK_STATUSES:
                existing.status = RollbackStatus.QUEUED
                existing.current_stage = "QUEUED"
                existing.attempt_number = (existing.attempt_number or 1) + 1
                existing.queued_at = datetime.now(timezone.utc)
                existing.started_at = None
                existing.completed_at = None
                existing.last_error_code = None
                existing.last_error_message = None
                existing.locked_by = None
                existing.locked_at = None
                self.db.commit()
                return {
                    "operationId": str(existing.id),
                    "status": existing.status.value,
                    "message": "Rollback retry queued",
                    "retried": True,
                }

        op = ProductRollbackOperation(
            shop_id=self.shop.id,
            product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            from_version_id=active.id,
            target_version_id=target.id,
            status=RollbackStatus.QUEUED,
            current_stage="QUEUED",
            idempotency_key=key,
            attempt_number=1,
            queued_at=datetime.now(timezone.utc),
        )
        self.db.add(op)
        self.db.commit()
        return {
            "operationId": str(op.id),
            "status": op.status.value,
            "message": "Rollback has been queued",
        }

    def retry(self, operation_id: UUID) -> dict[str, Any]:
        op = (
            self.db.query(ProductRollbackOperation)
            .filter(
                ProductRollbackOperation.id == operation_id,
                ProductRollbackOperation.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if not op:
            raise RollbackError("VERSION_NOT_FOUND", "Rollback operation not found")
        if op.status not in RETRYABLE_ROLLBACK_STATUSES:
            raise RollbackError("ROLLBACK_ALREADY_ACTIVE", f"Cannot retry from status {op.status.value}")

        lock = product_has_active_media_op(
            self.db, shop_id=self.shop.id, shopify_product_gid=op.shopify_product_gid
        )
        if lock and lock != "ROLLBACK_ALREADY_ACTIVE":
            # Allow if the only active is this op? It's not active if retryable.
            raise RollbackError(lock, "Another media operation is active for this product")

        op.status = RollbackStatus.QUEUED
        op.current_stage = "QUEUED"
        op.attempt_number = (op.attempt_number or 1) + 1
        op.queued_at = datetime.now(timezone.utc)
        op.started_at = None
        op.completed_at = None
        op.locked_by = None
        op.locked_at = None
        op.last_error_code = None
        op.last_error_message = None
        self.db.commit()
        return {"operationId": str(op.id), "status": op.status.value, "message": "Rollback retry queued"}

    def rollback_preview(self, product_id: UUID, target_version_id: UUID) -> dict[str, Any]:
        product = self.versions.get_product(product_id)
        target = self.versions.get_version(product_id, target_version_id)
        active = self.versions.active_version(product_id)
        eligibility = self._check_target_files(target)
        return {
            "productId": str(product.id),
            "shopifyProductGid": product.shopify_product_gid,
            "title": product.title,
            "current": _version_summary(active) if active else None,
            "target": _version_summary(target),
            "eligible": eligibility["eligible"],
            "unavailableReason": eligibility.get("reason"),
            "warnings": eligibility.get("warnings") or [],
            "alreadyActive": bool(active and active.id == target.id),
        }

    def _check_target_files(self, target: ProductMediaVersion) -> dict[str, Any]:
        media = (target.items_json or {}).get("media") or []
        if not media:
            self.versions.mark_unavailable(target, "Version has no media items")
            self.db.commit()
            return {"eligible": False, "reason": "VERSION_INCOMPLETE", "warnings": []}

        # Rollback restores durable Shopify Files (CDN). Require file identity per item.
        missing_file_identity = [
            m
            for m in media
            if isinstance(m, dict) and not (m.get("file_gid") or m.get("media_gid"))
        ]
        if missing_file_identity:
            reason = "Version media missing Shopify file/CDN identity"
            self.versions.mark_unavailable(target, reason)
            self.db.commit()
            return {"eligible": False, "reason": reason, "warnings": []}

        file_gids = [m.get("file_gid") or m.get("media_gid") for m in media]
        file_gids = [g for g in file_gids if g]
        warnings: list[str] = []
        try:
            statuses = self.client.get_file_statuses(file_gids)
        except ShopifyGraphQLError as exc:
            return {"eligible": False, "reason": str(exc), "warnings": warnings}

        by_id = {s.get("id"): s for s in (statuses or []) if s.get("id")}
        missing = [g for g in file_gids if g not in by_id]
        not_ready = [
            g
            for g in file_gids
            if g in by_id and str(by_id[g].get("fileStatus") or "").upper() not in {"READY", ""}
            and by_id[g].get("fileStatus") is not None
            and str(by_id[g].get("fileStatus")).upper() != "READY"
        ]
        # Some GraphQL shapes use status instead of fileStatus
        for g in file_gids:
            node = by_id.get(g) or {}
            st = str(node.get("fileStatus") or node.get("status") or "READY").upper()
            if st not in {"READY", "UPLOADED"} and g not in missing:
                if g not in not_ready:
                    not_ready.append(g)

        if missing:
            reason = f"Historical Shopify files missing: {', '.join(missing[:3])}"
            self.versions.mark_unavailable(target, reason)
            self.db.commit()
            return {"eligible": False, "reason": reason, "warnings": warnings}
        if not_ready:
            reason = f"Historical Shopify files not ready: {', '.join(not_ready[:3])}"
            self.versions.mark_unavailable(target, reason)
            self.db.commit()
            return {"eligible": False, "reason": reason, "warnings": warnings}

        # Enrich snapshot CDN URLs from live Shopify Files (preview + conflict identity).
        items = dict(target.items_json or {})
        enriched_media, changed = _enrich_media_cdn_from_file_nodes(media, by_id)
        if changed:
            items["media"] = enriched_media
            target.items_json = items
            self.db.flush()

        if not target.rollback_eligible:
            self.versions.mark_eligible(target)
            self.db.commit()
        else:
            self.db.commit()
        return {"eligible": True, "reason": None, "warnings": warnings}

    def run(self, operation_id: UUID) -> ProductRollbackOperation:
        op = (
            self.db.query(ProductRollbackOperation)
            .filter(
                ProductRollbackOperation.id == operation_id,
                ProductRollbackOperation.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if not op:
            raise RollbackError("VERSION_NOT_FOUND", "Rollback operation not found")

        product = self.versions.get_product(op.product_id)
        target = self.versions.get_version(op.product_id, op.target_version_id)
        active = self.versions.active_version(op.product_id)
        if not active:
            return self._fail(op, "VERSION_NOT_FOUND", "No active version")

        attached_target = False
        detached_current = False
        pre_snapshot: dict[str, Any] | None = None
        target_file_gids: list[str] = []
        current_file_gids: list[str] = []

        try:
            self._set_stage(op, "VALIDATING_TARGET")
            eligibility = self._check_target_files(target)
            if not eligibility["eligible"]:
                raise RollbackError("VERSION_FILE_MISSING", eligibility.get("reason") or "Target files unavailable")

            target_snap = target.items_json or {}
            target_file_gids = [
                m.get("file_gid") or m.get("media_gid") for m in (target_snap.get("media") or [])
            ]
            target_file_gids = [g for g in target_file_gids if g]

            self._set_stage(op, "CHECKING_CONFLICT")
            live_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            if not live_raw:
                raise RollbackError("ROLLBACK_TARGET_INVALID", "Product missing in Shopify")
            live = normalize_publish_snapshot(live_raw)
            pre_snapshot = live
            op.pre_rollback_snapshot_json = live
            self.db.flush()

            # Enrich active CDN from Files so rematerialized MediaImage GIDs can match by path.
            active_snap = dict(active.items_json or {})
            active_media = [m for m in (active_snap.get("media") or []) if isinstance(m, dict)]
            active_file_gids = [
                m.get("file_gid") or m.get("media_gid") for m in active_media if (m.get("file_gid") or m.get("media_gid"))
            ]
            if active_file_gids:
                try:
                    active_nodes = self.client.get_file_statuses(active_file_gids)
                    active_by_id = {n.get("id"): n for n in active_nodes if n.get("id")}
                    enriched_active, active_changed = _enrich_media_cdn_from_file_nodes(
                        active_media, active_by_id
                    )
                    if active_changed:
                        active_snap["media"] = enriched_active
                        active.items_json = active_snap
                        self.db.flush()
                except ShopifyGraphQLError:
                    logger.warning(
                        "Could not enrich active version CDN before conflict check | product=%s",
                        op.shopify_product_gid,
                        exc_info=True,
                    )

            conflict = _compare_by_files(active.items_json or {}, live)
            if conflict.get("hasConflict"):
                op.conflict_details = conflict
                raise RollbackError(
                    "ROLLBACK_CONFLICT",
                    "Live Shopify product media no longer matches the active version",
                    conflict=conflict,
                )

            current_file_gids = [
                m.get("file_gid") or m.get("media_gid") for m in (live.get("media") or [])
            ]
            current_file_gids = [g for g in current_file_gids if g]

            self._set_stage(op, "ATTACHING_TARGET_SET")
            # Attach all target files (Shopify ignores already-associated)
            self.client.add_file_product_references(
                file_gids=target_file_gids,
                product_gid=op.shopify_product_gid,
            )
            attached_target = True

            self._set_stage(op, "RESTORING_ALT_TEXT")
            for media in target_snap.get("media") or []:
                fg = media.get("file_gid") or media.get("media_gid")
                if fg:
                    try:
                        self.client.update_file_alt_text(file_gid=fg, alt=media.get("alt_text"))
                    except ShopifyGraphQLError:
                        logger.warning("Alt restore failed | file=%s", fg)

            mid_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            mid = normalize_publish_snapshot(mid_raw or {})
            file_to_live = _map_file_to_media(mid)

            self._set_stage(op, "RESTORING_VARIANTS")
            variant_inputs = []
            for v in target_snap.get("variants") or []:
                variant_gid = v.get("variant_gid")
                source = v.get("media_gid") or v.get("file_gid")
                if not variant_gid or not source:
                    continue
                live_media = file_to_live.get(source) or _resolve_live_media_id(
                    file_to_live, {"media_gid": source, "file_gid": source, "cdn_url": None}
                )
                # Prefer CDN match from target media row when GID rematerialized
                if not live_media:
                    for tm in target_snap.get("media") or []:
                        if (tm.get("media_gid") or tm.get("file_gid")) == source:
                            live_media = _resolve_live_media_id(file_to_live, tm)
                            break
                if live_media:
                    variant_inputs.append({"id": variant_gid, "mediaId": live_media})
            if variant_inputs:
                self.client.associate_media_to_variants(
                    product_gid=op.shopify_product_gid, variants=variant_inputs
                )

            self._set_stage(op, "REORDERING_TARGET_SET")
            ordered_ids = []
            for media in sorted(target_snap.get("media") or [], key=lambda x: x.get("position") or 0):
                mid = _resolve_live_media_id(file_to_live, media)
                if mid and mid not in ordered_ids:
                    ordered_ids.append(mid)
            if ordered_ids:
                moves = [{"id": mid, "newPosition": str(idx)} for idx, mid in enumerate(ordered_ids)]
                job = self.client.reorder_product_media(product_gid=op.shopify_product_gid, moves=moves)
                poll_reorder_job(self.client, job.get("id"))

            self._set_stage(op, "VERIFYING_TARGET_SET")
            verify_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            verify = normalize_publish_snapshot(verify_raw or {})
            verify_files = _file_keys(verify)
            target_ids = set(_identity_index(target_snap))
            verify_ids = set(_identity_index(verify))
            if not set(target_file_gids).issubset(verify_files) and not (
                target_ids and target_ids.issubset(verify_ids)
            ):
                raise RollbackError("ROLLBACK_VERIFICATION_FAILED", "Target media not fully attached")

            self._set_stage(op, "DETACHING_CURRENT_SET")
            to_detach = [g for g in current_file_gids if g not in set(target_file_gids)]
            if to_detach:
                self.client.remove_file_product_references(
                    file_gids=to_detach,
                    product_gid=op.shopify_product_gid,
                )
            detached_current = True

            self._set_stage(op, "FINAL_REORDER")
            final_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            final_mid = normalize_publish_snapshot(final_raw or {})
            file_to_live = _map_file_to_media(final_mid)
            ordered_ids = []
            for media in sorted(target_snap.get("media") or [], key=lambda x: x.get("position") or 0):
                mid = _resolve_live_media_id(file_to_live, media)
                if mid and mid not in ordered_ids:
                    ordered_ids.append(mid)
            if ordered_ids:
                moves = [{"id": mid, "newPosition": str(idx)} for idx, mid in enumerate(ordered_ids)]
                job = self.client.reorder_product_media(product_gid=op.shopify_product_gid, moves=moves)
                poll_reorder_job(self.client, job.get("id"))

            self._set_stage(op, "VERIFYING_FINAL_STATE")
            done_raw = self.client.get_product_media_snapshot(op.shopify_product_gid)
            done = normalize_publish_snapshot(done_raw or {})
            done_files = _file_keys(done)
            done_ids = set(_identity_index(done))
            if set(done_files) != set(target_file_gids):
                # Allow media_gid aliasing / CDN rematerialization
                if target_ids and target_ids == done_ids:
                    pass
                elif not set(target_file_gids).issubset(done_files) and not (
                    target_ids and target_ids.issubset(done_ids)
                ):
                    raise RollbackError("ROLLBACK_VERIFICATION_FAILED", "Final media membership mismatch")
                else:
                    extras = done_files - set(target_file_gids)
                    if extras & set(current_file_gids):
                        # Rematerialized current IDs may not be in done_files as originals
                        extra_ids = done_ids - target_ids
                        current_snap_ids = set(_identity_index(pre_snapshot or {}))
                        if extra_ids & current_snap_ids:
                            raise RollbackError(
                                "ROLLBACK_VERIFICATION_FAILED",
                                "Old media still associated after detach",
                            )
                    if target_ids and not target_ids.issubset(done_ids):
                        raise RollbackError("ROLLBACK_VERIFICATION_FAILED", "Final media membership mismatch")

            # Reactivate the selected historical version (no new ROLLBACK row).
            result = self.versions.activate_existing_version(
                target,
                rollback_operation_id=op.id,
            )
            try:
                from app.services.image_versions import ImageVersionsService

                media_items = list((result.items_json or {}).get("media") or [])
                ImageVersionsService(self.db, self.shop).mark_published_for_product_version(
                    product_id=product.id,
                    product_media_version_id=result.id,
                    media_items=media_items,
                    actor_type="rollback",
                )
            except Exception:
                logger.exception(
                    "Failed to link image versions after rollback | op=%s product=%s",
                    op.id,
                    op.shopify_product_gid,
                )
            now = datetime.now(timezone.utc)
            op.result_version_id = result.id
            op.status = RollbackStatus.ROLLED_BACK
            op.current_stage = "ROLLED_BACK"
            op.completed_at = now
            op.last_error_code = None
            op.last_error_message = None
            self.db.commit()
            logger.info(
                "Rollback succeeded | op=%s product=%s target=%s result=%s",
                op.id,
                op.shopify_product_gid,
                target.id,
                result.id,
            )
            return op

        except RollbackError as exc:
            if attached_target or detached_current:
                return self._compensate_and_fail(op, pre_snapshot, target_file_gids, detached_current, exc)
            if exc.code == "ROLLBACK_CONFLICT":
                return self._fail_conflict(op, exc)
            return self._fail(op, exc.code, str(exc), conflict=exc.conflict)
        except ShopifyGraphQLError as exc:
            wrapped = RollbackError("ROLLBACK_ATTACH_FAILED", str(exc))
            if attached_target or detached_current:
                return self._compensate_and_fail(op, pre_snapshot, target_file_gids, detached_current, wrapped)
            return self._fail(op, wrapped.code, str(exc))
        except PublishCompensationError as exc:
            return self._fail_restore(op, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected rollback failure | op=%s", operation_id)
            wrapped = RollbackError("ROLLBACK_FAILED", str(exc))
            if attached_target or detached_current:
                return self._compensate_and_fail(op, pre_snapshot, target_file_gids, detached_current, wrapped)
            return self._fail(op, "ROLLBACK_FAILED", str(exc))

    def _compensate_and_fail(
        self,
        op: ProductRollbackOperation,
        pre_snapshot: dict[str, Any] | None,
        target_file_gids: list[str],
        detached_current: bool,
        exc: RollbackError,
    ) -> ProductRollbackOperation:
        self._set_stage(op, "RESTORING_PRE_ROLLBACK")
        try:
            if pre_snapshot is None:
                raise PublishCompensationError("ROLLBACK_RESTORE_FAILED", "No pre-rollback snapshot")
            # Restore to pre-rollback; detach target-only files when safe
            self.compensation.restore_original(
                product_gid=op.shopify_product_gid,
                original_snapshot=pre_snapshot,
                new_file_gids=target_file_gids if not detached_current else target_file_gids,
            )
            return self._fail(op, exc.code, str(exc), conflict=exc.conflict)
        except (PublishCompensationError, ShopifyGraphQLError) as restore_exc:
            code = getattr(restore_exc, "code", "ROLLBACK_RESTORE_FAILED")
            return self._fail_restore(op, code, str(restore_exc))

    def _set_stage(self, op: ProductRollbackOperation, stage: str) -> None:
        op.status = RollbackStatus.ROLLING_BACK
        op.current_stage = stage
        if op.started_at is None:
            op.started_at = datetime.now(timezone.utc)
        self.db.flush()

    def _fail(
        self,
        op: ProductRollbackOperation,
        code: str,
        message: str,
        *,
        conflict: dict | None = None,
    ) -> ProductRollbackOperation:
        op.status = RollbackStatus.ROLLBACK_FAILED
        op.current_stage = "ROLLBACK_FAILED"
        op.last_error_code = code
        op.last_error_message = message[:2000]
        op.completed_at = datetime.now(timezone.utc)
        if conflict:
            op.conflict_details = conflict
        self.db.commit()
        return op

    def _fail_conflict(self, op: ProductRollbackOperation, exc: RollbackError) -> ProductRollbackOperation:
        op.status = RollbackStatus.ROLLBACK_CONFLICT
        op.current_stage = "ROLLBACK_CONFLICT"
        op.last_error_code = exc.code
        op.last_error_message = str(exc)[:2000]
        op.conflict_details = exc.conflict
        op.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        return op

    def _fail_restore(self, op: ProductRollbackOperation, code: str, message: str) -> ProductRollbackOperation:
        op.status = RollbackStatus.RESTORE_FAILED
        op.current_stage = "RESTORE_FAILED"
        op.last_error_code = code
        op.last_error_message = message[:2000]
        op.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        return op


ACTIVE_ROLLBACK_STATUSES_LOCAL = {RollbackStatus.QUEUED, RollbackStatus.ROLLING_BACK}


def _map_file_to_media(snapshot: dict[str, Any]) -> dict[str, str]:
    """Map file/media GIDs (and CDN identities) to live product MediaImage GIDs."""
    mapping: dict[str, str] = {}
    by_identity = _identity_index(snapshot)
    for m in snapshot.get("media") or []:
        mid = m.get("media_gid")
        if not mid:
            continue
        mapping[mid] = mid
        if m.get("file_gid"):
            mapping[m["file_gid"]] = mid
        key = _media_identity(m)
        if key:
            mapping[key] = mid
    # Allow lookup from historical identities that share CDN with live rows
    for key, m in by_identity.items():
        mid = m.get("media_gid")
        if mid:
            mapping[key] = mid
    return mapping


def _resolve_live_media_id(
    file_to_live: dict[str, str],
    media: dict[str, Any],
) -> str | None:
    key = media.get("file_gid") or media.get("media_gid")
    if key and key in file_to_live:
        return file_to_live[key]
    ident = _media_identity(media)
    if ident and ident in file_to_live:
        return file_to_live[ident]
    return None


def _version_summary(version: ProductMediaVersion | None) -> dict[str, Any] | None:
    if not version:
        return None
    items = version.items_json or {}
    media = items.get("media") or []
    return {
        "versionId": str(version.id),
        "versionNumber": version.version_number,
        "versionType": version.version_type.value,
        "isActive": version.is_active,
        "rollbackEligible": version.rollback_eligible,
        "unavailableReason": version.unavailable_reason,
        "snapshotHash": version.snapshot_hash,
        "imageCount": len(media),
        "media": [
            {
                "mediaGid": m.get("media_gid"),
                "fileGid": m.get("file_gid"),
                "position": m.get("position"),
                "isPrimary": m.get("is_primary"),
                "altText": m.get("alt_text"),
                "cdnUrl": m.get("cdn_url"),
                "filename": m.get("filename"),
            }
            for m in media
        ],
        "variants": items.get("variants") or [],
        "createdAt": version.created_at.isoformat() if version.created_at else None,
        "activatedAt": version.activated_at.isoformat() if version.activated_at else None,
    }

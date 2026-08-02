"""Compensation / restoration after partial Shopify publish failures."""

from __future__ import annotations

import logging
from typing import Any

from app.services.publish_snapshot import normalize_publish_snapshot, snapshot_hash
from app.services.shopify_file_upload import poll_reorder_job
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError

logger = logging.getLogger("app.services.publish_compensation")


class PublishCompensationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PublishCompensationService:
    def __init__(self, client: ShopifyGraphQLClient) -> None:
        self.client = client

    def restore_original(
        self,
        *,
        product_gid: str,
        original_snapshot: dict[str, Any],
        new_file_gids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Restore product media associations to the pre-publish snapshot.

        Never calls fileDelete. Only adjusts product references for this product GID.
        """
        original_media = original_snapshot.get("media") or []
        original_file_gids = [m.get("file_gid") or m.get("media_gid") for m in original_media]
        original_file_gids = [g for g in original_file_gids if g]
        new_gids = [g for g in (new_file_gids or []) if g]

        # 1) Re-add original file references
        if original_file_gids:
            try:
                self.client.add_file_product_references(
                    file_gids=original_file_gids,
                    product_gid=product_gid,
                )
            except ShopifyGraphQLError as exc:
                raise PublishCompensationError("PUBLISH_COMPENSATION_FAILED", str(exc)) from exc

        # 2) Remove newly added generated associations from this product only
        if new_gids:
            try:
                self.client.remove_file_product_references(
                    file_gids=new_gids,
                    product_gid=product_gid,
                )
            except ShopifyGraphQLError as exc:
                raise PublishCompensationError("PUBLISH_COMPENSATION_FAILED", str(exc)) from exc

        # 3) Restore alt text on originals
        for media in original_media:
            file_gid = media.get("file_gid") or media.get("media_gid")
            if not file_gid:
                continue
            try:
                self.client.update_file_alt_text(file_gid=file_gid, alt=media.get("alt_text"))
            except ShopifyGraphQLError:
                logger.warning("Failed to restore alt text | file=%s", file_gid)

        # 4) Resolve live media GIDs for reorder / variants after re-attach
        live_raw = self.client.get_product_media_snapshot(product_gid)
        if not live_raw:
            raise PublishCompensationError("PUBLISH_RESTORE_FAILED", "Product missing during restore")
        live = normalize_publish_snapshot(live_raw)

        # Map original media/file to current media GIDs on product
        file_to_live_media: dict[str, str] = {}
        for m in live.get("media") or []:
            file_to_live_media[m["media_gid"]] = m["media_gid"]
            if m.get("file_gid"):
                file_to_live_media[m["file_gid"]] = m["media_gid"]

        ordered_live_ids: list[str] = []
        for media in sorted(original_media, key=lambda x: x.get("position") or 0):
            key = media.get("media_gid") or media.get("file_gid")
            file_key = media.get("file_gid") or media.get("media_gid")
            live_id = file_to_live_media.get(key or "") or file_to_live_media.get(file_key or "")
            if live_id and live_id not in ordered_live_ids:
                ordered_live_ids.append(live_id)

        if ordered_live_ids:
            moves = [{"id": mid, "newPosition": str(idx)} for idx, mid in enumerate(ordered_live_ids)]
            try:
                job = self.client.reorder_product_media(product_gid=product_gid, moves=moves)
                poll_reorder_job(self.client, job.get("id"))
            except ShopifyGraphQLError as exc:
                raise PublishCompensationError("PUBLISH_RESTORE_FAILED", str(exc)) from exc

        # 5) Restore variant media associations
        variant_inputs: list[dict[str, Any]] = []
        for v in original_snapshot.get("variants") or []:
            variant_gid = v.get("variant_gid")
            source_media = v.get("media_gid")
            if not variant_gid or not source_media:
                continue
            live_media = file_to_live_media.get(source_media)
            if live_media:
                variant_inputs.append({"id": variant_gid, "mediaId": live_media})
        if variant_inputs:
            try:
                self.client.associate_media_to_variants(product_gid=product_gid, variants=variant_inputs)
            except ShopifyGraphQLError as exc:
                raise PublishCompensationError("PUBLISH_RESTORE_FAILED", str(exc)) from exc

        # 6) Verify
        final_raw = self.client.get_product_media_snapshot(product_gid)
        if not final_raw:
            raise PublishCompensationError("PUBLISH_RESTORE_FAILED", "Product missing after restore")
        final = normalize_publish_snapshot(final_raw)
        expected_ids = {m.get("media_gid") for m in original_media if m.get("media_gid")}
        final_ids = {m.get("media_gid") for m in (final.get("media") or []) if m.get("media_gid")}
        # After re-attach, Shopify may keep same MediaImage GIDs for originals.
        if expected_ids and not expected_ids.issubset(final_ids):
            # Also accept file_gid match
            final_files = {m.get("file_gid") or m.get("media_gid") for m in (final.get("media") or [])}
            expected_files = {
                m.get("file_gid") or m.get("media_gid") for m in original_media if m.get("media_gid") or m.get("file_gid")
            }
            if not expected_files.issubset(final_files):
                raise PublishCompensationError(
                    "PUBLISH_RESTORE_FAILED",
                    "Restored product media does not match original snapshot",
                )

        return {
            "restored": True,
            "snapshot_hash": snapshot_hash(final),
            "snapshot": final,
        }

    def detach_new_only(
        self,
        *,
        product_gid: str,
        new_file_gids: list[str],
    ) -> None:
        """Remove only newly attached generated files from the product (pre-destructive failure)."""
        gids = [g for g in new_file_gids if g]
        if not gids:
            return
        self.client.remove_file_product_references(file_gids=gids, product_gid=product_gid)

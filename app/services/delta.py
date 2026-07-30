"""Delta comparison between eligible media snapshots and processing baselines."""

from __future__ import annotations

from typing import Any, TypedDict

from app.models.enums import DeltaType
from app.services.snapshot import media_fingerprint


class MediaDeltaResult(TypedDict):
    new: list[dict[str, Any]]
    replaced: list[dict[str, Any]]
    skip_reason: str | None


def _media_gid(entry: dict[str, Any]) -> str | None:
    gid = entry.get("media_gid") or entry.get("shopify_media_gid")
    return str(gid) if gid else None


def _normalize_media_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Accept both snapshot helper keys and ORM-ish keys."""
    normalized = dict(entry)
    if "media_gid" not in normalized and entry.get("shopify_media_gid"):
        normalized["media_gid"] = entry["shopify_media_gid"]
    if "file_gid" not in normalized and entry.get("shopify_file_gid"):
        normalized["file_gid"] = entry.get("shopify_file_gid")
    if "fingerprint" not in normalized:
        normalized["fingerprint"] = (
            entry.get("content_fingerprint")
            or entry.get("source_fingerprint")
            or media_fingerprint(normalized)
        )
    if "filename" not in normalized and entry.get("original_filename"):
        normalized["filename"] = entry.get("original_filename")
    if "updated_at" not in normalized and entry.get("shopify_updated_at"):
        normalized["updated_at"] = entry.get("shopify_updated_at")
    return normalized


def _baseline_by_media_gid(baseline_media: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in baseline_media or []:
        normalized = _normalize_media_entry(entry)
        gid = _media_gid(normalized)
        if gid:
            result[gid] = normalized
    return result


def _content_changed(eligible: dict[str, Any], baseline: dict[str, Any]) -> bool:
    eligible_n = _normalize_media_entry(eligible)
    baseline_n = _normalize_media_entry(baseline)
    eligible_fp = eligible_n.get("fingerprint") or media_fingerprint(eligible_n)
    baseline_fp = baseline_n.get("fingerprint") or media_fingerprint(baseline_n)
    if eligible_fp != baseline_fp:
        return True
    for field in ("file_gid", "cdn_url", "filename", "width", "height", "mime_type", "updated_at"):
        if (eligible_n.get(field) or None) != (baseline_n.get(field) or None):
            return True
    return False


def compare_media_snapshots(
    eligible_media: list[dict[str, Any]],
    baseline_media: list[dict[str, Any]] | None,
) -> MediaDeltaResult:
    """
    Compare eligible queued media against the processing baseline.

    NEW: media_gid present in eligible but not in baseline.
    REPLACED: same media_gid with changed fingerprint/file/content fields.
    Alt-only changes (same fingerprint ignoring alt) do not produce batch images.
    """
    baseline_map = _baseline_by_media_gid(baseline_media)
    new_items: list[dict[str, Any]] = []
    replaced_items: list[dict[str, Any]] = []

    for eligible in eligible_media:
        eligible_n = _normalize_media_entry(eligible)
        media_gid = _media_gid(eligible_n)
        if not media_gid:
            continue
        cdn = eligible_n.get("cdn_url")
        if not cdn:
            continue

        baseline_entry = baseline_map.get(media_gid)
        if baseline_entry is None:
            item = dict(eligible_n)
            item["delta_type"] = DeltaType.NEW.value
            item["shopify_media_gid"] = media_gid
            new_items.append(item)
            continue

        if _content_changed(eligible_n, baseline_entry):
            item = dict(eligible_n)
            item["delta_type"] = DeltaType.REPLACED.value
            item["shopify_media_gid"] = media_gid
            replaced_items.append(item)

    skip_reason: str | None = None
    if not new_items and not replaced_items:
        skip_reason = "No eligible new or replaced images detected"

    return MediaDeltaResult(new=new_items, replaced=replaced_items, skip_reason=skip_reason)

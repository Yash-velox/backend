"""Detect Shopify media conflicts between processing baseline and live state."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.services.publish_conflict")


def heal_empty_publish_baseline(
    baseline: dict[str, Any],
    live: dict[str, Any],
    assets: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Repair legacy empty baselines that falsely conflict with unchanged live media.

    Older batches copied an empty ProcessingBaseline into baseline_snapshot_json.
    When the processed source media GIDs exactly match live Shopify membership,
    treat live as the baseline so publish can proceed without a false conflict.
    """
    if baseline.get("media"):
        return baseline, False

    source_ids = {
        str(a.get("source_media_gid"))
        for a in assets
        if isinstance(a, dict) and a.get("source_media_gid")
    }
    live_ids = {
        str(m.get("media_gid"))
        for m in (live.get("media") or [])
        if isinstance(m, dict) and m.get("media_gid")
    }
    if not source_ids or source_ids != live_ids:
        return baseline, False

    healed = {
        "product_gid": live.get("product_gid") or baseline.get("product_gid"),
        "updated_at": live.get("updated_at"),
        "featured_media_gid": live.get("featured_media_gid"),
        "media": list(live.get("media") or []),
        "variants": list(live.get("variants") or []),
    }
    logger.info(
        "Healed empty publish baseline from live snapshot | sources=%s",
        len(source_ids),
    )
    return healed, True


def compare_publish_snapshots(
    baseline: dict[str, Any],
    live: dict[str, Any],
) -> dict[str, Any]:
    """Return structured conflict details. Empty meaningful changes => no conflict."""
    baseline_media = {m["media_gid"]: m for m in (baseline.get("media") or []) if m.get("media_gid")}
    live_media = {m["media_gid"]: m for m in (live.get("media") or []) if m.get("media_gid")}

    baseline_ids = list(baseline_media.keys())
    live_ids = list(live_media.keys())

    added = [gid for gid in live_ids if gid not in baseline_media]
    removed = [gid for gid in baseline_ids if gid not in live_media]

    baseline_order = [m["media_gid"] for m in sorted(baseline.get("media") or [], key=lambda x: x.get("position") or 0)]
    live_order = [m["media_gid"] for m in sorted(live.get("media") or [], key=lambda x: x.get("position") or 0)]
    # Compare relative order of shared IDs only for reorder (membership handled separately).
    shared_baseline = [gid for gid in baseline_order if gid in live_media]
    shared_live = [gid for gid in live_order if gid in baseline_media]
    order_changed = shared_baseline != shared_live

    alt_changes: list[dict[str, Any]] = []
    for gid in baseline_ids:
        if gid not in live_media:
            continue
        b_alt = baseline_media[gid].get("alt_text") or ""
        l_alt = live_media[gid].get("alt_text") or ""
        if b_alt != l_alt:
            alt_changes.append({"media_gid": gid, "baseline": b_alt, "live": l_alt})

    featured_baseline = baseline.get("featured_media_gid")
    featured_live = live.get("featured_media_gid")
    featured_changed = bool(featured_baseline and featured_live and featured_baseline != featured_live)
    # Shopify often attaches the featured image to variants that previously had no media.
    # That is not a merchant gallery edit; treat null → featured as non-conflicting.
    featured_for_variants = featured_live or featured_baseline

    baseline_variants = {
        v["variant_gid"]: v.get("media_gid")
        for v in (baseline.get("variants") or [])
        if v.get("variant_gid")
    }
    live_variants = {
        v["variant_gid"]: v.get("media_gid")
        for v in (live.get("variants") or [])
        if v.get("variant_gid")
    }

    def _benign_null_to_featured(baseline_media: Any, live_media: Any) -> bool:
        if baseline_media:
            return False
        if not live_media or not featured_for_variants:
            return False
        return live_media == featured_for_variants

    variant_changes: list[dict[str, Any]] = []
    for vgid, b_media in baseline_variants.items():
        l_media = live_variants.get(vgid)
        if vgid in live_variants and b_media != l_media:
            if _benign_null_to_featured(b_media, l_media):
                continue
            variant_changes.append(
                {"variant_gid": vgid, "baseline_media_gid": b_media, "live_media_gid": l_media}
            )
    for vgid, l_media in live_variants.items():
        if vgid not in baseline_variants and l_media:
            if _benign_null_to_featured(None, l_media):
                continue
            variant_changes.append(
                {"variant_gid": vgid, "baseline_media_gid": None, "live_media_gid": l_media}
            )

    has_conflict = bool(
        added or removed or order_changed or alt_changes or featured_changed or variant_changes
    )
    membership_changed = bool(added or removed)

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
        "baselineMediaCount": len(baseline_ids),
        "liveMediaCount": len(live_ids),
    }

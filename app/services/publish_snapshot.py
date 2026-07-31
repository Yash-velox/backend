"""Canonical live Shopify product media snapshots for publishing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.snapshot import filename_from_url


def normalize_publish_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize GraphQL product node into a deterministic publish snapshot."""
    product_gid = raw.get("id") or raw.get("product_gid")
    featured = raw.get("featuredMedia") or {}
    featured_gid = featured.get("id") if isinstance(featured, dict) else None

    media_block = raw.get("media") or {}
    nodes = media_block.get("nodes") if isinstance(media_block, dict) else media_block
    if not isinstance(nodes, list):
        nodes = []

    media_rows: list[dict[str, Any]] = []
    position = 0
    for node in nodes:
        if not node:
            continue
        content_type = (node.get("mediaContentType") or "IMAGE").upper()
        if content_type not in {"IMAGE", ""} and "MediaImage" not in str(node.get("__typename") or ""):
            # Keep only image media for publish comparison.
            if node.get("image") is None and not str(node.get("id") or "").startswith("gid://shopify/MediaImage/"):
                continue
        media_gid = node.get("id") or node.get("media_gid")
        if not media_gid:
            continue
        image = node.get("image") or {}
        cdn_url = image.get("url") or node.get("cdn_url")
        filename = node.get("filename") or filename_from_url(cdn_url)
        is_primary = bool(featured_gid and media_gid == featured_gid) or position == 0 and featured_gid is None
        media_rows.append(
            {
                "media_gid": media_gid,
                "file_gid": node.get("file_gid") or media_gid,
                "position": position,
                "alt_text": node.get("alt") if "alt" in node else node.get("alt_text"),
                "cdn_url": cdn_url,
                "filename": filename,
                "width": image.get("width") or node.get("width"),
                "height": image.get("height") or node.get("height"),
                "mime_type": node.get("mimeType") or node.get("mime_type"),
                "is_primary": is_primary,
            }
        )
        position += 1

    if featured_gid:
        for row in media_rows:
            row["is_primary"] = row["media_gid"] == featured_gid
    elif media_rows:
        for idx, row in enumerate(media_rows):
            row["is_primary"] = idx == 0

    variants_block = raw.get("variants") or {}
    variant_nodes = variants_block.get("nodes") if isinstance(variants_block, dict) else variants_block
    if not isinstance(variant_nodes, list):
        variant_nodes = []

    variant_rows: list[dict[str, Any]] = []
    for vnode in variant_nodes:
        if not vnode:
            continue
        variant_gid = vnode.get("id") or vnode.get("variant_gid")
        if not variant_gid:
            continue
        media_gid = None
        media = vnode.get("media") or {}
        media_nodes = media.get("nodes") if isinstance(media, dict) else None
        if media_nodes:
            media_gid = (media_nodes[0] or {}).get("id")
        if not media_gid:
            image = vnode.get("image") or {}
            media_gid = image.get("id") if isinstance(image, dict) else None
        variant_rows.append(
            {
                "variant_gid": variant_gid,
                "media_gid": media_gid,
            }
        )

    return {
        "product_gid": product_gid,
        "updated_at": raw.get("updatedAt") or raw.get("updated_at"),
        "featured_media_gid": featured_gid or (media_rows[0]["media_gid"] if media_rows else None),
        "media": media_rows,
        "variants": variant_rows,
    }


def snapshot_from_baseline(baseline: dict[str, Any] | list | None) -> dict[str, Any]:
    """Build a publish snapshot from batch_product.baseline_snapshot_json or processing baseline media list."""
    if baseline is None:
        return {"product_gid": None, "updated_at": None, "featured_media_gid": None, "media": [], "variants": []}

    if isinstance(baseline, list):
        media_list = baseline
        product_gid = None
        variants: list[dict[str, Any]] = []
    else:
        media_list = baseline.get("media") or []
        product_gid = baseline.get("product_gid") or (baseline.get("product") or {}).get("product_gid")
        variants = baseline.get("variants") or []

    media_rows: list[dict[str, Any]] = []
    for idx, m in enumerate(media_list):
        if not isinstance(m, dict):
            continue
        media_gid = m.get("media_gid") or m.get("shopify_media_gid")
        if not media_gid:
            continue
        position = m.get("position")
        if position is None:
            position = idx
        media_rows.append(
            {
                "media_gid": media_gid,
                "file_gid": m.get("file_gid") or m.get("shopify_file_gid") or media_gid,
                "position": int(position),
                "alt_text": m.get("alt_text") or m.get("alt"),
                "cdn_url": m.get("cdn_url"),
                "filename": m.get("filename") or m.get("original_filename"),
                "width": m.get("width"),
                "height": m.get("height"),
                "mime_type": m.get("mime_type"),
                "is_primary": bool(m.get("is_primary")) or int(position) == 0,
            }
        )
    media_rows.sort(key=lambda r: (r["position"] is None, r["position"] or 0))
    featured = next((r["media_gid"] for r in media_rows if r.get("is_primary")), None)
    if not featured and media_rows:
        featured = media_rows[0]["media_gid"]
        media_rows[0]["is_primary"] = True

    variant_rows: list[dict[str, Any]] = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        variant_rows.append(
            {
                "variant_gid": v.get("variant_gid") or v.get("id"),
                "media_gid": v.get("media_gid") or v.get("image_gid"),
            }
        )

    return {
        "product_gid": product_gid,
        "updated_at": None if isinstance(baseline, list) else baseline.get("updated_at"),
        "featured_media_gid": featured,
        "media": media_rows,
        "variants": variant_rows,
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    canonical = {
        "product_gid": snapshot.get("product_gid"),
        "featured_media_gid": snapshot.get("featured_media_gid"),
        "media": [
            {
                "media_gid": m.get("media_gid"),
                "file_gid": m.get("file_gid"),
                "position": m.get("position"),
                "alt_text": m.get("alt_text") or "",
                "is_primary": bool(m.get("is_primary")),
            }
            for m in (snapshot.get("media") or [])
        ],
        "variants": [
            {
                "variant_gid": v.get("variant_gid"),
                "media_gid": v.get("media_gid"),
            }
            for v in (snapshot.get("variants") or [])
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

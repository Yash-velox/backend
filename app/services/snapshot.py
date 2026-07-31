"""Snapshot helpers for Shopify product/media state and delta comparison."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.models import Product, ProductMedia

_GID_NUMERIC_RE = re.compile(r"/(\d+)$")


def parse_shopify_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _numeric_from_gid(gid: str | None) -> str | None:
    if not gid:
        return None
    match = _GID_NUMERIC_RE.search(gid)
    return match.group(1) if match else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        path = url.split("?", 1)[0]
        name = path.rsplit("/", 1)[-1]
        return name or None
    except Exception:
        return None


# Re-export for publish snapshot helpers
filename_from_url = _filename_from_url


def media_fingerprint(media: dict[str, Any]) -> str:
    """Stable fingerprint from media identity fields (alt text excluded)."""
    parts = [
        str(media.get("media_gid") or media.get("shopify_media_gid") or ""),
        str(media.get("file_gid") or media.get("shopify_file_gid") or ""),
        str(media.get("cdn_url") or ""),
        str(media.get("filename") or media.get("original_filename") or ""),
        str(media.get("width") or ""),
        str(media.get("height") or ""),
        str(media.get("mime_type") or ""),
        str(media.get("updated_at") or media.get("shopify_updated_at") or ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def media_snapshot_from_dict(media: dict[str, Any]) -> dict[str, Any]:
    snap = {
        "media_gid": media.get("media_gid") or media.get("shopify_media_gid"),
        "file_gid": media.get("file_gid") or media.get("shopify_file_gid"),
        "cdn_url": media.get("cdn_url"),
        "filename": media.get("filename") or media.get("original_filename"),
        "width": media.get("width"),
        "height": media.get("height"),
        "mime_type": media.get("mime_type"),
        "updated_at": media.get("updated_at"),
        "position": media.get("position"),
        "is_primary": media.get("is_primary", False),
        "alt_text": media.get("alt_text"),
    }
    if snap.get("updated_at") and isinstance(snap["updated_at"], datetime):
        snap["updated_at"] = snap["updated_at"].isoformat()
    snap["fingerprint"] = media_fingerprint(snap)
    return snap


def product_snapshot_from_model(product: Product) -> dict[str, Any]:
    updated = product.shopify_updated_at.isoformat() if product.shopify_updated_at else None
    return {
        "product_gid": product.shopify_product_gid,
        "numeric_id": product.shopify_numeric_id,
        "title": product.title,
        "description_html": product.description_html,
        "handle": product.handle,
        "status": product.status,
        "product_type": product.product_type,
        "vendor": product.vendor,
        "tags": product.tags,
        "updated_at": updated,
    }


def media_snapshots_from_models(
    media: list[ProductMedia],
    *,
    visible_only: bool = True,
) -> list[dict[str, Any]]:
    rows = media
    if visible_only:
        rows = [m for m in media if m.is_visible and m.is_active]
    snapshots: list[dict[str, Any]] = []
    for m in sorted(rows, key=lambda x: (x.position is None, x.position or 0)):
        updated = m.shopify_updated_at.isoformat() if m.shopify_updated_at else None
        snap = media_snapshot_from_dict(
            {
                "media_gid": m.shopify_media_gid,
                "file_gid": m.shopify_file_gid,
                "cdn_url": m.cdn_url,
                "filename": m.original_filename,
                "width": m.width,
                "height": m.height,
                "mime_type": m.mime_type,
                "updated_at": updated,
                "position": m.position,
                "is_primary": m.is_primary,
                "alt_text": m.alt_text,
            }
        )
        snapshots.append(snap)
    return snapshots


def _extract_file_gid(media_node: dict[str, Any]) -> str | None:
    """MediaImage implements the Shopify File interface, so its own GID is the file identity."""
    file_obj = media_node.get("file")
    if isinstance(file_obj, dict) and file_obj.get("id"):
        return file_obj.get("id")
    return media_node.get("id") or None


def _extract_media_image_node(media_node: dict[str, Any]) -> dict[str, Any] | None:
    if not media_node:
        return None
    content_type = media_node.get("mediaContentType")
    if content_type not in (None, "IMAGE", "MediaImage"):
        if "image" not in media_node and content_type != "IMAGE":
            return None
    image = media_node.get("image") or {}
    url = image.get("url") or ((media_node.get("originalSource") or {}).get("url"))
    if not url:
        preview_image = (media_node.get("preview") or {}).get("image") or {}
        url = preview_image.get("url")
    if not url:
        return None
    return media_node


def normalize_shopify_product_node(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Shopify GraphQL product node into product, variants, and media dicts."""
    product_gid = node.get("id") or ""
    featured_id = None
    featured = node.get("featuredMedia")
    if isinstance(featured, dict):
        featured_id = featured.get("id")

    tags = node.get("tags")
    if isinstance(tags, list):
        tags_str = ", ".join(str(t) for t in tags)
    else:
        tags_str = tags

    product = {
        "product_gid": product_gid,
        "numeric_id": _numeric_from_gid(product_gid),
        "title": node.get("title"),
        "description_html": node.get("descriptionHtml"),
        "handle": node.get("handle"),
        "status": node.get("status"),
        "product_type": node.get("productType"),
        "vendor": node.get("vendor"),
        "tags": tags_str,
        "updated_at": parse_shopify_datetime(node.get("updatedAt")),
        "raw": node,
    }

    variants: list[dict[str, Any]] = []
    variant_nodes = ((node.get("variants") or {}).get("nodes")) or []
    for v in variant_nodes:
        if not v:
            continue
        variants.append(
            {
                "variant_gid": v.get("id"),
                "sku": v.get("sku"),
                "title": v.get("title"),
                "updated_at": parse_shopify_datetime(v.get("updatedAt")),
            }
        )

    media: list[dict[str, Any]] = []
    media_nodes = ((node.get("media") or {}).get("nodes")) or []
    for position, raw_media in enumerate(media_nodes):
        media_node = _extract_media_image_node(raw_media)
        if not media_node:
            continue
        image = media_node.get("image") or {}
        url = image.get("url") or ((media_node.get("originalSource") or {}).get("url"))
        file_gid = _extract_file_gid(media_node)
        updated_at = parse_shopify_datetime(media_node.get("updatedAt"))
        media_gid = media_node.get("id") or ""
        media.append(
            {
                "media_gid": media_gid,
                "file_gid": file_gid,
                "cdn_url": url,
                "filename": _filename_from_url(url),
                "width": image.get("width"),
                "height": image.get("height"),
                "mime_type": media_node.get("mimeType"),
                "alt_text": media_node.get("alt"),
                "position": position,
                "is_primary": bool(featured_id and media_gid == featured_id),
                "is_visible": True,
                "updated_at": updated_at,
                "variant_gids": [],
            }
        )

    return {"product": product, "variants": variants, "media": media}


def product_snapshot_from_shopify(product: dict[str, Any]) -> dict[str, Any]:
    updated = product.get("updated_at")
    if isinstance(updated, datetime):
        updated = updated.isoformat()
    return {
        "product_gid": product.get("product_gid"),
        "numeric_id": product.get("numeric_id"),
        "title": product.get("title"),
        "description_html": product.get("description_html"),
        "handle": product.get("handle"),
        "status": product.get("status"),
        "product_type": product.get("product_type"),
        "vendor": product.get("vendor"),
        "tags": product.get("tags"),
        "updated_at": updated,
    }


def media_snapshots_from_shopify(media: list[dict[str, Any]], *, visible_only: bool = True) -> list[dict[str, Any]]:
    rows = media
    if visible_only:
        rows = [m for m in media if m.get("is_visible", True)]
    snapshots: list[dict[str, Any]] = []
    for m in sorted(rows, key=lambda x: (x.get("position") is None, x.get("position") or 0)):
        updated = m.get("updated_at")
        if isinstance(updated, datetime):
            updated = updated.isoformat()
        entry = dict(m)
        entry["updated_at"] = updated
        snapshots.append(media_snapshot_from_dict(entry))
    return snapshots

"""Build merchant-facing Shopify Admin and storefront product URLs."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Product, Shop

_PRODUCT_GID_RE = re.compile(r"Product/(\d+)")


def shop_host(shop: Shop) -> str:
    return (shop.shop_domain or "").replace("https://", "").replace("http://", "").split("/")[0].lower()


def snapshot_str(snapshot: dict[str, Any] | None, *keys: str) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    for key in keys:
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def product_numeric_id(
    gid: str,
    catalog: Product | None,
    snapshot: dict[str, Any] | None,
) -> str | None:
    if catalog and catalog.shopify_numeric_id:
        return str(catalog.shopify_numeric_id).strip() or None
    snap_id = snapshot_str(snapshot, "numeric_id", "shopify_numeric_id")
    if snap_id:
        return snap_id
    match = _PRODUCT_GID_RE.search(gid or "")
    return match.group(1) if match else None


def catalog_by_id(db: Session, shop: Shop, product_ids: list[UUID | None]) -> dict[UUID, Product]:
    ids = [item_id for item_id in product_ids if item_id]
    if not ids:
        return {}
    rows = db.query(Product).filter(Product.shop_id == shop.id, Product.id.in_(ids)).all()
    return {row.id: row for row in rows}


def product_link_fields(
    *,
    shop: Shop,
    shopify_product_gid: str,
    catalog: Product | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    title = (catalog.title.strip() if catalog and catalog.title else None) or snapshot_str(snapshot, "title")
    handle = (catalog.handle.strip() if catalog and catalog.handle else None) or snapshot_str(snapshot, "handle")
    numeric_id = product_numeric_id(shopify_product_gid, catalog, snapshot)
    host = shop_host(shop)
    return {
        "title": title,
        "handle": handle,
        "adminUrl": f"https://{host}/admin/products/{numeric_id}" if host and numeric_id else None,
        "storefrontUrl": f"https://{host}/products/{handle}" if host and handle else None,
    }

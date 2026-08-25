"""Catalog product listing for Jobs manual batch picker."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.deps import CurrentShop, DbSession
from app.models import Product, ProductMedia
from app.schemas.week2 import SuccessEnvelope

router = APIRouter(prefix="/api/products", tags=["products"])

# Hard cap for select-all GID payloads (protects API/memory).
SELECT_ALL_GID_CAP = 5000


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _thumbnail_url(product: Product) -> str | None:
    media = list(getattr(product, "media", None) or [])
    visible = [
        m
        for m in media
        if m.is_active and m.is_visible and m.cdn_url
    ]
    if not visible:
        return None
    visible.sort(key=lambda m: (0 if m.is_primary else 1, m.position if m.position is not None else 10_000))
    return visible[0].cdn_url


def _eligible_media_exists(shop_id):
    """Same eligibility as PrimaryBatchService.create_manual_batch visible_media."""
    return exists().where(
        ProductMedia.product_id == Product.id,
        ProductMedia.shop_id == shop_id,
        ProductMedia.is_active.is_(True),
        ProductMedia.is_visible.is_(True),
        ProductMedia.cdn_url.isnot(None),
        ProductMedia.cdn_url != "",
    )


def _filtered_products_query(
    db: DbSession,
    shop_id,
    *,
    search: str | None,
    product_type: str | None,
    status: str | None,
    has_images: bool | None = None,
):
    query = db.query(Product).filter(Product.shop_id == shop_id, Product.is_deleted.is_(False))
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Product.title.ilike(term),
                Product.handle.ilike(term),
                Product.shopify_product_gid.ilike(term),
                Product.vendor.ilike(term),
            )
        )
    if product_type and product_type.strip():
        wanted = product_type.strip().casefold()
        query = query.filter(func.lower(func.trim(Product.product_type)) == wanted)
    if status and status.strip():
        query = query.filter(Product.status == status.strip().upper())
    if has_images is True:
        query = query.filter(_eligible_media_exists(shop_id))
    elif has_images is False:
        query = query.filter(~_eligible_media_exists(shop_id))
    return query


@router.get("")
def list_products(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    search: str | None = Query(default=None),
    product_type: str | None = Query(default=None, alias="productType"),
    status: str | None = Query(default=None),
    has_images: bool | None = Query(default=None, alias="hasImages"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
):
    query = _filtered_products_query(
        db,
        shop.id,
        search=search,
        product_type=product_type,
        status=status,
        has_images=has_images,
    )
    total = query.count()
    rows = (
        query.options(selectinload(Product.media))
        .order_by(Product.title.asc(), Product.shopify_product_gid.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": str(p.id),
            "shopifyProductGid": p.shopify_product_gid,
            "title": p.title,
            "handle": p.handle,
            "status": p.status,
            "productType": p.product_type,
            "vendor": p.vendor,
            "imageUrl": _thumbnail_url(p),
        }
        for p in rows
    ]
    return SuccessEnvelope(
        success=True,
        message="Products retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": items,
            "total": int(total),
            "page": page,
            "pageSize": page_size,
            "manualBatchProductLimit": settings.manual_batch_product_limit,
        },
    )


@router.get("/matching-gids")
def matching_product_gids(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    search: str | None = Query(default=None),
    product_type: str | None = Query(default=None, alias="productType"),
    status: str | None = Query(default=None),
    has_images: bool | None = Query(default=None, alias="hasImages"),
):
    """Return all GIDs matching the current filter (for Select all)."""
    query = _filtered_products_query(
        db,
        shop.id,
        search=search,
        product_type=product_type,
        status=status,
        has_images=has_images,
    )
    total = query.count()
    rows = (
        query.order_by(Product.title.asc(), Product.shopify_product_gid.asc())
        .limit(SELECT_ALL_GID_CAP)
        .all()
    )
    items: list[dict[str, Any]] = [
        {"shopifyProductGid": p.shopify_product_gid, "title": p.title} for p in rows
    ]
    truncated = int(total) > len(items)
    return SuccessEnvelope(
        success=True,
        message="Matching product GIDs retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": items,
            "total": int(total),
            "returned": len(items),
            "truncated": truncated,
            "cap": SELECT_ALL_GID_CAP,
            "manualBatchProductLimit": settings.manual_batch_product_limit,
        },
    )


@router.get("/product-types")
def list_catalog_product_types(request: Request, db: DbSession, shop: CurrentShop):
    rows = (
        db.query(Product.product_type)
        .filter(
            Product.shop_id == shop.id,
            Product.is_deleted.is_(False),
            Product.product_type.isnot(None),
            Product.product_type != "",
        )
        .distinct()
        .order_by(Product.product_type.asc())
        .all()
    )
    types = sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()}, key=str.casefold)
    return SuccessEnvelope(
        success=True,
        message="Catalog product types retrieved successfully.",
        requestId=_request_id(request),
        data={"items": types},
    )

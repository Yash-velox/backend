from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func

from app.core.deps import CurrentShop, DbSession
from app.models import Product, ProductMedia, SyncRun
from app.schemas.week2 import SuccessEnvelope, SyncRunOut
from app.services.catalog_sync import CatalogSyncService
from app.services.shopify_graphql import ShopifyGraphQLError

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _sync_run_out(run) -> SyncRunOut:
    return SyncRunOut(
        id=run.id,
        runType=run.run_type.value,
        status=run.status.value,
        productsSynced=run.products_synced,
        mediaSynced=run.media_synced,
        cursor=run.cursor,
        errorMessage=run.error_message,
        startedAt=run.started_at,
        completedAt=run.completed_at,
        createdAt=run.created_at,
        updatedAt=run.updated_at,
    )


@router.post("/catalog")
def start_catalog_sync(request: Request, db: DbSession, shop: CurrentShop):
    svc = CatalogSyncService(db, shop)
    try:
        run = svc.start_full_sync()
    except ShopifyGraphQLError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SuccessEnvelope(
        success=True,
        message="Catalog sync completed." if run.status.value == "COMPLETED" else "Catalog sync failed.",
        requestId=_request_id(request),
        data=_sync_run_out(run).model_dump(),
    )


@router.get("/runs")
def list_sync_runs(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    limit: int = 20,
):
    runs = CatalogSyncService(db, shop).list_recent_runs(limit=limit)
    return SuccessEnvelope(
        success=True,
        message="Sync runs retrieved successfully.",
        requestId=_request_id(request),
        data={"items": [_sync_run_out(r).model_dump() for r in runs]},
    )


@router.get("/runs/{run_id}")
def get_sync_run(run_id: uuid.UUID, request: Request, db: DbSession, shop: CurrentShop):
    svc = CatalogSyncService(db, shop)
    run = next((r for r in svc.list_recent_runs(limit=100) if r.id == run_id), None)
    if run is None:
        run = (
            db.query(SyncRun)
            .filter(SyncRun.id == run_id, SyncRun.shop_id == shop.id)
            .one_or_none()
        )
    if run is None:
        raise HTTPException(status_code=404, detail="Sync run not found")

    return SuccessEnvelope(
        success=True,
        message="Sync run retrieved successfully.",
        requestId=_request_id(request),
        data=_sync_run_out(run).model_dump(),
    )


@router.get("/status")
def sync_status(request: Request, db: DbSession, shop: CurrentShop):
    svc = CatalogSyncService(db, shop)
    latest = svc.get_latest_run()

    product_count = (
        db.query(func.count(Product.id))
        .filter(Product.shop_id == shop.id, Product.is_deleted.is_(False))
        .scalar()
        or 0
    )
    media_count = (
        db.query(func.count(ProductMedia.id))
        .filter(
            ProductMedia.shop_id == shop.id,
            ProductMedia.is_active.is_(True),
        )
        .scalar()
        or 0
    )

    return SuccessEnvelope(
        success=True,
        message="Sync status retrieved successfully.",
        requestId=_request_id(request),
        data={
            "latestRun": _sync_run_out(latest).model_dump() if latest else None,
            "productCount": int(product_count),
            "activeMediaCount": int(media_count),
        },
    )

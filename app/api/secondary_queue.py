from __future__ import annotations

import math
import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.deps import CurrentShop, DbSession
from app.models import SecondaryQueueStatus
from app.schemas.week2 import (
    PaginationMeta,
    SecondaryQueueItemOut,
    SecondaryQueueSummaryOut,
    SuccessEnvelope,
)
from app.services.product_links import catalog_by_id, product_link_fields
from app.services.secondary_queue import SecondaryQueueService

router = APIRouter(prefix="/api/secondary-queue", tags=["secondary-queue"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _item_out(item, *, shop, catalog=None) -> SecondaryQueueItemOut:
    snapshot = item.eligible_product_snapshot_json if isinstance(item.eligible_product_snapshot_json, dict) else {}
    links = product_link_fields(
        shop=shop,
        shopify_product_gid=item.shopify_product_gid,
        catalog=catalog,
        snapshot=snapshot,
    )
    return SecondaryQueueItemOut(
        id=item.id,
        shopifyProductGid=item.shopify_product_gid,
        productId=item.product_id,
        title=links["title"],
        handle=links["handle"],
        adminUrl=links["adminUrl"],
        storefrontUrl=links["storefrontUrl"],
        queueRevision=item.queue_revision,
        status=item.status.value,
        webhookCount=item.webhook_count,
        firstQueuedAt=item.first_queued_at,
        lastQueuedAt=item.last_queued_at,
        latestEligibleWebhookId=item.latest_eligible_webhook_id,
        claimedAt=item.claimed_at,
        claimedBy=item.claimed_by,
        convertedBatchId=item.converted_batch_id,
        skipReason=item.skip_reason,
        failureReason=item.failure_reason,
        createdAt=item.created_at,
        updatedAt=item.updated_at,
    )


@router.get("/summary")
def secondary_queue_summary(request: Request, db: DbSession, shop: CurrentShop):
    summary = SecondaryQueueService(db, shop).summary()
    out = SecondaryQueueSummaryOut(**summary)
    return SuccessEnvelope(
        success=True,
        message="Secondary queue summary retrieved successfully.",
        requestId=_request_id(request),
        data=out.model_dump(),
    )


@router.get("")
def list_secondary_queue_items(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    page: int = Query(1, ge=1),
    pageSize: int = Query(7, ge=1, le=100),
    status: str | None = Query(None),
):
    status_filter: SecondaryQueueStatus | None = None
    if status:
        try:
            status_filter = SecondaryQueueStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from exc

    items, total = SecondaryQueueService(db, shop).list_items(
        status=status_filter,
        page=page,
        page_size=pageSize,
    )
    catalog = catalog_by_id(db, shop, [i.product_id for i in items])
    total_pages = max(1, math.ceil(total / pageSize)) if total else 0
    return SuccessEnvelope(
        success=True,
        message="Secondary queue items retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": [
                _item_out(i, shop=shop, catalog=catalog.get(i.product_id) if i.product_id else None).model_dump()
                for i in items
            ],
            "pagination": PaginationMeta(
                page=page,
                pageSize=pageSize,
                totalItems=total,
                totalPages=total_pages,
            ).model_dump(),
        },
    )


@router.get("/{item_id}")
def get_secondary_queue_item(
    item_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    item = SecondaryQueueService(db, shop).get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Secondary queue item not found")

    catalog = catalog_by_id(db, shop, [item.product_id])
    return SuccessEnvelope(
        success=True,
        message="Secondary queue item retrieved successfully.",
        requestId=_request_id(request),
        data=_item_out(
            item,
            shop=shop,
            catalog=catalog.get(item.product_id) if item.product_id else None,
        ).model_dump(),
    )

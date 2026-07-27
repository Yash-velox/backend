from __future__ import annotations

import math
import uuid
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.core.deps import CurrentShop, DbSession
from app.core.shop_resolver import resolve_shop_access_token
from app.models import QueueItemStatus
from app.schemas.queue import (
    AttemptOut,
    PaginationMeta,
    QueueItemOut,
    RetrySelectedRequest,
    ShopifyEnqueueRequest,
    SuccessEnvelope,
)
from app.services.output_storage import get_output_storage
from app.services.queue_service import QueueService
from app.services.retry_service import RetryService
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError
from app.services.shopify_image_source import ShopifyImageSourceService

router = APIRouter(prefix="/api/processing-queue", tags=["processing-queue"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _item_out(item, *, include_attempts: bool = False) -> QueueItemOut:
    attempts = None
    if include_attempts and getattr(item, "attempts", None) is not None:
        attempts = [
            AttemptOut(
                id=a.id,
                attemptNumber=a.attempt_number,
                status=a.status.value,
                provider=a.provider,
                providerRequestId=a.provider_request_id,
                shopifySourceUrl=a.shopify_source_url,
                outputStorageKey=a.output_storage_key,
                errorCode=a.error_code,
                errorMessage=a.error_message,
                startedAt=a.started_at,
                completedAt=a.completed_at,
            )
            for a in item.attempts
        ]
    return QueueItemOut(
        id=item.id,
        sourceType=item.source_type.value,
        shopifyProductId=item.shopify_product_id,
        shopifyMediaId=item.shopify_media_id,
        shopifyImageId=item.shopify_image_id,
        shopifyCdnUrl=item.shopify_cdn_url,
        originalFilename=item.original_filename,
        sourceMimeType=item.source_mime_type,
        sourceWidth=item.source_width,
        sourceHeight=item.source_height,
        status=item.status.value,
        priority=item.priority,
        batchId=item.batch_id,
        attemptCount=item.attempt_count,
        maxAttempts=item.max_attempts,
        outputStorageKey=item.output_storage_key,
        outputUrl=(
            f"/api/processing-queue/{item.id}/output"
            if item.output_storage_key and item.status.value == "COMPLETED"
            else item.output_url
        ),
        outputMimeType=item.output_mime_type,
        outputChecksum=item.output_checksum,
        errorCode=item.error_code,
        errorMessage=item.error_message,
        promptData=item.prompt_data,
        processingConfig=item.processing_config,
        processingStartedAt=item.processing_started_at,
        processingCompletedAt=item.processing_completed_at,
        nextRetryAt=item.next_retry_at,
        createdAt=item.created_at,
        updatedAt=item.updated_at,
        attempts=attempts,
    )


@router.post("/shopify-products")
def enqueue_shopify_products(
    payload: ShopifyEnqueueRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    if not payload.productIds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="productIds is required")

    # Intentionally ignore any client-supplied CDN URLs — only productIds are accepted.
    result = ShopifyImageSourceService(db, shop).enqueue_products(
        payload.productIds,
        prompt_data=payload.prompts,
        processing_config=payload.processingConfig,
        priority=payload.priority,
    )
    return SuccessEnvelope(
        success=True,
        message="Shopify product images enqueued.",
        requestId=_request_id(request),
        data={
            "productsRequested": result.products_requested,
            "productsFound": result.products_found,
            "imagesFound": result.images_found,
            "imagesQueued": result.images_queued,
            "duplicatesSkipped": result.duplicates_skipped,
            "errors": result.errors,
            "items": [_item_out(i) for i in result.items],
        },
    )


@router.get("/summary")
def queue_summary(request: Request, db: DbSession, shop: CurrentShop):
    data = QueueService(db).summary(shop_id=shop.id)
    return SuccessEnvelope(
        success=True,
        message="Queue summary retrieved successfully.",
        requestId=_request_id(request),
        data=data,
    )


@router.get("/products/search")
def search_products(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
):
    try:
        token = resolve_shop_access_token(shop)
        client = ShopifyGraphQLClient(shop_domain=shop.shop_domain, access_token=token)
        nodes = client.search_products(q, first=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ShopifyGraphQLError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY if exc.retryable else status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    products = [
        {
            "id": n.get("id"),
            "title": n.get("title"),
            "handle": n.get("handle"),
            "status": n.get("status"),
            "imageUrl": ((n.get("featuredImage") or {}).get("url")),
        }
        for n in nodes
    ]
    return SuccessEnvelope(
        success=True,
        message="Products retrieved successfully.",
        requestId=_request_id(request),
        data={"query": q, "products": products},
    )


@router.get("")
def list_queue(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    batch_id: UUID | None = Query(None, alias="batchId"),
    shopify_product_id: str | None = Query(None, alias="shopifyProductId"),
    filename: str | None = None,
    created_from: datetime | None = Query(None, alias="createdFrom"),
    created_to: datetime | None = Query(None, alias="createdTo"),
    sort_by: str = Query("created_at", alias="sortBy"),
    sort_dir: str = Query("desc", alias="sortDir"),
):
    status_enum = None
    if status_filter:
        try:
            status_enum = QueueItemStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid status filter") from exc

    items, total = QueueService(db).list_items(
        shop_id=shop.id,
        page=page,
        page_size=pageSize,
        status=status_enum,
        batch_id=batch_id,
        shopify_product_id=shopify_product_id,
        filename=filename,
        created_from=created_from,
        created_to=created_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_pages = max(1, math.ceil(total / pageSize)) if total else 0
    return SuccessEnvelope(
        success=True,
        message="Queue items retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": [_item_out(i) for i in items],
            "pagination": PaginationMeta(
                page=page,
                pageSize=pageSize,
                totalItems=total,
                totalPages=total_pages,
            ).model_dump(),
        },
    )


@router.post("/retry-selected")
def retry_selected(payload: RetrySelectedRequest, request: Request, db: DbSession, shop: CurrentShop):
    items = RetryService(db).manual_retry_items(shop_id=shop.id, item_ids=payload.itemIds)
    return SuccessEnvelope(
        success=True,
        message="Selected failed items scheduled for retry.",
        requestId=_request_id(request),
        data={"retriedCount": len(items), "items": [_item_out(i) for i in items]},
    )


@router.post("/retry-all-failed")
def retry_all_failed(request: Request, db: DbSession, shop: CurrentShop):
    items = RetryService(db).manual_retry_items(shop_id=shop.id, all_failed=True)
    return SuccessEnvelope(
        success=True,
        message="All failed items scheduled for retry.",
        requestId=_request_id(request),
        data={"retriedCount": len(items), "items": [_item_out(i) for i in items]},
    )


@router.get("/{item_id}")
def get_queue_item(item_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    item = QueueService(db).get_item(shop_id=shop.id, item_id=item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return SuccessEnvelope(
        success=True,
        message="Queue item retrieved successfully.",
        requestId=_request_id(request),
        data=_item_out(item, include_attempts=True),
    )


@router.get("/{item_id}/output")
def get_queue_item_output(item_id: UUID, db: DbSession, shop: CurrentShop):
    item = QueueService(db).get_item(shop_id=shop.id, item_id=item_id)
    if not item or not item.output_storage_key:
        raise HTTPException(status_code=404, detail="Output not found")
    storage = get_output_storage()
    path: Path = storage.resolve_path(item.output_storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Output file missing")
    return FileResponse(path, media_type=item.output_mime_type or "image/png", filename=path.name)


@router.post("/{item_id}/retry")
def retry_queue_item(item_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    items = RetryService(db).manual_retry_items(shop_id=shop.id, item_ids=[item_id])
    if not items:
        raise HTTPException(status_code=400, detail="Item is not failed or was not found")
    return SuccessEnvelope(
        success=True,
        message="Queue item scheduled for retry.",
        requestId=_request_id(request),
        data={"items": [_item_out(i) for i in items]},
    )


@router.post("/{item_id}/cancel")
def cancel_queue_item(item_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        item = QueueService(db).cancel_item(shop_id=shop.id, item_id=item_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SuccessEnvelope(
        success=True,
        message="Queue item cancelled.",
        requestId=_request_id(request),
        data=_item_out(item),
    )

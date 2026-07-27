from __future__ import annotations

import asyncio
import math
import uuid
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from app.config import settings
from app.core.deps import CurrentShop, DbSession
from app.models import TriggerType
from app.schemas.queue import BatchOut, PaginationMeta, QueueItemOut, SuccessEnvelope
from app.services.batch_service import BatchService
from app.workers.processing_worker import kickoff_batch_processing

router = APIRouter(prefix="/api/processing-batches", tags=["processing-batches"])


def _run_batch_in_background(batch_id: UUID) -> None:
    """Sync entrypoint for FastAPI BackgroundTasks (runs in a worker thread)."""
    asyncio.run(kickoff_batch_processing(batch_id))


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _batch_out(batch) -> BatchOut:
    return BatchOut(
        id=batch.id,
        triggerType=batch.trigger_type.value,
        status=batch.status.value,
        batchSize=batch.batch_size,
        totalItems=batch.total_items,
        pendingItems=batch.pending_items,
        queuedItems=batch.queued_items,
        processingItems=batch.processing_items,
        completedItems=batch.completed_items,
        retryPendingItems=batch.retry_pending_items,
        failedItems=batch.failed_items,
        cancelledItems=batch.cancelled_items,
        startedBy=batch.started_by,
        errorMessage=batch.error_message,
        createdAt=batch.created_at,
        startedAt=batch.started_at,
        completedAt=batch.completed_at,
        updatedAt=batch.updated_at,
    )


def _item_out(item) -> QueueItemOut:
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
        outputUrl=item.output_url,
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
    )


@router.post("/start")
async def start_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    shop: CurrentShop,
):
    worker_id = settings.effective_worker_id
    batch = BatchService(db).claim_pending_batch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        worker_id=worker_id,
        started_by="manual",
    )
    if not batch:
        return SuccessEnvelope(
            success=True,
            message="No pending Shopify images are available.",
            requestId=_request_id(request),
            data={"batchId": None, "itemCount": 0},
        )

    background_tasks.add_task(_run_batch_in_background, batch.id)
    return SuccessEnvelope(
        success=True,
        message="Batch created successfully.",
        requestId=_request_id(request),
        data={
            "batchId": str(batch.id),
            "itemCount": batch.total_items,
            "triggerType": batch.trigger_type.value,
        },
    )


@router.get("")
def list_batches(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
):
    items, total = BatchService(db).list_batches(shop_id=shop.id, page=page, page_size=pageSize)
    total_pages = max(1, math.ceil(total / pageSize)) if total else 0
    return SuccessEnvelope(
        success=True,
        message="Batches retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": [_batch_out(b) for b in items],
            "pagination": PaginationMeta(
                page=page,
                pageSize=pageSize,
                totalItems=total,
                totalPages=total_pages,
            ).model_dump(),
        },
    )


@router.get("/{batch_id}")
def get_batch(batch_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    batch = BatchService(db).get_batch(shop_id=shop.id, batch_id=batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return SuccessEnvelope(
        success=True,
        message="Batch retrieved successfully.",
        requestId=_request_id(request),
        data=_batch_out(batch),
    )


@router.get("/{batch_id}/items")
def get_batch_items(batch_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    batch = BatchService(db).get_batch(shop_id=shop.id, batch_id=batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items = BatchService(db).get_batch_items(shop_id=shop.id, batch_id=batch_id)
    return SuccessEnvelope(
        success=True,
        message="Batch items retrieved successfully.",
        requestId=_request_id(request),
        data={"batchId": str(batch_id), "items": [_item_out(i) for i in items]},
    )

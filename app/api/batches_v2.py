from __future__ import annotations

import math
import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.core.deps import CurrentShop, DbSession
from app.models import BatchImage, BatchStatus
from app.schemas.queue import AttemptOut
from app.schemas.week2 import (
    BatchImageOut,
    BatchOut,
    BatchProductOut,
    ManualBatchCreateRequest,
    PaginationMeta,
    ReprocessRequest,
    SuccessEnvelope,
)
from app.services.output_storage import get_output_storage
from app.services.primary_batch import PrimaryBatchError, PrimaryBatchService
from app.services.reprocess_service import ReprocessError, ReprocessService
from app.services.retry_service import RetryService

router = APIRouter(prefix="/api/batches", tags=["batches"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _batch_out(batch) -> BatchOut:
    return BatchOut(
        id=batch.id,
        triggerType=batch.trigger_type.value,
        status=batch.status.value,
        processingPhase=getattr(batch, "processing_phase", None),
        currentWorkflowStep=int(getattr(batch, "current_workflow_step", 0) or 0),
        totalWorkflowSteps=int(getattr(batch, "total_workflow_steps", 0) or 0),
        openaiRequestsTotal=int(getattr(batch, "openai_requests_total", 0) or 0),
        openaiRequestsCompleted=int(getattr(batch, "openai_requests_completed", 0) or 0),
        openaiRequestsFailed=int(getattr(batch, "openai_requests_failed", 0) or 0),
        productCount=batch.product_count,
        imageCount=batch.image_count,
        pendingProductCount=batch.pending_product_count,
        processingProductCount=batch.processing_product_count,
        completedProductCount=batch.completed_product_count,
        failedProductCount=batch.failed_product_count,
        retryingProductCount=batch.retrying_product_count,
        settingsSnapshotJson=batch.settings_snapshot_json,
        errorSummary=batch.error_summary,
        createdAt=batch.created_at,
        startedAt=batch.started_at,
        completedAt=batch.completed_at,
        updatedAt=batch.updated_at,
    )


def _product_out(product) -> BatchProductOut:
    return BatchProductOut(
        id=product.id,
        batchId=product.batch_id,
        shopifyProductGid=product.shopify_product_gid,
        productId=product.product_id,
        status=product.status.value,
        publishStatus=product.publish_status.value if product.publish_status else None,
        imageCount=product.image_count,
        retryCount=product.retry_count,
        errorCode=product.error_code,
        errorMessage=product.error_message,
        lockedBy=product.locked_by,
        lockedAt=product.locked_at,
        claimedAt=product.claimed_at,
        startedAt=product.started_at,
        completedAt=product.completed_at,
        nextRetryAt=product.next_retry_at,
        createdAt=product.created_at,
        updatedAt=product.updated_at,
    )


def _image_out(image, *, include_attempts: bool = False) -> BatchImageOut:
    attempts = None
    if include_attempts and getattr(image, "attempts", None):
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
            for a in image.attempts
        ]
    return BatchImageOut(
        id=image.id,
        batchProductId=image.batch_product_id,
        shopifyMediaGid=image.shopify_media_gid,
        shopifyFileGid=image.shopify_file_gid,
        cdnUrl=image.cdn_url,
        originalFilename=image.original_filename,
        width=image.width,
        height=image.height,
        mimeType=image.mime_type,
        sourceFingerprint=image.source_fingerprint,
        deltaType=image.delta_type.value,
        currentPromptStep=image.current_prompt_step,
        status=image.status.value,
        attemptCount=image.attempt_count,
        outputStorageKey=image.output_storage_key,
        outputUrl=image.output_url,
        outputMimeType=image.output_mime_type,
        outputChecksum=image.output_checksum,
        generatedShopifyFileGid=image.generated_shopify_file_gid,
        generatedShopifyCdnUrl=image.generated_shopify_cdn_url,
        generatedImageVersionId=image.generated_image_version_id,
        errorCode=image.error_code,
        errorMessage=image.error_message,
        startedAt=image.started_at,
        completedAt=image.completed_at,
        createdAt=image.created_at,
        updatedAt=image.updated_at,
        attempts=attempts,
    )


@router.post("/manual")
def create_manual_batch(
    payload: ManualBatchCreateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PrimaryBatchService(db, shop)
    try:
        batch = svc.create_manual_batch(payload.productGids)
    except PrimaryBatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SuccessEnvelope(
        success=True,
        message="Manual batch created successfully.",
        requestId=_request_id(request),
        data=_batch_out(batch).model_dump(),
    )


@router.get("")
def list_batches(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
):
    status_filter: BatchStatus | None = None
    if status:
        try:
            status_filter = BatchStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from exc

    items, total = PrimaryBatchService(db, shop).list_batches(
        status=status_filter,
        page=page,
        page_size=pageSize,
    )
    total_pages = max(1, math.ceil(total / pageSize)) if total else 0
    return SuccessEnvelope(
        success=True,
        message="Batches retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": [_batch_out(b).model_dump() for b in items],
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
    batch = PrimaryBatchService(db, shop).get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    return SuccessEnvelope(
        success=True,
        message="Batch retrieved successfully.",
        requestId=_request_id(request),
        data=_batch_out(batch).model_dump(),
    )


@router.get("/{batch_id}/products")
def get_batch_products(batch_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    svc = PrimaryBatchService(db, shop)
    batch = svc.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    products = svc.get_batch_products(batch_id)
    return SuccessEnvelope(
        success=True,
        message="Batch products retrieved successfully.",
        requestId=_request_id(request),
        data={
            "batchId": str(batch_id),
            "items": [_product_out(p).model_dump() for p in products],
        },
    )


@router.get("/{batch_id}/images")
def get_batch_images(batch_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    svc = PrimaryBatchService(db, shop)
    batch = svc.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    images = svc.get_batch_images(batch_id)
    return SuccessEnvelope(
        success=True,
        message="Batch images retrieved successfully.",
        requestId=_request_id(request),
        data={
            "batchId": str(batch_id),
            "items": [_image_out(i).model_dump() for i in images],
        },
    )


@router.get("/images/{image_id}/output")
def get_batch_image_output(image_id: UUID, db: DbSession, shop: CurrentShop):
    image = (
        db.query(BatchImage)
        .filter(BatchImage.id == image_id, BatchImage.shop_id == shop.id)
        .one_or_none()
    )
    if image is None:
        raise HTTPException(status_code=404, detail="Batch image not found")

    # Prefer durable Shopify CDN after local temp cleanup.
    if image.generated_shopify_cdn_url:
        return RedirectResponse(
            url=image.generated_shopify_cdn_url,
            status_code=302,
            headers={"Cache-Control": "private, max-age=300"},
        )

    if not image.output_storage_key:
        raise HTTPException(status_code=404, detail="Processed output not available yet")

    storage = get_output_storage()
    try:
        path = storage.resolve_path(image.output_storage_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid output storage key") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Processed output file missing")

    return FileResponse(
        path=str(path),
        media_type=image.output_mime_type or "image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/{batch_id}/retry-failed")
def retry_failed_batch_products(
    batch_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PrimaryBatchService(db, shop)
    if svc.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    products = RetryService(db).manual_retry_failed_products(
        shop_id=shop.id,
        batch_id=batch_id,
    )
    return SuccessEnvelope(
        success=True,
        message="Failed batch products queued for retry.",
        requestId=_request_id(request),
        data={
            "batchId": str(batch_id),
            "retriedCount": len(products),
            "productIds": [str(p.id) for p in products],
        },
    )


def _reprocess_http(exc: ReprocessError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/{batch_id}/reprocess/preview")
def preview_batch_reprocess(
    batch_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    try:
        data = ReprocessService(db, shop).preview_for_batch(batch_id)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Batch reprocess prompt preview.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/{batch_id}/reprocess")
def reprocess_batch(
    batch_id: UUID,
    payload: ReprocessRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    steps = [s.model_dump(exclude_none=True) for s in payload.steps] if payload.steps is not None else None
    try:
        data = ReprocessService(db, shop).reprocess_batch(batch_id, steps=steps)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Batch queued for reprocess.",
        requestId=_request_id(request),
        data=data,
    )


@router.get("/products/{product_id}/reprocess/preview")
def preview_product_reprocess(
    product_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    try:
        data = ReprocessService(db, shop).preview_for_product(product_id)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Product reprocess prompt preview.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/products/{product_id}/reprocess")
def reprocess_product(
    product_id: UUID,
    payload: ReprocessRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    steps = [s.model_dump(exclude_none=True) for s in payload.steps] if payload.steps is not None else None
    try:
        data = ReprocessService(db, shop).reprocess_product(product_id, steps=steps)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Product queued for reprocess.",
        requestId=_request_id(request),
        data=data,
    )


@router.get("/images/{image_id}/reprocess/preview")
def preview_image_reprocess(
    image_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    try:
        data = ReprocessService(db, shop).preview_for_image(image_id)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Image reprocess prompt preview.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/images/{image_id}/reprocess")
def reprocess_image(
    image_id: UUID,
    payload: ReprocessRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    steps = [s.model_dump(exclude_none=True) for s in payload.steps] if payload.steps is not None else None
    try:
        data = ReprocessService(db, shop).reprocess_image(image_id, steps=steps)
    except ReprocessError as exc:
        raise _reprocess_http(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Image queued for reprocess.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/products/{product_id}/retry")
def retry_batch_product(
    product_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    products = RetryService(db).manual_retry_failed_products(
        shop_id=shop.id,
        product_ids=[product_id],
    )
    if not products:
        raise HTTPException(status_code=404, detail="Failed batch product not found")

    return SuccessEnvelope(
        success=True,
        message="Batch product queued for retry.",
        requestId=_request_id(request),
        data=_product_out(products[0]).model_dump(),
    )

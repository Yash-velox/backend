"""Shopify product publishing API endpoints."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.core.deps import CurrentShop, DbSession
from app.models import BatchProduct, ProductPublishOperation, PublishStatus, PublishTriggerSource
from app.schemas.week2 import SuccessEnvelope
from app.services.publish_trigger import PublishEnqueueError, PublishTriggerService, RETRYABLE_PUBLISH_STATUSES

router = APIRouter(tags=["publishing"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _safe_assets(assets: list | None) -> list[dict]:
    out: list[dict] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        out.append(
            {
                "batchImageId": asset.get("batch_image_id"),
                "processedFilename": asset.get("processed_filename"),
                "uploadStatus": asset.get("upload_status"),
                "shopifyFileStatus": asset.get("shopify_file_status"),
                "associationStatus": asset.get("association_status"),
                "targetPosition": asset.get("target_position"),
                "targetAltText": asset.get("target_alt_text"),
            }
        )
    return out


def _operation_out(op: ProductPublishOperation) -> dict:
    return {
        "operationId": str(op.id),
        "batchProductId": str(op.batch_product_id),
        "processingBatchId": str(op.processing_batch_id),
        "shopifyProductGid": op.shopify_product_gid,
        "status": op.status.value,
        "currentStage": op.current_stage,
        "triggerSource": op.trigger_source.value,
        "attemptNumber": op.attempt_number,
        "outputSetChecksum": op.output_set_checksum,
        "lastErrorCode": op.last_error_code,
        "lastErrorMessage": op.last_error_message,
        "conflictDetails": op.conflict_details,
        "assets": _safe_assets(op.assets_json),
        "queuedAt": op.queued_at.isoformat() if op.queued_at else None,
        "startedAt": op.started_at.isoformat() if op.started_at else None,
        "completedAt": op.completed_at.isoformat() if op.completed_at else None,
        "publishedAt": op.published_at.isoformat() if op.published_at else None,
        "createdAt": op.created_at.isoformat() if op.created_at else None,
        "updatedAt": op.updated_at.isoformat() if op.updated_at else None,
    }


@router.post("/api/batches/products/{batch_product_id}/publish", status_code=status.HTTP_202_ACCEPTED)
def publish_product(batch_product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        result = PublishTriggerService(db, shop).enqueue_product(
            batch_product_id,
            trigger=PublishTriggerSource.MANUAL,
        )
    except PublishEnqueueError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    return SuccessEnvelope(
        success=True,
        message=result.get("message") or "Product publishing has been queued.",
        requestId=_request_id(request),
        data=result,
    )


@router.post("/api/batches/{batch_id}/publish-ready", status_code=status.HTTP_202_ACCEPTED)
def publish_ready(batch_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        counts = PublishTriggerService(db, shop).enqueue_ready_for_batch(
            batch_id,
            trigger=PublishTriggerSource.MANUAL,
        )
    except PublishEnqueueError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    return SuccessEnvelope(
        success=True,
        message="Ready products have been queued for publishing.",
        requestId=_request_id(request),
        data=counts,
    )


@router.get("/api/batches/products/{batch_product_id}/publish-status")
def publish_status(batch_product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    product = (
        db.query(BatchProduct)
        .filter(BatchProduct.id == batch_product_id, BatchProduct.shop_id == shop.id)
        .one_or_none()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Batch product not found")

    op = (
        db.query(ProductPublishOperation)
        .filter(
            ProductPublishOperation.batch_product_id == product.id,
            ProductPublishOperation.shop_id == shop.id,
        )
        .order_by(ProductPublishOperation.created_at.desc())
        .first()
    )
    data = {
        "batchProductId": str(product.id),
        "publishStatus": product.publish_status.value if product.publish_status else None,
        "operation": _operation_out(op) if op else None,
    }
    return SuccessEnvelope(
        success=True,
        message="Publish status retrieved.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/api/batches/products/{batch_product_id}/retry-publish", status_code=status.HTTP_202_ACCEPTED)
def retry_publish(batch_product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    product = (
        db.query(BatchProduct)
        .filter(BatchProduct.id == batch_product_id, BatchProduct.shop_id == shop.id)
        .one_or_none()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Batch product not found")

    op = (
        db.query(ProductPublishOperation)
        .filter(
            ProductPublishOperation.batch_product_id == product.id,
            ProductPublishOperation.shop_id == shop.id,
        )
        .order_by(ProductPublishOperation.created_at.desc())
        .first()
    )
    if not op or op.status not in RETRYABLE_PUBLISH_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PUBLISH_PRODUCT_NOT_PROCESSED",
                "message": "No failed or conflicted publish operation to retry",
            },
        )
    try:
        result = PublishTriggerService(db, shop).enqueue_product(
            batch_product_id,
            trigger=PublishTriggerSource.RETRY,
            force_retry=True,
        )
    except PublishEnqueueError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    return SuccessEnvelope(
        success=True,
        message=result.get("message") or "Publish retry queued.",
        requestId=_request_id(request),
        data=result,
    )


@router.get("/api/batches/products/{batch_product_id}/publish-conflict")
def publish_conflict(batch_product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    product = (
        db.query(BatchProduct)
        .filter(BatchProduct.id == batch_product_id, BatchProduct.shop_id == shop.id)
        .one_or_none()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Batch product not found")

    op = (
        db.query(ProductPublishOperation)
        .filter(
            ProductPublishOperation.batch_product_id == product.id,
            ProductPublishOperation.shop_id == shop.id,
            ProductPublishOperation.status == PublishStatus.PUBLISH_CONFLICT,
        )
        .order_by(ProductPublishOperation.created_at.desc())
        .first()
    )
    if not op:
        raise HTTPException(status_code=404, detail="No publish conflict for this product")

    return SuccessEnvelope(
        success=True,
        message="Publish conflict details retrieved.",
        requestId=_request_id(request),
        data={
            "batchProductId": str(product.id),
            "operationId": str(op.id),
            "status": op.status.value,
            "conflictDetails": op.conflict_details,
            "message": (
                "Shopify product media changed during processing. "
                "Sync the catalog and process this product again before publishing."
            ),
        },
    )


@router.get("/api/publish-operations/{operation_id}")
def get_publish_operation(operation_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    op = (
        db.query(ProductPublishOperation)
        .filter(ProductPublishOperation.id == operation_id, ProductPublishOperation.shop_id == shop.id)
        .one_or_none()
    )
    if not op:
        raise HTTPException(status_code=404, detail="Publish operation not found")
    return SuccessEnvelope(
        success=True,
        message="Publish operation retrieved.",
        requestId=_request_id(request),
        data=_operation_out(op),
    )

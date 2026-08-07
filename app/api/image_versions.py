"""Normalized per-image Shopify CDN version APIs (under product-level versions)."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.deps import CurrentShop, DbSession
from app.models import BatchImage, ImageVersion, ImageVersionEvent
from app.schemas.week2 import SuccessEnvelope
from app.services.image_processor import ImageProcessor, ProcessingError
from app.services.image_versions import ImageVersionError, ImageVersionsService

router = APIRouter(tags=["image-versions"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _image_version_out(version: ImageVersion) -> dict:
    return {
        "versionId": str(version.id),
        "productId": str(version.product_id),
        "sourceMediaGid": version.source_media_gid,
        "parentVersionId": str(version.parent_version_id) if version.parent_version_id else None,
        "versionNumber": version.version_number,
        "versionType": version.version_type.value,
        "shopifyFileGid": version.shopify_file_gid,
        "shopifyMediaGid": version.shopify_media_gid,
        "shopifyCdnUrl": version.shopify_cdn_url,
        "originalFilename": version.original_filename,
        "storedFilename": version.stored_filename,
        "mimeType": version.mime_type,
        "fileSizeBytes": version.file_size_bytes,
        "width": version.width,
        "height": version.height,
        "checksum": version.checksum,
        "isCurrent": version.is_current,
        "isPublished": version.is_published,
        "isOriginal": version.is_original,
        "isProtected": version.is_protected,
        "createdByBatchId": str(version.created_by_batch_id) if version.created_by_batch_id else None,
        "createdByBatchImageId": str(version.created_by_batch_image_id)
        if version.created_by_batch_image_id
        else None,
        "productMediaVersionId": str(version.product_media_version_id)
        if version.product_media_version_id
        else None,
        "publishedAt": version.published_at.isoformat() if version.published_at else None,
        "supersededAt": version.superseded_at.isoformat() if version.superseded_at else None,
        "createdAt": version.created_at.isoformat() if version.created_at else None,
        "metadata": version.metadata_json,
    }


def _event_out(event: ImageVersionEvent) -> dict:
    return {
        "eventId": str(event.id),
        "imageVersionId": str(event.image_version_id) if event.image_version_id else None,
        "eventType": event.event_type.value,
        "previousVersionId": str(event.previous_version_id) if event.previous_version_id else None,
        "newVersionId": str(event.new_version_id) if event.new_version_id else None,
        "batchId": str(event.batch_id) if event.batch_id else None,
        "productMediaVersionId": str(event.product_media_version_id)
        if event.product_media_version_id
        else None,
        "actorType": event.actor_type,
        "actorId": event.actor_id,
        "details": event.details_json,
        "createdAt": event.created_at.isoformat() if event.created_at else None,
    }


def _http_from_iv(exc: ImageVersionError | ProcessingError) -> HTTPException:
    code = exc.code
    status_code = 400
    if code in {"VERSION_NOT_FOUND", "BATCH_IMAGE_NOT_FOUND", "BATCH_PRODUCT_NOT_FOUND"}:
        status_code = 404
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(exc)})


@router.get("/api/products/{product_id}/image-versions")
def list_image_versions(
    product_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    sourceMediaGid: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows, total = ImageVersionsService(db, shop).list_for_product(
        product_id,
        source_media_gid=sourceMediaGid,
        limit=limit,
        offset=offset,
    )
    return SuccessEnvelope(
        success=True,
        message="Image versions.",
        requestId=_request_id(request),
        data={
            "items": [_image_version_out(v) for v in rows],
            "count": len(rows),
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/api/products/{product_id}/image-versions/{version_id}")
def get_image_version(product_id: UUID, version_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        version = ImageVersionsService(db, shop).get_version(product_id, version_id)
    except ImageVersionError as exc:
        raise _http_from_iv(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Image version detail.",
        requestId=_request_id(request),
        data=_image_version_out(version),
    )


@router.get("/api/products/{product_id}/image-versions/{version_id}/events")
def list_image_version_events(
    product_id: UUID,
    version_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        rows, total = ImageVersionsService(db, shop).list_events(
            product_id, version_id, limit=limit, offset=offset
        )
    except ImageVersionError as exc:
        raise _http_from_iv(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Image version events.",
        requestId=_request_id(request),
        data={
            "items": [_event_out(e) for e in rows],
            "count": len(rows),
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/api/shops/me/image-storage-summary")
def image_storage_summary(request: Request, db: DbSession, shop: CurrentShop):
    summary = ImageVersionsService(db, shop).storage_summary()
    return SuccessEnvelope(
        success=True,
        message="Estimated image version storage summary.",
        requestId=_request_id(request),
        data=summary,
    )


@router.post(
    "/api/batches/images/{image_id}/retry-upload",
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_image_upload(image_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    image = (
        db.query(BatchImage)
        .filter(BatchImage.id == image_id, BatchImage.shop_id == shop.id)
        .one_or_none()
    )
    if not image:
        raise HTTPException(
            status_code=404,
            detail={"code": "BATCH_IMAGE_NOT_FOUND", "message": "Batch image not found"},
        )
    try:
        updated = ImageProcessor(db).retry_upload_only(image_id, worker_id="api")
    except ProcessingError as exc:
        raise _http_from_iv(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Shopify Files upload retry completed or rescheduled.",
        requestId=_request_id(request),
        data={
            "imageId": str(updated.id),
            "status": updated.status.value,
            "generatedShopifyFileGid": updated.generated_shopify_file_gid,
            "generatedImageVersionId": str(updated.generated_image_version_id)
            if updated.generated_image_version_id
            else None,
            "errorCode": updated.error_code,
            "errorMessage": updated.error_message,
        },
    )

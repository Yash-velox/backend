"""Product media version history and rollback APIs."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentShop, DbSession
from app.models import ProductMediaVersion, ProductRollbackOperation
from app.schemas.week2 import ReprocessPromptStepIn, SuccessEnvelope
from app.services.media_versions import MediaVersionError, MediaVersionsService
from app.services.primary_batch import PrimaryBatchService
from app.services.product_rollback import RollbackError, ProductRollbackService
from app.services.reprocess_service import ReprocessError, ReprocessService

router = APIRouter(tags=["media-versions"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


class RollbackConfirmBody(BaseModel):
    confirm: bool = Field(default=False)
    forceDespiteConflict: bool = Field(
        default=False,
        description="When true, proceed even if live Shopify media differs from the active version.",
    )


class RollbackRetryBody(BaseModel):
    forceDespiteConflict: bool = Field(
        default=False,
        description="When true, retry and skip the conflict hard-stop (still records conflict details).",
    )


class LiveReprocessBody(BaseModel):
    mediaGids: list[str] = Field(min_length=1)
    steps: list[ReprocessPromptStepIn] | None = None


def _cdn_lookup_from_linked(linked_images: list | None) -> dict[str, str]:
    """Map Shopify file/media GIDs → CDN URL from linked image_versions."""
    out: dict[str, str] = {}
    for iv in linked_images or []:
        if not isinstance(iv, dict):
            continue
        cdn = iv.get("shopifyCdnUrl") or iv.get("shopify_cdn_url")
        if not cdn:
            continue
        for key in (
            iv.get("shopifyFileGid"),
            iv.get("shopify_file_gid"),
            iv.get("shopifyMediaGid"),
            iv.get("shopify_media_gid"),
            iv.get("sourceMediaGid"),
            iv.get("source_media_gid"),
        ):
            if key:
                out[str(key)] = str(cdn)
    return out


def _version_out(version: ProductMediaVersion, *, include_items: bool = False, linked_images: list | None = None) -> dict:
    items = version.items_json or {}
    media = items.get("media") or []
    cdn_by_gid = _cdn_lookup_from_linked(linked_images)
    data = {
        "versionId": str(version.id),
        "productId": str(version.product_id),
        "shopifyProductGid": version.shopify_product_gid,
        "versionNumber": version.version_number,
        "versionType": version.version_type.value,
        "sourceVersionId": str(version.source_version_id) if version.source_version_id else None,
        "processingBatchId": str(version.processing_batch_id) if version.processing_batch_id else None,
        "publishOperationId": str(version.publish_operation_id) if version.publish_operation_id else None,
        "rollbackOperationId": str(version.rollback_operation_id) if version.rollback_operation_id else None,
        "isActive": version.is_active,
        "rollbackEligible": version.rollback_eligible,
        "unavailableReason": version.unavailable_reason,
        "snapshotHash": version.snapshot_hash,
        "imageCount": len(media),
        "modelName": version.model_name,
        "quality": version.quality,
        "createdBy": version.created_by,
        "activatedAt": version.activated_at.isoformat() if version.activated_at else None,
        "createdAt": version.created_at.isoformat() if version.created_at else None,
        "updatedAt": version.updated_at.isoformat() if version.updated_at else None,
    }
    if include_items:
        data["media"] = []
        for m in media:
            file_gid = m.get("file_gid")
            media_gid = m.get("media_gid")
            cdn = (
                m.get("cdn_url")
                or (cdn_by_gid.get(str(file_gid)) if file_gid else None)
                or (cdn_by_gid.get(str(media_gid)) if media_gid else None)
            )
            data["media"].append(
                {
                    "mediaGid": media_gid,
                    "fileGid": file_gid,
                    "position": m.get("position"),
                    "isPrimary": m.get("is_primary"),
                    "altText": m.get("alt_text"),
                    "cdnUrl": cdn,
                    "filename": m.get("filename"),
                    "width": m.get("width"),
                    "height": m.get("height"),
                    "mimeType": m.get("mime_type"),
                }
            )
        data["variants"] = items.get("variants") or []
        data["promptSnapshot"] = version.prompt_snapshot_json
        if linked_images is not None:
            data["linkedImageVersions"] = linked_images
    return data


def _rollback_out(op: ProductRollbackOperation) -> dict:
    return {
        "operationId": str(op.id),
        "productId": str(op.product_id),
        "shopifyProductGid": op.shopify_product_gid,
        "fromVersionId": str(op.from_version_id),
        "targetVersionId": str(op.target_version_id),
        "resultVersionId": str(op.result_version_id) if op.result_version_id else None,
        "status": op.status.value,
        "currentStage": op.current_stage,
        "attemptNumber": op.attempt_number,
        "lastErrorCode": op.last_error_code,
        "lastErrorMessage": op.last_error_message,
        "conflictDetails": op.conflict_details,
        "forceDespiteConflict": bool(getattr(op, "force_despite_conflict", False)),
        "queuedAt": op.queued_at.isoformat() if op.queued_at else None,
        "startedAt": op.started_at.isoformat() if op.started_at else None,
        "completedAt": op.completed_at.isoformat() if op.completed_at else None,
        "createdAt": op.created_at.isoformat() if op.created_at else None,
        "updatedAt": op.updated_at.isoformat() if op.updated_at else None,
    }


def _http_from_media(exc: MediaVersionError | RollbackError) -> HTTPException:
    code = exc.code
    status_code = 400
    if code in {"VERSION_NOT_FOUND"}:
        status_code = 404
    elif code in {
        "PUBLISH_ALREADY_ACTIVE",
        "ROLLBACK_ALREADY_ACTIVE",
        "VERSION_ALREADY_ACTIVE",
        "ROLLBACK_CONFLICT",
    }:
        status_code = 409
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(exc)})


def _http_from_reprocess(exc: ReprocessError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/api/products/media-versions")
def search_products_with_versions(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    items = MediaVersionsService(db, shop).search_products_with_versions(search, limit=limit)
    return SuccessEnvelope(
        success=True,
        message="Products with media versions.",
        requestId=_request_id(request),
        data={"items": items, "count": len(items)},
    )


@router.get("/api/products/{product_id}/media-versions")
def list_media_versions(product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        PrimaryBatchService(db, shop).refresh_catalog_product(product_id)
        versions = MediaVersionsService(db, shop).list_versions(product_id)
        live_media = MediaVersionsService(db, shop).live_media(product_id)
    except MediaVersionError as exc:
        raise _http_from_media(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Media versions.",
        requestId=_request_id(request),
        data={
            "items": [_version_out(v) for v in versions],
            "liveMedia": live_media,
            "count": len(versions),
        },
    )


@router.get("/api/products/{product_id}/media-versions/{version_id}")
def get_media_version(product_id: UUID, version_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        version = MediaVersionsService(db, shop).get_version(product_id, version_id)
    except MediaVersionError as exc:
        raise _http_from_media(exc) from exc
    linked = []
    try:
        from app.services.image_versions import ImageVersionsService

        for iv in ImageVersionsService(db, shop).versions_for_product_media_version(version.id):
            linked.append(
                {
                    "versionId": str(iv.id),
                    "sourceMediaGid": iv.source_media_gid,
                    "versionNumber": iv.version_number,
                    "versionType": iv.version_type.value,
                    "shopifyFileGid": iv.shopify_file_gid,
                    "shopifyMediaGid": iv.shopify_media_gid,
                    "shopifyCdnUrl": iv.shopify_cdn_url,
                    "fileSizeBytes": iv.file_size_bytes,
                    "width": iv.width,
                    "height": iv.height,
                    "isCurrent": iv.is_current,
                    "isPublished": iv.is_published,
                    "isOriginal": iv.is_original,
                }
            )
    except Exception:
        linked = []
    return SuccessEnvelope(
        success=True,
        message="Media version detail.",
        requestId=_request_id(request),
        data=_version_out(version, include_items=True, linked_images=linked),
    )


@router.get("/api/products/{product_id}/live-reprocess/preview")
def preview_live_reprocess(product_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        data = ReprocessService(db, shop).preview_live(product_id)
    except ReprocessError as exc:
        raise _http_from_reprocess(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Live reprocess prompt preview.",
        requestId=_request_id(request),
        data=data,
    )


@router.post("/api/products/{product_id}/live-reprocess", status_code=status.HTTP_202_ACCEPTED)
def start_live_reprocess(
    product_id: UUID,
    body: LiveReprocessBody,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    try:
        steps = [s.model_dump(exclude_none=True) for s in body.steps] if body.steps is not None else None
        data = ReprocessService(db, shop).reprocess_live(
            product_id,
            media_gids=body.mediaGids,
            steps=steps,
        )
    except ReprocessError as exc:
        raise _http_from_reprocess(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Selected live images queued for reprocess. They will publish automatically when processing finishes.",
        requestId=_request_id(request),
        data=data,
    )


@router.get("/api/products/{product_id}/media-versions/{version_id}/rollback-preview")
def rollback_preview(product_id: UUID, version_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    try:
        preview = ProductRollbackService(db, shop).rollback_preview(product_id, version_id)
    except (MediaVersionError, RollbackError) as exc:
        raise _http_from_media(exc) from exc
    return SuccessEnvelope(
        success=True,
        message="Rollback preview.",
        requestId=_request_id(request),
        data=preview,
    )


@router.post(
    "/api/products/{product_id}/media-versions/{version_id}/rollback",
    status_code=status.HTTP_202_ACCEPTED,
)
def start_rollback(
    product_id: UUID,
    version_id: UUID,
    body: RollbackConfirmBody,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    try:
        result = ProductRollbackService(db, shop).enqueue(
            product_id=product_id,
            target_version_id=version_id,
            confirm=body.confirm,
            force_despite_conflict=body.forceDespiteConflict,
        )
    except (MediaVersionError, RollbackError) as exc:
        raise _http_from_media(exc) from exc
    return SuccessEnvelope(
        success=True,
        message=result.get("message") or "Rollback queued.",
        requestId=_request_id(request),
        data=result,
    )


@router.get("/api/rollback-operations/{operation_id}")
def get_rollback_operation(operation_id: UUID, request: Request, db: DbSession, shop: CurrentShop):
    op = (
        db.query(ProductRollbackOperation)
        .filter(
            ProductRollbackOperation.id == operation_id,
            ProductRollbackOperation.shop_id == shop.id,
        )
        .one_or_none()
    )
    if not op:
        raise HTTPException(
            status_code=404,
            detail={"code": "VERSION_NOT_FOUND", "message": "Rollback operation not found"},
        )
    return SuccessEnvelope(
        success=True,
        message="Rollback operation status.",
        requestId=_request_id(request),
        data=_rollback_out(op),
    )


@router.post("/api/rollback-operations/{operation_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_rollback(
    operation_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    body: RollbackRetryBody | None = None,
):
    payload = body or RollbackRetryBody()
    try:
        result = ProductRollbackService(db, shop).retry(
            operation_id,
            force_despite_conflict=payload.forceDespiteConflict,
        )
    except RollbackError as exc:
        raise _http_from_media(exc) from exc
    return SuccessEnvelope(
        success=True,
        message=result.get("message") or "Rollback retry queued.",
        requestId=_request_id(request),
        data=result,
    )

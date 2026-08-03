from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from app.core.deps import CurrentShop, DbSession
from app.schemas.week2 import SettingsOut, SettingsUpdateRequest, SuccessEnvelope
from app.services.settings_service import SettingsService, SettingsValidationError

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _settings_out(row) -> SettingsOut:
    return SettingsOut(
        autoSyncEnabled=row.auto_sync_enabled,
        autoPublishProcessedImages=bool(getattr(row, "auto_publish_processed_images", False)),
        batchIntervalMinutes=row.batch_interval_minutes,
        createdAt=row.created_at,
        updatedAt=row.updated_at,
    )


@router.get("")
def get_settings(request: Request, db: DbSession, shop: CurrentShop):
    row = SettingsService(db, shop).get()
    return SuccessEnvelope(
        success=True,
        message="Settings retrieved successfully.",
        requestId=_request_id(request),
        data=_settings_out(row).model_dump(),
    )


@router.put("")
@router.patch("")
def update_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = SettingsService(db, shop)
    try:
        row = svc.update(
            auto_sync_enabled=payload.autoSyncEnabled,
            auto_publish_processed_images=payload.autoPublishProcessedImages,
            batch_interval_minutes=payload.batchIntervalMinutes,
        )
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SuccessEnvelope(
        success=True,
        message="Settings updated successfully.",
        requestId=_request_id(request),
        data=_settings_out(row).model_dump(),
    )

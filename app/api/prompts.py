"""Prompt Management API — product types and sequential prompt steps."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.deps import CurrentShop, DbSession
from app.models import PromptProductTypeSource
from app.schemas.prompts import (
    ConfigurationUpdateRequest,
    ManualProductTypeCreateRequest,
    PromptConfigurationDetailOut,
    PromptStepCreateRequest,
    PromptStepOut,
    PromptStepStatusRequest,
    PromptStepsReorderRequest,
    PromptStepUpdateRequest,
)
from app.schemas.week2 import SuccessEnvelope
from app.services.prompt_configuration import PromptConfigurationError, PromptConfigurationService
from app.services.prompt_product_types import (
    PromptProductTypeError,
    PromptProductTypeService,
    compute_list_status,
)
from app.services.prompt_variables import extract_variables, list_supported_variables

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _raise_domain(exc: PromptProductTypeError | PromptConfigurationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _step_out(step) -> dict:
    return PromptStepOut(
        id=step.id,
        name=step.name,
        promptText=step.prompt_text,
        stepOrder=step.step_order,
        stepType=getattr(step.step_type, "value", None) or getattr(step, "step_type", None) or "IMAGE",
        isEnabled=step.is_enabled,
        variables=extract_variables(step.prompt_text),
        createdAt=step.created_at,
        updatedAt=step.updated_at,
    ).model_dump(mode="json")


@router.get("/variables")
def get_variables(request: Request, shop: CurrentShop):
    del shop
    return SuccessEnvelope(
        success=True,
        message="Supported prompt variables.",
        requestId=_request_id(request),
        data={"items": list_supported_variables()},
    )


@router.get("/product-types")
def list_product_types(
    request: Request,
    db: DbSession,
    shop: CurrentShop,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100, alias="pageSize"),
):
    items, total = PromptProductTypeService(db, shop).list(
        search=search,
        status=status,
        page=page,
        page_size=page_size,
    )
    return SuccessEnvelope(
        success=True,
        message="Prompt product types retrieved successfully.",
        requestId=_request_id(request),
        data={
            "items": [
                {
                    "id": str(i["id"]),
                    "name": i["name"],
                    "source": i["source"],
                    "stepCount": i["stepCount"],
                    "enabledStepCount": i["enabledStepCount"],
                    "status": i["status"],
                    "isEnabled": i["isEnabled"],
                    "updatedAt": i["updatedAt"].isoformat() if i["updatedAt"] else None,
                    "createdAt": i["createdAt"].isoformat() if i["createdAt"] else None,
                }
                for i in items
            ],
            "total": total,
            "page": page,
            "pageSize": page_size,
        },
    )


@router.post("/product-types")
def create_product_type(
    payload: ManualProductTypeCreateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptProductTypeService(db, shop)
    try:
        row = svc.add_manual(payload.name)
    except PromptProductTypeError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Product type created successfully.",
        requestId=_request_id(request),
        data={
            "id": str(row.id),
            "name": row.name,
            "source": row.source.value if isinstance(row.source, PromptProductTypeSource) else row.source,
            "stepCount": 0,
            "enabledStepCount": 0,
            "status": "NOT_CONFIGURED",
            "isEnabled": True,
            "updatedAt": None,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        },
    )


@router.get("/product-types/{product_type_id}")
def get_product_type(
    product_type_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        product_type, config = svc.get_detail(product_type_id)
    except PromptProductTypeError as exc:
        _raise_domain(exc)
        raise

    steps = sorted(config.steps or [], key=lambda s: s.step_order)
    step_count = len(steps)
    enabled_step_count = sum(1 for s in steps if s.is_enabled)
    status = compute_list_status(
        step_count=step_count,
        enabled_step_count=enabled_step_count,
        is_enabled=config.is_enabled,
    )
    detail = PromptConfigurationDetailOut(
        id=config.id,
        productTypeId=product_type.id,
        name=product_type.name,
        source=product_type.source.value
        if isinstance(product_type.source, PromptProductTypeSource)
        else str(product_type.source),
        isEnabled=config.is_enabled,
        status=status.value,
        stepCount=step_count,
        enabledStepCount=enabled_step_count,
        steps=[
            PromptStepOut(
                id=s.id,
                name=s.name,
                promptText=s.prompt_text,
                stepOrder=s.step_order,
                isEnabled=s.is_enabled,
                variables=extract_variables(s.prompt_text),
                createdAt=s.created_at,
                updatedAt=s.updated_at,
            )
            for s in steps
        ],
        createdAt=config.created_at,
        updatedAt=config.updated_at,
    )
    return SuccessEnvelope(
        success=True,
        message="Prompt configuration retrieved successfully.",
        requestId=_request_id(request),
        data=detail.model_dump(mode="json"),
    )


@router.patch("/product-types/{product_type_id}/configuration")
def update_configuration(
    product_type_id: UUID,
    payload: ConfigurationUpdateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        config = svc.set_enabled(product_type_id, payload.isEnabled)
    except PromptProductTypeError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Configuration updated successfully.",
        requestId=_request_id(request),
        data={"id": str(config.id), "isEnabled": config.is_enabled},
    )


@router.post("/product-types/{product_type_id}/steps")
def add_step(
    product_type_id: UUID,
    payload: PromptStepCreateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        step = svc.add_step(
            product_type_id,
            name=payload.name,
            prompt_text=payload.promptText,
            is_enabled=payload.isEnabled,
            step_type=payload.stepType,
        )
    except (PromptProductTypeError, PromptConfigurationError) as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Prompt step created successfully.",
        requestId=_request_id(request),
        data=_step_out(step),
    )


@router.put("/product-types/{product_type_id}/steps/reorder")
def reorder_steps(
    product_type_id: UUID,
    payload: PromptStepsReorderRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        steps = svc.reorder_steps(product_type_id, payload.stepIds)
    except (PromptProductTypeError, PromptConfigurationError) as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Prompt steps reordered successfully.",
        requestId=_request_id(request),
        data={"items": [_step_out(s) for s in steps]},
    )


@router.delete("/product-types/{product_type_id}")
def delete_product_type(
    product_type_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptProductTypeService(db, shop)
    try:
        svc.delete_manual(product_type_id)
    except PromptProductTypeError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Product type deleted successfully.",
        requestId=_request_id(request),
        data={"id": str(product_type_id)},
    )


@router.put("/steps/{step_id}")
def update_step(
    step_id: UUID,
    payload: PromptStepUpdateRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        step = svc.update_step(
            step_id,
            name=payload.name,
            prompt_text=payload.promptText,
            is_enabled=payload.isEnabled,
            step_type=payload.stepType,
        )
    except PromptConfigurationError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Prompt step updated successfully.",
        requestId=_request_id(request),
        data=_step_out(step),
    )


@router.patch("/steps/{step_id}/status")
def update_step_status(
    step_id: UUID,
    payload: PromptStepStatusRequest,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        step = svc.set_step_status(step_id, payload.isEnabled)
    except PromptConfigurationError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Prompt step status updated successfully.",
        requestId=_request_id(request),
        data=_step_out(step),
    )


@router.delete("/steps/{step_id}")
def delete_step(
    step_id: UUID,
    request: Request,
    db: DbSession,
    shop: CurrentShop,
):
    svc = PromptConfigurationService(db, shop)
    try:
        svc.delete_step(step_id)
    except PromptConfigurationError as exc:
        _raise_domain(exc)
        raise
    return SuccessEnvelope(
        success=True,
        message="Prompt step deleted successfully.",
        requestId=_request_id(request),
        data={"id": str(step_id)},
    )

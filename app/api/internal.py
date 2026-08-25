from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.crypto import verify_internal_signature, verify_shopify_webhook_hmac
from app.core.deps import DbSession
from app.core.shop_resolver import get_shop_by_domain, mark_shop_uninstalled, upsert_shop_install
from app.schemas.week2 import SuccessEnvelope
from app.services.secondary_queue import purge_processed_secondary_queue_items
from app.services.webhook_intake import WebhookIntakeService

router = APIRouter(prefix="/internal", tags=["internal"])


class ShopInstallRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    shop: str
    accessToken: str
    scope: str | None = None
    refreshToken: str | None = None
    refreshTokenExpires: datetime | None = None
    # Access-token expiry (~24h for expiring offline tokens). Do NOT send refresh expiry here.
    accessTokenExpires: datetime | None = None


class ShopUninstallRequest(BaseModel):
    shop: str


class SecondaryQueuePurgeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Optional shop domain. When omitted, purge processed rows for every shop.
    shop: str | None = None


class WebhookProductsUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    shop: str | None = None
    topic: str | None = None
    webhookId: str | None = None
    payload: dict[str, Any] | None = None


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _require_internal_signature(request: Request, body: bytes) -> None:
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    if not verify_internal_signature(body, timestamp=timestamp, signature=signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal signature",
        )


def _require_webhook_signature(request: Request, body: bytes) -> None:
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    if timestamp and signature and verify_internal_signature(body, timestamp=timestamp, signature=signature):
        return

    shopify_hmac = request.headers.get("X-Shopify-Hmac-Sha256")
    if shopify_hmac and verify_shopify_webhook_hmac(body, shopify_hmac):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid webhook signature",
    )


def _parse_webhook_payload(
    request: Request,
    body: bytes,
    data: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any]]:
    from app.services.webhook_intake import normalize_webhook_topic

    if data.get("shop") and data.get("webhookId"):
        shop = str(data["shop"]).strip().lower()
        topic = normalize_webhook_topic(str(data.get("topic") or "products/update"))
        webhook_id = str(data["webhookId"])
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        return shop, topic, webhook_id, payload

    shop = (
        data.get("shop")
        or request.headers.get("X-Shopify-Shop-Domain")
        or request.headers.get("x-shopify-shop-domain")
    )
    if not shop:
        raise HTTPException(status_code=400, detail="Shop domain is required")

    topic = normalize_webhook_topic(
        request.headers.get("X-Shopify-Topic")
        or request.headers.get("x-shopify-topic")
        or "products/update"
    )
    webhook_id = (
        request.headers.get("X-Shopify-Webhook-Id")
        or request.headers.get("x-shopify-webhook-id")
        or request.headers.get("X-Shopify-Event-Id")
        or request.headers.get("x-shopify-event-id")
    )
    if not webhook_id:
        webhook_id = hashlib.sha256(body).hexdigest()

    return str(shop).strip().lower(), topic, webhook_id, data


@router.post("/shops/install")
async def shop_install(request: Request, db: DbSession):
    body = await request.body()
    _require_internal_signature(request, body)

    try:
        payload = ShopInstallRequest.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid install payload") from exc

    shop = upsert_shop_install(
        db,
        shop_domain=payload.shop,
        access_token=payload.accessToken,
        scopes=payload.scope,
        refresh_token=payload.refreshToken,
        token_expires_at=payload.accessTokenExpires,
    )
    return SuccessEnvelope(
        success=True,
        message="Shop installed successfully.",
        requestId=_request_id(request),
        data={"shop": shop.shop_domain, "shopId": str(shop.id), "status": shop.status.value},
    )


@router.post("/shops/uninstall")
async def shop_uninstall(request: Request, db: DbSession):
    body = await request.body()
    _require_internal_signature(request, body)

    try:
        payload = ShopUninstallRequest.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid uninstall payload") from exc

    shop = mark_shop_uninstalled(db, payload.shop)
    if shop is None:
        raise HTTPException(status_code=404, detail="Shop not found")

    return SuccessEnvelope(
        success=True,
        message="Shop uninstalled successfully.",
        requestId=_request_id(request),
        data={"shop": shop.shop_domain, "status": shop.status.value},
    )


@router.post("/secondary-queue/purge-processed")
async def purge_processed_secondary_queue(request: Request, db: DbSession):
    """Private ops API: delete converted / skipped / failed secondary-queue rows.

    Keeps PENDING (and in-flight CLAIMED). Optional body ``{"shop": "..."}`` scopes
    to one shop; omit shop to purge across all tenants.
    """
    body = await request.body()
    _require_internal_signature(request, body)

    shop_domain: str | None = None
    if body.strip():
        try:
            payload = SecondaryQueuePurgeRequest.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid purge payload") from exc
        shop_domain = payload.shop.strip().lower() if payload.shop else None

    shop_id = None
    if shop_domain:
        shop = get_shop_by_domain(db, shop_domain)
        if shop is None:
            raise HTTPException(status_code=404, detail="Shop not found")
        shop_id = shop.id

    stats = purge_processed_secondary_queue_items(db, shop_id=shop_id)
    return SuccessEnvelope(
        success=True,
        message="Processed secondary queue items purged successfully.",
        requestId=_request_id(request),
        data={
            "shop": shop_domain,
            "deleted": stats["deleted"],
            "converted": stats["converted"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "remaining": stats["remaining"],
            "pendingRemaining": stats["pendingRemaining"],
        },
    )


@router.post("/webhooks/products-update")
async def products_update_webhook(request: Request, db: DbSession):
    """Async intake for products/create and products/update (topic on event)."""
    body = await request.body()
    _require_webhook_signature(request, body)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be a JSON object")

    shop_domain, topic, webhook_id, payload = _parse_webhook_payload(request, body, data)
    raw_hash = hashlib.sha256(body).hexdigest()

    event = WebhookIntakeService(db).enqueue_products_update(
        shop_domain,
        webhook_id,
        topic,
        payload,
        raw_hash,
    )
    return SuccessEnvelope(
        success=True,
        message="Webhook accepted.",
        requestId=_request_id(request),
        data={
            "webhookId": webhook_id,
            "eventId": str(event.id),
            "topic": topic,
            "processingResult": event.processing_result.value,
            "shopifyProductGid": event.shopify_product_gid,
        },
    )

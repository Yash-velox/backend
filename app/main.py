from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.batches_v2 import router as batches_router
from app.api.image_versions import router as image_versions_router
from app.api.internal import router as internal_router
from app.api.products import router as products_router
from app.api.prompts import router as prompts_router
from app.api.publishing import router as publishing_router
from app.api.secondary_queue import router as secondary_queue_router
from app.api.settings import router as settings_router
from app.api.sync import router as sync_router
from app.api.versions import router as versions_router
from app.config import settings
from app.core.deps import CurrentShop, DbSession
from app.logging_setup import setup_logging
from app.poc.auth import require_shopify_jwt
from app.poc.router import router as poc_router
from app.services.webhook_intake import WebhookIntakeService
from app.workers.processing_worker import processing_worker
from app.workers.publish_worker import publish_worker

setup_logging()
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.auto_processing_enabled:
        await processing_worker.start()
        await publish_worker.start()
    else:
        logger.info("Automatic processing disabled - workers not started")
    try:
        yield
    finally:
        await publish_worker.stop()
        await processing_worker.stop()


app = FastAPI(
    title="Image Enhancement API",
    version="0.3.0",
    description="FastAPI backend for Shopify Image Enhancement",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_origin_regex=r"https://.*\.(trycloudflare\.com|ngrok-free\.(app|dev)|ngrok\.io)",
)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": f"HTTP_{exc.status_code}",
                "message": str(exc.detail),
                "retryable": exc.status_code >= 500,
                "request_id": _request_id(request),
                "details": {},
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # pragma: no cover
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unexpected server error",
                "retryable": True,
                "request_id": _request_id(request),
                "details": {},
            }
        },
    )


@app.get("/health")
@app.get("/health/live")
@app.get("/api/health/live")
def health_live():
    """Liveness: process is up. No DB, no workers."""
    return {
        "status": "ok",
        "service": "image-enhancement-api",
        "env": settings.app_env,
        "processingEnabled": settings.auto_processing_enabled,
    }


@app.get("/health/ready")
@app.get("/api/health/ready")
def health_ready(db: DbSession):
    """Readiness: database is reachable. Webhook backlog is reported, not a 503."""
    db.execute(text("SELECT 1"))
    webhooks = WebhookIntakeService(db).queue_metrics()
    return {
        "status": "ok",
        "db": "ok",
        "service": "image-enhancement-api",
        "env": settings.app_env,
        "processingEnabled": settings.auto_processing_enabled,
        "webhooks": webhooks,
    }


@app.get("/health/webhooks")
@app.get("/api/health/webhooks")
def health_webhooks(db: DbSession):
    """Webhook queue depth, lag, and retry counts for alerts."""
    metrics = WebhookIntakeService(db).queue_metrics()
    return {
        "status": "ok" if not metrics["alerts"] else "alert",
        "webhooks": metrics,
    }


@app.get("/tenant/checkConfig", dependencies=[Depends(require_shopify_jwt)])
def tenant_check_config(shop: CurrentShop):
    installed = bool(shop.encrypted_access_token)
    return {
        "status": "ok",
        "installed": installed,
        "shop": shop.shop_domain,
        "message": "Shop is installed and configured." if installed else "Shop install handoff pending.",
    }


app.include_router(poc_router)
app.include_router(internal_router)
app.include_router(sync_router)
app.include_router(settings_router)
app.include_router(secondary_queue_router)
app.include_router(batches_router)
app.include_router(publishing_router)
app.include_router(versions_router)
app.include_router(image_versions_router)
app.include_router(prompts_router)
app.include_router(products_router)

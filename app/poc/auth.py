from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
import jwt

from app.config import settings


def verify_shopify_jwt(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if settings.app_env == "dev" and settings.poc_dev_skip_auth:
            return {"sub": "dev-local", "dest": "dev-local.myshopify.com"}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = auth_header.replace("Bearer ", "", 1).strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )

    if not settings.shopify_api_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SHOPIFY_API_SECRET is not configured",
        )

    audience = settings.shopify_api_key if settings.shopify_api_key else None
    options = {"verify_aud": bool(audience)}

    try:
        payload = jwt.decode(
            token,
            settings.shopify_api_secret,
            algorithms=["HS256"],
            audience=audience,
            options=options,
        )
    except jwt.PyJWTError as exc:  # pragma: no cover - framework-level auth failure
        if settings.app_env == "dev" and settings.poc_dev_skip_auth:
            return {"sub": "dev-local", "dest": "dev-local.myshopify.com", "auth_note": "invalid_jwt_dev_bypass"}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session token: {exc}",
        ) from exc

    return payload


def require_shopify_jwt(payload: dict = Depends(verify_shopify_jwt)) -> dict:
    return payload

"""Refresh Shopify Admin API access tokens and persist them on shops."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token
from app.models import Shop

logger = logging.getLogger("app.services.shopify_token_refresh")

# Refresh a few minutes early so workers do not race the hard expiry.
_REFRESH_SKEW = timedelta(minutes=5)


class ShopifyTokenRefreshError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def access_token_needs_refresh(shop: Shop, *, now: datetime | None = None) -> bool:
    """True when shops.token_expires_at is near or past expiry."""
    if shop.token_expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    expires = shop.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= current + _REFRESH_SKEW


def _normalize_shop_domain(shop_domain: str) -> str:
    return shop_domain.replace("https://", "").replace("http://", "").rstrip("/").lower()


def _request_access_token(shop_domain: str, form: dict[str, str]) -> dict[str, Any]:
    domain = _normalize_shop_domain(shop_domain)
    url = f"https://{domain}/admin/oauth/access_token"
    try:
        response = httpx.post(
            url,
            content=urlencode(form),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
    except httpx.TimeoutException as exc:
        raise ShopifyTokenRefreshError("Shopify token refresh timed out", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise ShopifyTokenRefreshError(f"Shopify token refresh network error: {exc}", retryable=True) from exc

    if response.status_code >= 500 or response.status_code == 429:
        raise ShopifyTokenRefreshError(
            f"Shopify token refresh temporary error HTTP {response.status_code}",
            retryable=True,
        )
    if response.status_code >= 400:
        detail = (response.text or "")[:300]
        raise ShopifyTokenRefreshError(
            f"Shopify token refresh failed HTTP {response.status_code}: {detail}",
            retryable=False,
        )

    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ShopifyTokenRefreshError("Shopify token refresh response missing access_token")
    return payload


def refresh_shop_access_token(db: Session, shop: Shop, *, force: bool = False) -> Shop:
    """Refresh shop Admin API token when expired (or force) and write shops row."""
    # Local import avoids circular import with shop_resolver.
    from app.core.shop_resolver import upsert_shop_install

    if not force and not access_token_needs_refresh(shop):
        return shop

    client_id = (settings.shopify_api_key or "").strip()
    client_secret = (settings.shopify_api_secret or "").strip()
    if not client_id or not client_secret:
        raise ShopifyTokenRefreshError(
            "Cannot refresh Shopify access token: SHOPIFY_API_KEY/SHOPIFY_API_SECRET missing"
        )

    refresh_token = decrypt_token(shop.encrypted_refresh_token) if shop.encrypted_refresh_token else ""
    if refresh_token:
        form = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        grant = "refresh_token"
    else:
        # Custom-app / UAT client-credentials tokens expire ~24h and have no refresh_token.
        form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        grant = "client_credentials"

    logger.info(
        "Refreshing Shopify access token | shop=%s grant=%s force=%s",
        shop.shop_domain,
        grant,
        force,
    )
    payload = _request_access_token(shop.shop_domain, form)
    access_token = str(payload["access_token"]).strip()
    new_refresh = (payload.get("refresh_token") or "").strip() or None
    scope = (payload.get("scope") or shop.scopes or "") or None
    expires_in = payload.get("expires_in")
    expires_at = None
    if expires_in is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    return upsert_shop_install(
        db,
        shop_domain=shop.shop_domain,
        access_token=access_token,
        scopes=scope,
        refresh_token=new_refresh,
        token_expires_at=expires_at,
    )

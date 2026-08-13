from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models import Shop, ShopSettings, ShopStatus


def _dev_access_token_seed_allowed() -> bool:
    """Allow seeding shops from SHOPIFY_DEV_ACCESS_TOKEN only on first create (dev/uat).

    API callers must still resolve from the shops table - never read env at call time.
    """
    return settings.app_env in {"dev", "uat"} and bool(settings.shopify_dev_access_token)


def extract_shop_domain(jwt_payload: dict) -> str:
    dest = (jwt_payload.get("dest") or jwt_payload.get("iss") or "").strip()
    if dest.startswith("https://"):
        dest = dest[len("https://") :]
    if dest.startswith("http://"):
        dest = dest[len("http://") :]
    dest = dest.rstrip("/")
    if not dest:
        raise ValueError("Shop domain missing from session token")
    return dest.lower()


def ensure_shop_settings(db: Session, shop: Shop) -> ShopSettings:
    if shop.settings:
        return shop.settings
    existing = db.query(ShopSettings).filter(ShopSettings.shop_id == shop.id).one_or_none()
    if existing:
        return existing
    row = ShopSettings(
        shop_id=shop.id,
        auto_sync_enabled=False,
        auto_publish_processed_images=False,
        batch_interval_minutes=settings.default_batch_interval_minutes,
    )
    db.add(row)
    db.flush()
    return row


def get_or_create_shop(db: Session, shop_domain: str) -> Shop:
    domain = shop_domain.strip().lower()
    shop = db.query(Shop).filter(Shop.shop_domain == domain).one_or_none()
    if shop:
        ensure_shop_settings(db, shop)
        db.commit()
        db.refresh(shop)
        return shop

    shop = Shop(
        shop_domain=domain,
        encrypted_access_token=None,
        status=ShopStatus.ACTIVE,
        updated_at=datetime.now(timezone.utc),
    )
    if _dev_access_token_seed_allowed():
        # One-time bootstrap into DB so workers can resolve from shops thereafter.
        shop.encrypted_access_token = encrypt_token(settings.shopify_dev_access_token)
    db.add(shop)
    db.flush()
    ensure_shop_settings(db, shop)
    db.commit()
    db.refresh(shop)
    return shop


def upsert_shop_install(
    db: Session,
    *,
    shop_domain: str,
    access_token: str,
    scopes: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: datetime | None = None,
) -> Shop:
    domain = shop_domain.strip().lower()
    shop = db.query(Shop).filter(Shop.shop_domain == domain).one_or_none()
    now = datetime.now(timezone.utc)
    if shop is None:
        shop = Shop(shop_domain=domain, created_at=now)
        db.add(shop)
    shop.encrypted_access_token = encrypt_token(access_token)
    if refresh_token:
        shop.encrypted_refresh_token = encrypt_token(refresh_token)
    if scopes is not None:
        shop.scopes = scopes
    if token_expires_at is not None:
        shop.token_expires_at = token_expires_at
    shop.status = ShopStatus.ACTIVE
    shop.installed_at = shop.installed_at or now
    shop.uninstalled_at = None
    shop.updated_at = now
    db.flush()
    ensure_shop_settings(db, shop)
    db.commit()
    db.refresh(shop)
    return shop


def mark_shop_uninstalled(db: Session, shop_domain: str) -> Shop | None:
    domain = shop_domain.strip().lower()
    shop = db.query(Shop).filter(Shop.shop_domain == domain).one_or_none()
    if not shop:
        return None
    shop.status = ShopStatus.INACTIVE
    shop.encrypted_access_token = None
    shop.encrypted_refresh_token = None
    shop.token_expires_at = None
    shop.uninstalled_at = datetime.now(timezone.utc)
    shop.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(shop)
    return shop


def resolve_shop_access_token(shop: Shop, db: Session | None = None) -> str:
    """Return Admin API token from the shops table only (never SHOPIFY_DEV_ACCESS_TOKEN).

    When ``db`` is provided and the token is due (~23h / near expiry), refreshes into shops first.
    """
    working = shop
    if db is not None:
        from app.services.shopify_token_refresh import (
            ShopifyTokenRefreshError,
            access_token_needs_refresh,
            refresh_shop_access_token,
        )

        if access_token_needs_refresh(working):
            try:
                working = refresh_shop_access_token(db, working)
            except ShopifyTokenRefreshError as exc:
                # Keep attempting with the stored token; GraphQL 401 path force-refreshes.
                import logging

                logging.getLogger("app.core.shop_resolver").warning(
                    "Shopify token refresh failed | shop=%s error=%s",
                    shop.shop_domain,
                    exc,
                )

    if working.encrypted_access_token:
        token = decrypt_token(working.encrypted_access_token)
        if token:
            return token

    raise RuntimeError(
        "Shopify access token is not available in the shops table for this shop. "
        "Reinstall the app (install handoff) or refresh the token into shops."
    )


def create_shopify_graphql_client(db: Session, shop: Shop):
    """Build a GraphQL client that reads shops tokens and refreshes on HTTP 401."""
    from app.services.shopify_graphql import ShopifyGraphQLClient
    from app.services.shopify_token_refresh import refresh_shop_access_token

    token = resolve_shop_access_token(shop, db=db)

    def _refresh_after_unauthorized() -> str:
        refreshed = refresh_shop_access_token(db, shop, force=True)
        new_token = decrypt_token(refreshed.encrypted_access_token or "")
        if not new_token:
            raise RuntimeError("Shopify token refresh returned an empty access token")
        return new_token

    return ShopifyGraphQLClient(
        shop_domain=shop.shop_domain,
        access_token=token,
        refresh_access_token=_refresh_after_unauthorized,
    )


def get_shop_by_id(db: Session, shop_id: UUID) -> Shop | None:
    return db.get(Shop, shop_id)


def get_shop_by_domain(db: Session, shop_domain: str) -> Shop | None:
    return db.query(Shop).filter(Shop.shop_domain == shop_domain.strip().lower()).one_or_none()

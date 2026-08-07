from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models import Shop, ShopSettings, ShopStatus


def _dev_access_token_fallback_allowed() -> bool:
    """Client-credentials token is for Vite-only local/UAT tunnels (no OAuth shell).

    Production must use install handoff only.
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
    if _dev_access_token_fallback_allowed():
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
    shop.encrypted_refresh_token = encrypt_token(refresh_token) if refresh_token else shop.encrypted_refresh_token
    shop.scopes = scopes
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
    shop.uninstalled_at = datetime.now(timezone.utc)
    shop.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(shop)
    return shop


def resolve_shop_access_token(shop: Shop) -> str:
    if shop.encrypted_access_token:
        token = decrypt_token(shop.encrypted_access_token)
        if token:
            return token
    if _dev_access_token_fallback_allowed():
        return settings.shopify_dev_access_token
    raise RuntimeError(
        "Shopify access token is not available for this shop. "
        "Install handoff is not complete, or set SHOPIFY_DEV_ACCESS_TOKEN for local/UAT."
    )


def get_shop_by_id(db: Session, shop_id: UUID) -> Shop | None:
    return db.get(Shop, shop_id)


def get_shop_by_domain(db: Session, shop_domain: str) -> Shop | None:
    return db.query(Shop).filter(Shop.shop_domain == shop_domain.strip().lower()).one_or_none()

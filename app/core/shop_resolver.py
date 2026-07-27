from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Shop, ShopStatus


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


def get_or_create_shop(db: Session, shop_domain: str) -> Shop:
    domain = shop_domain.strip().lower()
    shop = db.query(Shop).filter(Shop.shop_domain == domain).one_or_none()
    if shop:
        return shop

    token: str | None = None
    if settings.app_env == "dev" and settings.shopify_dev_access_token:
        token = settings.shopify_dev_access_token

    shop = Shop(
        shop_domain=domain,
        access_token=token,
        status=ShopStatus.ACTIVE,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(shop)
    db.commit()
    db.refresh(shop)
    return shop


def resolve_shop_access_token(shop: Shop) -> str:
    if shop.access_token:
        return shop.access_token
    if settings.app_env == "dev" and settings.shopify_dev_access_token:
        return settings.shopify_dev_access_token
    raise RuntimeError(
        "Shopify access token is not available for this shop. "
        "Install handoff is not complete, or set SHOPIFY_DEV_ACCESS_TOKEN in development."
    )


def get_shop_by_id(db: Session, shop_id: UUID) -> Shop | None:
    return db.get(Shop, shop_id)

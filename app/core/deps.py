from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.shop_resolver import extract_shop_domain, get_or_create_shop
from app.db.session import get_db
from app.models import Shop
from app.poc.auth import require_shopify_jwt

DbSession = Annotated[Session, Depends(get_db)]
JwtPayload = Annotated[dict, Depends(require_shopify_jwt)]


def get_current_shop(
    db: DbSession,
    payload: JwtPayload,
) -> Shop:
    domain = extract_shop_domain(payload)
    return get_or_create_shop(db, domain)


CurrentShop = Annotated[Shop, Depends(get_current_shop)]

"""Unit tests for Shopify Admin API token refresh helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.core.crypto import encrypt_token
from app.core.shop_resolver import resolve_shop_access_token, upsert_shop_install
from app.models import Shop
from app.services.shopify_token_refresh import access_token_needs_refresh, refresh_shop_access_token


def test_access_token_needs_refresh_respects_skew():
    shop = Shop(shop_domain="a.myshopify.com")
    shop.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert access_token_needs_refresh(shop) is True
    shop.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
    assert access_token_needs_refresh(shop) is False
    shop.token_expires_at = None
    assert access_token_needs_refresh(shop) is False


def test_resolve_shop_access_token_uses_db_only(db_session, shop):
    shop.encrypted_access_token = encrypt_token("shpat_from_db")
    db_session.commit()
    with patch("app.config.settings.shopify_dev_access_token", "shpat_from_env"):
        assert resolve_shop_access_token(shop) == "shpat_from_db"


def test_resolve_refreshes_expired_token_into_db(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.config.settings.shopify_api_key", "key")
    monkeypatch.setattr("app.config.settings.shopify_api_secret", "secret")
    shop.encrypted_access_token = encrypt_token("shpat_old")
    shop.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    payload = {
        "access_token": "shpat_new",
        "scope": "write_products",
        "expires_in": 86400,
    }
    with patch(
        "app.services.shopify_token_refresh._request_access_token",
        return_value=payload,
    ) as mocked:
        token = resolve_shop_access_token(shop, db=db_session)
    assert token == "shpat_new"
    mocked.assert_called_once()
    db_session.refresh(shop)
    assert resolve_shop_access_token(shop) == "shpat_new"
    assert shop.token_expires_at is not None
    expires = shop.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc)


def test_refresh_prefers_refresh_token_grant(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.config.settings.shopify_api_key", "key")
    monkeypatch.setattr("app.config.settings.shopify_api_secret", "secret")
    shop.encrypted_access_token = encrypt_token("shpat_old")
    shop.encrypted_refresh_token = encrypt_token("refresh_old")
    shop.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db_session.commit()

    captured: dict = {}

    def fake_request(domain, form):
        captured["domain"] = domain
        captured["form"] = form
        return {
            "access_token": "shpat_rotated",
            "refresh_token": "refresh_new",
            "expires_in": 3600,
            "scope": "write_files",
        }

    with patch("app.services.shopify_token_refresh._request_access_token", side_effect=fake_request):
        refresh_shop_access_token(db_session, shop, force=True)

    assert captured["form"]["grant_type"] == "refresh_token"
    assert captured["form"]["refresh_token"] == "refresh_old"
    db_session.refresh(shop)
    assert resolve_shop_access_token(shop) == "shpat_rotated"


def test_upsert_keeps_existing_expiry_when_omitted(db_session, shop):
    expires = datetime.now(timezone.utc) + timedelta(hours=20)
    upsert_shop_install(
        db_session,
        shop_domain=shop.shop_domain,
        access_token="shpat_a",
        token_expires_at=expires,
    )
    db_session.refresh(shop)
    upsert_shop_install(
        db_session,
        shop_domain=shop.shop_domain,
        access_token="shpat_b",
        token_expires_at=None,
    )
    db_session.refresh(shop)
    assert resolve_shop_access_token(shop) == "shpat_b"
    stored = shop.token_expires_at
    assert stored is not None
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert abs((stored - expires).total_seconds()) < 2

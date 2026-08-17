from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.deps import get_current_shop, get_db
from app.db.base import Base
from app.main import app
from app.core.crypto import encrypt_token
from app.models import Shop
from app.poc.auth import require_shopify_jwt

# Existing upload tests use opaque 1x1 PNGs. Cut-out is covered in dedicated tests.
settings.rembg_enabled = False


@pytest.fixture()
def db_engine():
    # Keep existing ImageProcessor/sync tests on the SYNC path.
    settings.ai_execution_mode = "SYNC"
    settings.openai_batch_enabled = False
    settings.openai_allow_sync_fallback = True
    settings.rembg_enabled = False

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def SessionLocal(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture()
def db_session(SessionLocal) -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def shop(db_session: Session) -> Shop:
    row = Shop(
        shop_domain="test-shop.myshopify.com",
        encrypted_access_token=encrypt_token("shpat_test"),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def client(SessionLocal, shop: Shop, monkeypatch, tmp_path) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(settings, "auto_processing_enabled", False)
    monkeypatch.setattr(settings, "processing_output_directory", str(tmp_path / "processed"))
    monkeypatch.setattr(settings, "processing_batch_size", 2)
    monkeypatch.setattr(settings, "processing_max_attempts", 3)
    monkeypatch.setattr(settings, "poc_dev_skip_auth", True)
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "processing_stale_lock_seconds", 1)

    shop_id = shop.id

    def override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_jwt() -> dict:
        return {"dest": "https://test-shop.myshopify.com", "sub": "test"}

    def override_shop(db: Session = Depends(get_db)) -> Shop:  # noqa: B008
        return db.query(Shop).filter(Shop.id == shop_id).one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_shopify_jwt] = override_jwt
    app.dependency_overrides[get_current_shop] = override_shop

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.core.shop_resolver import ensure_shop_settings
from app.models import Shop, ShopSettings


class SettingsValidationError(ValueError):
    pass


class SettingsService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def get(self) -> ShopSettings:
        return ensure_shop_settings(self.db, self.shop)

    def update(
        self,
        *,
        auto_sync_enabled: bool | None = None,
        auto_publish_processed_images: bool | None = None,
        batch_interval_minutes: int | None = None,
    ) -> ShopSettings:
        row = self.get()
        if auto_sync_enabled is not None:
            row.auto_sync_enabled = auto_sync_enabled
        if auto_publish_processed_images is not None:
            row.auto_publish_processed_images = auto_publish_processed_images
        if batch_interval_minutes is not None:
            if batch_interval_minutes < 0:
                raise SettingsValidationError("batch_interval_minutes cannot be negative")
            cap = settings.batch_interval_minutes_cap
            if batch_interval_minutes > cap:
                raise SettingsValidationError(f"batch_interval_minutes cannot exceed {cap}")
            row.batch_interval_minutes = batch_interval_minutes
        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return row

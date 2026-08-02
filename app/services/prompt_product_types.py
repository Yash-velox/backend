"""Product-type registry for Prompt Management."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.models import (
    BatchProduct,
    BatchProductStatus,
    Product,
    PromptConfiguration,
    PromptListStatus,
    PromptProductType,
    PromptProductTypeSource,
    PromptStep,
    Shop,
)

logger = logging.getLogger("app.services.prompt_product_types")

ACTIVE_BATCH_PRODUCT_STATUSES = (
    BatchProductStatus.QUEUED,
    BatchProductStatus.PROCESSING,
    BatchProductStatus.RETRYING,
)


class PromptProductTypeError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def normalize_product_type_name(name: str | None) -> str | None:
    if name is None:
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    return trimmed.casefold()


def display_name(name: str) -> str:
    return name.strip()


def compute_list_status(
    *,
    step_count: int,
    enabled_step_count: int,
    is_enabled: bool | None,
) -> PromptListStatus:
    if step_count == 0:
        return PromptListStatus.NOT_CONFIGURED
    if is_enabled is False:
        return PromptListStatus.DISABLED
    if enabled_step_count == 0:
        return PromptListStatus.NOT_READY
    return PromptListStatus.ENABLED


class PromptProductTypeService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def sync_shopify_product_types(self) -> int:
        """Upsert distinct non-empty product types from synchronized products.

        Does not delete types that disappeared from the catalog.
        Does not overwrite MANUAL rows that share a normalized name.
        """
        rows = (
            self.db.query(Product.product_type)
            .filter(
                Product.shop_id == self.shop.id,
                Product.is_deleted.is_(False),
                Product.product_type.isnot(None),
            )
            .distinct()
            .all()
        )

        created = 0
        for (raw_type,) in rows:
            normalized = normalize_product_type_name(raw_type)
            if not normalized:
                continue
            name = display_name(raw_type or "")
            existing = (
                self.db.query(PromptProductType)
                .filter(
                    PromptProductType.shop_id == self.shop.id,
                    PromptProductType.normalized_name == normalized,
                )
                .one_or_none()
            )
            if existing is None:
                row = PromptProductType(
                    shop_id=self.shop.id,
                    name=name,
                    normalized_name=normalized,
                    source=PromptProductTypeSource.SHOPIFY,
                    is_active=True,
                )
                self.db.add(row)
                self.db.flush()
                self._ensure_configuration(row)
                created += 1
            elif existing.source == PromptProductTypeSource.SHOPIFY:
                # Preserve display name if blank; otherwise keep existing display name.
                if not existing.name and name:
                    existing.name = name
                existing.is_active = True

        if created:
            self.db.flush()
            logger.info(
                "Synced Shopify product types | shop=%s created=%s",
                self.shop.id,
                created,
            )
        return created

    def add_manual(self, name: str) -> PromptProductType:
        display = display_name(name)
        normalized = normalize_product_type_name(display)
        if not normalized:
            raise PromptProductTypeError(
                "Product type name is required.",
                code="PROMPT_PRODUCT_TYPE_INVALID",
                status_code=422,
            )

        existing = (
            self.db.query(PromptProductType)
            .filter(
                PromptProductType.shop_id == self.shop.id,
                PromptProductType.normalized_name == normalized,
            )
            .one_or_none()
        )
        if existing is not None:
            raise PromptProductTypeError(
                f'Product type "{display}" already exists for this shop.',
                code="PROMPT_PRODUCT_TYPE_DUPLICATE",
                status_code=409,
            )

        row = PromptProductType(
            shop_id=self.shop.id,
            name=display,
            normalized_name=normalized,
            source=PromptProductTypeSource.MANUAL,
            is_active=True,
        )
        self.db.add(row)
        self.db.flush()
        self._ensure_configuration(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get(self, product_type_id: UUID) -> PromptProductType:
        row = (
            self.db.query(PromptProductType)
            .options(
                selectinload(PromptProductType.configuration).selectinload(PromptConfiguration.steps)
            )
            .filter(
                PromptProductType.id == product_type_id,
                PromptProductType.shop_id == self.shop.id,
            )
            .one_or_none()
        )
        if row is None:
            raise PromptProductTypeError(
                "Product type not found.",
                code="PROMPT_PRODUCT_TYPE_NOT_FOUND",
                status_code=404,
            )
        self._ensure_configuration(row)
        return row

    def list(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        self.sync_shopify_product_types()
        self.db.commit()

        page = max(1, page)
        page_size = min(max(1, page_size), 100)

        q = (
            self.db.query(PromptProductType)
            .options(
                selectinload(PromptProductType.configuration).selectinload(PromptConfiguration.steps)
            )
            .filter(PromptProductType.shop_id == self.shop.id)
        )
        if search and search.strip():
            term = f"%{search.strip().casefold()}%"
            q = q.filter(
                or_(
                    func.lower(PromptProductType.name).like(term),
                    PromptProductType.normalized_name.like(term),
                )
            )

        rows = q.order_by(PromptProductType.name.asc()).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            config = self._ensure_configuration(row)
            steps = (
                self.db.query(PromptStep)
                .filter(PromptStep.prompt_configuration_id == config.id)
                .all()
            )
            step_count = len(steps)
            enabled_step_count = sum(1 for s in steps if s.is_enabled)
            is_enabled = config.is_enabled if config else True
            computed = compute_list_status(
                step_count=step_count,
                enabled_step_count=enabled_step_count,
                is_enabled=is_enabled,
            )
            if status:
                wanted = status.strip().upper()
                if wanted != "ALL" and computed.value != wanted:
                    continue
            updated_at = row.updated_at
            if config and config.updated_at and (updated_at is None or config.updated_at > updated_at):
                updated_at = config.updated_at
            for step in steps:
                if step.updated_at and (updated_at is None or step.updated_at > updated_at):
                    updated_at = step.updated_at
            items.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "source": row.source.value if isinstance(row.source, PromptProductTypeSource) else row.source,
                    "stepCount": step_count,
                    "enabledStepCount": enabled_step_count,
                    "status": computed.value,
                    "isEnabled": is_enabled,
                    "updatedAt": updated_at if step_count > 0 else None,
                    "createdAt": row.created_at,
                }
            )

        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]
        return page_items, total

    def delete_manual(self, product_type_id: UUID) -> None:
        row = self.get(product_type_id)
        if row.source != PromptProductTypeSource.MANUAL:
            raise PromptProductTypeError(
                "Shopify-sourced product types cannot be deleted.",
                code="PROMPT_PRODUCT_TYPE_DELETE_FORBIDDEN",
                status_code=403,
            )

        if self._has_active_processing(row.normalized_name):
            raise PromptProductTypeError(
                "Cannot delete this product type while products of this type are actively processing.",
                code="PROMPT_PRODUCT_TYPE_DELETE_FORBIDDEN",
                status_code=409,
            )

        self.db.delete(row)
        self.db.commit()

    def _has_active_processing(self, normalized_name: str) -> bool:
        products = (
            self.db.query(Product.id, Product.product_type)
            .filter(Product.shop_id == self.shop.id, Product.is_deleted.is_(False))
            .all()
        )
        matching_ids = [
            pid
            for pid, ptype in products
            if normalize_product_type_name(ptype) == normalized_name
        ]
        if not matching_ids:
            return False
        active = (
            self.db.query(BatchProduct.id)
            .filter(
                BatchProduct.shop_id == self.shop.id,
                BatchProduct.product_id.in_(matching_ids),
                BatchProduct.status.in_(ACTIVE_BATCH_PRODUCT_STATUSES),
            )
            .first()
        )
        return active is not None

    def _ensure_configuration(self, product_type: PromptProductType) -> PromptConfiguration:
        config = product_type.configuration
        if config is None:
            config = (
                self.db.query(PromptConfiguration)
                .options(selectinload(PromptConfiguration.steps))
                .filter(
                    PromptConfiguration.shop_id == self.shop.id,
                    PromptConfiguration.prompt_product_type_id == product_type.id,
                )
                .one_or_none()
            )
        if config is None:
            config = PromptConfiguration(
                shop_id=self.shop.id,
                prompt_product_type_id=product_type.id,
                is_enabled=True,
            )
            self.db.add(config)
            self.db.flush()
            product_type.configuration = config
        return config

    def find_by_normalized_name(self, normalized_name: str) -> PromptProductType | None:
        return (
            self.db.query(PromptProductType)
            .options(
                selectinload(PromptProductType.configuration).selectinload(PromptConfiguration.steps)
            )
            .filter(
                PromptProductType.shop_id == self.shop.id,
                PromptProductType.normalized_name == normalized_name,
            )
            .one_or_none()
        )

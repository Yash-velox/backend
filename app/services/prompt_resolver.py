"""Resolve enabled sequential prompts for a product at processing time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import BatchImage, Product, Shop
from app.services.prompt_product_types import (
    PromptProductTypeService,
    normalize_product_type_name,
)
from app.services.prompt_variables import extract_variables, render_prompt


class PromptResolverError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class ResolvedPromptStep:
    step_order: int
    name: str
    prompt_text: str
    rendered_prompt: str
    variables: list[str]


class PromptResolver:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop
        self.types = PromptProductTypeService(db, shop)

    def resolve_for_product(
        self,
        product: Product | None,
        *,
        product_type_override: str | None = None,
        image: BatchImage | None = None,
        image_position: int | None = None,
    ) -> list[ResolvedPromptStep]:
        raw_type = product_type_override
        if raw_type is None and product is not None:
            raw_type = product.product_type
        if product is not None and product.shop_id != self.shop.id:
            raise PromptResolverError("Product does not belong to this shop.", code="SHOP_MISMATCH")

        normalized = normalize_product_type_name(raw_type)
        if not normalized:
            raise PromptResolverError(
                "This product has no Shopify product type. "
                "Assign a product type in Shopify or configure prompts after setting one.",
                code="PRODUCT_TYPE_MISSING",
                retryable=False,
            )

        ppt = self.types.find_by_normalized_name(normalized)
        if ppt is None:
            # Attempt a sync in case types were never imported.
            self.types.sync_shopify_product_types()
            self.db.flush()
            ppt = self.types.find_by_normalized_name(normalized)

        display = (raw_type or "").strip() or normalized
        if ppt is None:
            raise PromptResolverError(
                f'No active prompt configuration is available for product type "{display}". '
                "Configure prompts for this product type before processing.",
                code="PROMPT_NOT_CONFIGURED",
                retryable=False,
            )

        config = self.types._ensure_configuration(ppt)
        if not config.is_enabled:
            raise PromptResolverError(
                f'Prompt configuration for product type "{ppt.name}" is disabled.',
                code="PROMPT_CONFIGURATION_DISABLED",
                retryable=False,
            )

        from app.models import PromptStep

        all_steps = (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == config.id)
            .order_by(PromptStep.step_order.asc())
            .all()
        )
        enabled_steps = [s for s in all_steps if s.is_enabled]
        if not enabled_steps:
            if not all_steps:
                raise PromptResolverError(
                    f'No active prompt configuration is available for product type "{ppt.name}". '
                    "Configure prompts for this product type before processing.",
                    code="PROMPT_NOT_CONFIGURED",
                    retryable=False,
                )
            raise PromptResolverError(
                f'No enabled prompt steps for product type "{ppt.name}". '
                "Enable at least one step before processing.",
                code="PROMPT_NO_ENABLED_STEPS",
                retryable=False,
            )

        values = self._build_variable_values(
            product,
            product_type_display=ppt.name,
            image=image,
            image_position=image_position,
        )
        resolved: list[ResolvedPromptStep] = []
        for index, step in enumerate(enabled_steps, start=1):
            rendered = render_prompt(step.prompt_text, values)
            resolved.append(
                ResolvedPromptStep(
                    step_order=index,
                    name=step.name,
                    prompt_text=step.prompt_text,
                    rendered_prompt=rendered,
                    variables=extract_variables(step.prompt_text),
                )
            )
        return resolved

    def to_snapshot(self, steps: list[ResolvedPromptStep]) -> list[dict[str, Any]]:
        return [
            {
                "step": s.step_order,
                "name": s.name,
                "prompt": s.rendered_prompt,
                "promptTemplate": s.prompt_text,
                "variables": s.variables,
                "preserveTransparency": True,
            }
            for s in steps
        ]

    def _build_variable_values(
        self,
        product: Product | None,
        *,
        product_type_display: str,
        image: BatchImage | None,
        image_position: int | None,
    ) -> dict[str, str | None]:
        shop_name = self.shop.shop_domain.split(".")[0] if self.shop.shop_domain else ""
        return {
            "product_title": product.title if product else None,
            "product_type": product_type_display,
            "product_vendor": product.vendor if product else None,
            "product_handle": product.handle if product else None,
            "product_description": product.description_html if product else None,
            "image_filename": image.original_filename if image else None,
            "image_position": str(image_position) if image_position is not None else None,
            "shop_name": shop_name,
        }

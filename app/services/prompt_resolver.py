"""Resolve enabled sequential prompts for a product at processing time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import BatchImage, Product, PromptConfiguration, PromptStep, Shop
from app.models.enums import PromptStepType
from app.services.prompt_product_types import (
    CENTRAL_PROMPT_DISPLAY_NAME,
    PromptProductTypeService,
    is_central_product_type,
    normalize_product_type_name,
)
from app.services.prompt_variables import (
    PromptVariableError,
    extract_variables,
    render_prompt,
    validate_prompt_variables,
)


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
    step_type: PromptStepType = PromptStepType.IMAGE
    step_id: UUID | None = None


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

        display = (raw_type or "").strip()
        normalized = normalize_product_type_name(raw_type)

        type_steps = None
        if normalized:
            type_steps = self._try_resolve_product_type(
                normalized,
                display_name=display or normalized,
                product=product,
                image=image,
                image_position=image_position,
            )
            if type_steps is not None:
                return type_steps

        # Missing / unconfigured / disabled / no enabled steps / no product type → System Prompt.
        return self._resolve_central(
            product=product,
            product_type_display=display or CENTRAL_PROMPT_DISPLAY_NAME,
            image=image,
            image_position=image_position,
        )

    def _try_resolve_product_type(
        self,
        normalized: str,
        *,
        display_name: str,
        product: Product | None,
        image: BatchImage | None,
        image_position: int | None,
    ) -> list[ResolvedPromptStep] | None:
        """Return type-specific steps, or None when System Prompt should be used."""
        ppt = self.types.find_by_normalized_name(normalized)
        if ppt is None:
            self.types.sync_shopify_product_types()
            self.db.flush()
            ppt = self.types.find_by_normalized_name(normalized)

        if ppt is None or is_central_product_type(ppt):
            return None

        config = self.types._ensure_configuration(ppt)
        if not config.is_enabled:
            return None

        enabled_steps = self._enabled_steps(config)
        if not enabled_steps:
            return None

        return self._render_steps(
            enabled_steps,
            product=product,
            product_type_display=ppt.name or display_name,
            image=image,
            image_position=image_position,
        )

    def _resolve_central(
        self,
        *,
        product: Product | None,
        product_type_display: str,
        image: BatchImage | None,
        image_position: int | None,
    ) -> list[ResolvedPromptStep]:
        central = self.types.ensure_central_prompt()
        config = self.types._ensure_configuration(central)
        config.is_enabled = True
        enabled_steps = self._enabled_steps(config)
        if not enabled_steps:
            raise PromptResolverError(
                f"No {CENTRAL_PROMPT_DISPLAY_NAME} is configured. "
                f"Save a {CENTRAL_PROMPT_DISPLAY_NAME} before processing products "
                "without a ready product-type prompt.",
                code="PROMPT_NOT_CONFIGURED",
                retryable=False,
            )
        # System Prompt is a single prompt — never run sequential steps.
        return self._render_steps(
            enabled_steps[:1],
            product=product,
            product_type_display=product_type_display,
            image=image,
            image_position=image_position,
        )

    def _enabled_steps(self, config: PromptConfiguration) -> list[PromptStep]:
        all_steps = (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == config.id)
            .order_by(PromptStep.step_order.asc())
            .all()
        )
        return [s for s in all_steps if s.is_enabled]

    def _render_steps(
        self,
        enabled_steps: list[PromptStep],
        *,
        product: Product | None,
        product_type_display: str,
        image: BatchImage | None,
        image_position: int | None,
    ) -> list[ResolvedPromptStep]:
        values = self._build_variable_values(
            product,
            product_type_display=product_type_display,
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
                    step_type=getattr(step, "step_type", None) or PromptStepType.IMAGE,
                    step_id=step.id,
                )
            )
        return resolved

    def resolve_from_override(
        self,
        override_steps: list[dict[str, Any]],
        *,
        product: Product | None,
        product_type_display: str,
        image: BatchImage | None = None,
        image_position: int | None = None,
    ) -> list[ResolvedPromptStep]:
        """Render a one-time prompt override (does not change saved configuration)."""
        if not override_steps:
            raise PromptResolverError(
                "Prompt override is empty.",
                code="PROMPT_OVERRIDE_EMPTY",
                retryable=False,
            )

        values = self._build_variable_values(
            product,
            product_type_display=product_type_display,
            image=image,
            image_position=image_position,
        )
        resolved: list[ResolvedPromptStep] = []
        for index, raw in enumerate(override_steps, start=1):
            if not isinstance(raw, dict):
                raise PromptResolverError(
                    "Each prompt override step must be an object.",
                    code="PROMPT_OVERRIDE_INVALID",
                    retryable=False,
                )
            name = str(raw.get("name") or f"Step {index}").strip() or f"Step {index}"
            template = str(
                raw.get("promptTemplate")
                or raw.get("prompt_template")
                or raw.get("prompt")
                or ""
            )
            if not template.strip():
                raise PromptResolverError(
                    f'Prompt override step "{name}" has empty text.',
                    code="PROMPT_OVERRIDE_EMPTY",
                    retryable=False,
                )
            try:
                validate_prompt_variables(template)
            except PromptVariableError as exc:
                raise PromptResolverError(
                    str(exc),
                    code=exc.code,
                    retryable=False,
                ) from exc
            raw_type = str(raw.get("stepType") or raw.get("step_type") or "IMAGE").upper()
            try:
                step_type = PromptStepType(raw_type)
            except ValueError:
                step_type = PromptStepType.IMAGE
            rendered = render_prompt(template, values)
            resolved.append(
                ResolvedPromptStep(
                    step_order=index,
                    name=name,
                    prompt_text=template,
                    rendered_prompt=rendered,
                    variables=extract_variables(template),
                    step_type=step_type,
                    step_id=None,
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
                "stepType": s.step_type.value if hasattr(s.step_type, "value") else str(s.step_type),
                "stepId": str(s.step_id) if s.step_id else None,
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


def product_prompt_block_message(
    exc: PromptResolverError,
    *,
    product_label: str,
) -> str:
    """Merchant-facing reason when a product cannot be queued/processed."""
    label = (product_label or "").strip() or "product"
    return f'Cannot process "{label}": {exc}'


def assert_product_prompts_ready(
    db: Session,
    shop: Shop,
    product: Product | None,
    *,
    product_type_override: str | None = None,
    product_label: str | None = None,
) -> list[ResolvedPromptStep]:
    """Resolve prompts or raise PromptResolverError with a clear product label."""
    label = product_label
    if not label and product is not None:
        label = product.title or product.shopify_product_gid
    try:
        return PromptResolver(db, shop).resolve_for_product(
            product,
            product_type_override=product_type_override,
        )
    except PromptResolverError as exc:
        raise PromptResolverError(
            product_prompt_block_message(exc, product_label=label or "product"),
            code=exc.code,
            retryable=exc.retryable,
        ) from exc

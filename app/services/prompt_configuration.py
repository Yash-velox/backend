"""Prompt configuration and sequential step CRUD."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models import PromptConfiguration, PromptProductType, PromptStep, PromptStepType, Shop
from app.services.prompt_product_types import PromptProductTypeError, PromptProductTypeService
from app.services.prompt_variables import PromptVariableError, validate_prompt_variables

MAX_STEP_NAME_LENGTH = 150
MAX_PROMPT_TEXT_LENGTH = 20000


class PromptConfigurationError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PromptConfigurationService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop
        self.types = PromptProductTypeService(db, shop)

    def get_detail(self, product_type_id: UUID) -> tuple[PromptProductType, PromptConfiguration]:
        product_type = self.types.get(product_type_id)
        config = self.types._ensure_configuration(product_type)
        self.db.refresh(config)
        steps = (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == config.id)
            .order_by(PromptStep.step_order.asc())
            .all()
        )
        config.steps = steps
        return product_type, config

    def set_enabled(self, product_type_id: UUID, is_enabled: bool) -> PromptConfiguration:
        _, config = self.get_detail(product_type_id)
        config.is_enabled = bool(is_enabled)
        config.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(config)
        return config

    def add_step(
        self,
        product_type_id: UUID,
        *,
        name: str,
        prompt_text: str,
        is_enabled: bool = True,
        step_type: str | PromptStepType = PromptStepType.IMAGE,
    ) -> PromptStep:
        _, config = self.get_detail(product_type_id)
        cleaned_name, cleaned_text = self._validate_step_fields(name, prompt_text)
        next_order = (max((s.step_order for s in config.steps), default=0)) + 1
        resolved_type = (
            step_type if isinstance(step_type, PromptStepType) else PromptStepType(str(step_type).upper())
        )
        step = PromptStep(
            prompt_configuration_id=config.id,
            name=cleaned_name,
            prompt_text=cleaned_text,
            step_order=next_order,
            step_type=resolved_type,
            is_enabled=bool(is_enabled),
        )
        self.db.add(step)
        config.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(step)
        return step

    def update_step(
        self,
        step_id: UUID,
        *,
        name: str | None = None,
        prompt_text: str | None = None,
        is_enabled: bool | None = None,
        step_type: str | PromptStepType | None = None,
    ) -> PromptStep:
        step = self._get_step_for_shop(step_id)
        new_name = name if name is not None else step.name
        new_text = prompt_text if prompt_text is not None else step.prompt_text
        cleaned_name, cleaned_text = self._validate_step_fields(new_name, new_text)
        step.name = cleaned_name
        step.prompt_text = cleaned_text
        if is_enabled is not None:
            step.is_enabled = bool(is_enabled)
        if step_type is not None:
            step.step_type = (
                step_type if isinstance(step_type, PromptStepType) else PromptStepType(str(step_type).upper())
            )
        step.updated_at = datetime.now(timezone.utc)
        step.configuration.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(step)
        return step

    def set_step_status(self, step_id: UUID, is_enabled: bool) -> PromptStep:
        return self.update_step(step_id, is_enabled=is_enabled)

    def delete_step(self, step_id: UUID) -> None:
        step = self._get_step_for_shop(step_id)
        config_id = step.prompt_configuration_id
        config = step.configuration
        self.db.delete(step)
        self.db.flush()
        self._normalize_order(config_id)
        config.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def reorder_steps(self, product_type_id: UUID, step_ids: list[UUID]) -> list[PromptStep]:
        _, config = self.get_detail(product_type_id)
        existing = (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == config.id)
            .all()
        )
        by_id = {s.id: s for s in existing}
        if len(step_ids) != len(existing):
            raise PromptConfigurationError(
                "Reorder must include every step exactly once.",
                code="PROMPT_STEP_INVALID_ORDER",
                status_code=422,
            )
        if len(set(step_ids)) != len(step_ids):
            raise PromptConfigurationError(
                "Reorder contains duplicate step IDs.",
                code="PROMPT_STEP_INVALID_ORDER",
                status_code=422,
            )
        for sid in step_ids:
            if sid not in by_id:
                raise PromptConfigurationError(
                    "One or more steps do not belong to this configuration.",
                    code="PROMPT_STEP_INVALID_ORDER",
                    status_code=422,
                )

        # Two-phase update avoids unique constraint collisions on step_order.
        for offset, step in enumerate(existing, start=1):
            step.step_order = -(offset)
        self.db.flush()
        for index, sid in enumerate(step_ids, start=1):
            by_id[sid].step_order = index
            by_id[sid].updated_at = datetime.now(timezone.utc)
        config.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        return (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == config.id)
            .order_by(PromptStep.step_order.asc())
            .all()
        )

    def _normalize_order(self, configuration_id: UUID) -> None:
        steps = (
            self.db.query(PromptStep)
            .filter(PromptStep.prompt_configuration_id == configuration_id)
            .order_by(PromptStep.step_order.asc())
            .all()
        )
        for offset, step in enumerate(steps, start=1):
            step.step_order = -(offset)
        self.db.flush()
        for index, step in enumerate(steps, start=1):
            step.step_order = index

    def _get_step_for_shop(self, step_id: UUID) -> PromptStep:
        step = (
            self.db.query(PromptStep)
            .options(selectinload(PromptStep.configuration))
            .filter(PromptStep.id == step_id)
            .one_or_none()
        )
        if step is None or step.configuration.shop_id != self.shop.id:
            raise PromptConfigurationError(
                "Prompt step not found.",
                code="PROMPT_STEP_NOT_FOUND",
                status_code=404,
            )
        return step

    def _validate_step_fields(self, name: str, prompt_text: str) -> tuple[str, str]:
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise PromptConfigurationError(
                "Step name is required.",
                code="PROMPT_STEP_INVALID",
                status_code=422,
            )
        if len(cleaned_name) > MAX_STEP_NAME_LENGTH:
            raise PromptConfigurationError(
                f"Step name must be at most {MAX_STEP_NAME_LENGTH} characters.",
                code="PROMPT_STEP_INVALID",
                status_code=422,
            )
        cleaned_text = prompt_text if prompt_text is not None else ""
        if not cleaned_text.strip():
            raise PromptConfigurationError(
                "Prompt text is required.",
                code="PROMPT_STEP_INVALID",
                status_code=422,
            )
        if len(cleaned_text) > MAX_PROMPT_TEXT_LENGTH:
            raise PromptConfigurationError(
                f"Prompt text must be at most {MAX_PROMPT_TEXT_LENGTH} characters.",
                code="PROMPT_STEP_INVALID",
                status_code=422,
            )
        try:
            validate_prompt_variables(cleaned_text)
        except PromptVariableError as exc:
            raise PromptConfigurationError(str(exc), code=exc.code, status_code=422) from exc
        return cleaned_name, cleaned_text

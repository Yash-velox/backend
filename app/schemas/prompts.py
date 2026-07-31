"""Prompt Management API schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ManualProductTypeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ConfigurationUpdateRequest(BaseModel):
    isEnabled: bool


class PromptStepCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    promptText: str = Field(min_length=1, max_length=20000)
    isEnabled: bool = True


class PromptStepUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    promptText: str | None = Field(default=None, max_length=20000)
    isEnabled: bool | None = None


class PromptStepStatusRequest(BaseModel):
    isEnabled: bool


class PromptStepsReorderRequest(BaseModel):
    stepIds: list[UUID] = Field(min_length=1)


class PromptStepOut(BaseModel):
    id: UUID
    name: str
    promptText: str
    stepOrder: int
    isEnabled: bool
    variables: list[str]
    createdAt: datetime
    updatedAt: datetime


class PromptProductTypeListItemOut(BaseModel):
    id: UUID
    name: str
    source: str
    stepCount: int
    enabledStepCount: int
    status: str
    isEnabled: bool
    updatedAt: datetime | None = None
    createdAt: datetime


class PromptConfigurationDetailOut(BaseModel):
    id: UUID
    productTypeId: UUID
    name: str
    source: str
    isEnabled: bool
    status: str
    stepCount: int
    enabledStepCount: int
    steps: list[PromptStepOut]
    createdAt: datetime
    updatedAt: datetime

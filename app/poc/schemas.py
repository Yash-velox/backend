from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PromptInput(BaseModel):
    step: int = Field(ge=1)
    prompt: str = Field(min_length=1)


class PromptStep(BaseModel):
    step: int
    prompt: str
    status: StepStatus
    output_url: str | None = Field(default=None, alias="outputUrl")
    error_message: str | None = Field(default=None, alias="errorMessage")

    model_config = {"populate_by_name": True}


class JobProgress(BaseModel):
    job_id: str = Field(alias="jobId")
    status: JobStatus
    total_steps: int = Field(alias="totalSteps")
    completed_steps: int = Field(alias="completedSteps")
    steps: list[PromptStep]
    failed_step: int | None = Field(default=None, alias="failedStep")

    model_config = {"populate_by_name": True}


class SuccessEnvelope(BaseModel):
    success: bool = True
    message: str | None = None
    data: Any


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str | None = Field(default=None, alias="request_id")
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ErrorEnvelope(BaseModel):
    error: ErrorBody

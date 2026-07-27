from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SuccessEnvelope(BaseModel):
    success: bool = True
    message: str | None = None
    data: Any = None
    requestId: str | None = None


class ShopifyEnqueueRequest(BaseModel):
    productIds: list[str] = Field(default_factory=list, min_length=1)
    prompts: list[dict[str, Any] | str] | None = None
    processingConfig: dict[str, Any] | None = None
    priority: int = 100


class RetrySelectedRequest(BaseModel):
    itemIds: list[UUID] = Field(default_factory=list, min_length=1)


class PaginationMeta(BaseModel):
    page: int
    pageSize: int
    totalItems: int
    totalPages: int


class AttemptOut(BaseModel):
    id: UUID
    attemptNumber: int
    status: str
    provider: str | None = None
    providerRequestId: str | None = None
    shopifySourceUrl: str | None = None
    outputStorageKey: str | None = None
    errorCode: str | None = None
    errorMessage: str | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None


class QueueItemOut(BaseModel):
    id: UUID
    sourceType: str
    shopifyProductId: str
    shopifyMediaId: str
    shopifyImageId: str | None = None
    shopifyCdnUrl: str
    originalFilename: str | None = None
    sourceMimeType: str | None = None
    sourceWidth: int | None = None
    sourceHeight: int | None = None
    status: str
    priority: int
    batchId: UUID | None = None
    attemptCount: int
    maxAttempts: int
    outputStorageKey: str | None = None
    outputUrl: str | None = None
    outputMimeType: str | None = None
    outputChecksum: str | None = None
    errorCode: str | None = None
    errorMessage: str | None = None
    promptData: Any = None
    processingConfig: Any = None
    processingStartedAt: datetime | None = None
    processingCompletedAt: datetime | None = None
    nextRetryAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime
    attempts: list[AttemptOut] | None = None


class BatchOut(BaseModel):
    id: UUID
    triggerType: str
    status: str
    batchSize: int
    totalItems: int
    pendingItems: int
    queuedItems: int
    processingItems: int
    completedItems: int
    retryPendingItems: int
    failedItems: int
    cancelledItems: int
    startedBy: str | None = None
    errorMessage: str | None = None
    createdAt: datetime
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    updatedAt: datetime

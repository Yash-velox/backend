from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.queue import AttemptOut, PaginationMeta, SuccessEnvelope

__all__ = [
    "AttemptOut",
    "BatchImageOut",
    "BatchOut",
    "BatchProductOut",
    "ManualBatchCreateRequest",
    "PaginationMeta",
    "SecondaryQueueItemOut",
    "SecondaryQueueSummaryOut",
    "SettingsOut",
    "SettingsUpdateRequest",
    "SuccessEnvelope",
    "SyncRunOut",
]


class ManualBatchCreateRequest(BaseModel):
    productGids: list[str] = Field(min_length=1)


class ReprocessPromptStepIn(BaseModel):
    name: str | None = None
    promptTemplate: str | None = None
    prompt: str | None = None


class ReprocessRequest(BaseModel):
    steps: list[ReprocessPromptStepIn] | None = None


class LiveReprocessRequest(BaseModel):
    mediaGids: list[str] = Field(min_length=1)
    steps: list[ReprocessPromptStepIn] | None = None


class SettingsOut(BaseModel):
    autoSyncEnabled: bool
    autoPublishProcessedImages: bool
    batchIntervalMinutes: int
    createdAt: datetime
    updatedAt: datetime


class SettingsUpdateRequest(BaseModel):
    autoSyncEnabled: bool | None = None
    autoPublishProcessedImages: bool | None = None
    batchIntervalMinutes: int | None = None


class SyncRunOut(BaseModel):
    id: UUID
    runType: str
    status: str
    productsSynced: int
    mediaSynced: int
    cursor: str | None = None
    errorMessage: str | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime


class SecondaryQueueSummaryOut(BaseModel):
    pending: int
    claimed: int
    converted: int
    skipped: int
    failed: int
    total: int


class SecondaryQueueItemOut(BaseModel):
    id: UUID
    shopifyProductGid: str
    productId: UUID | None = None
    title: str | None = None
    handle: str | None = None
    adminUrl: str | None = None
    storefrontUrl: str | None = None
    queueRevision: int
    status: str
    webhookCount: int
    firstQueuedAt: datetime
    lastQueuedAt: datetime
    latestEligibleWebhookId: str | None = None
    claimedAt: datetime | None = None
    claimedBy: str | None = None
    convertedBatchId: UUID | None = None
    skipReason: str | None = None
    failureReason: str | None = None
    createdAt: datetime
    updatedAt: datetime


class BatchOut(BaseModel):
    id: UUID
    triggerType: str
    status: str
    processingPhase: str | None = None
    currentWorkflowStep: int = 0
    totalWorkflowSteps: int = 0
    openaiRequestsTotal: int = 0
    openaiRequestsCompleted: int = 0
    openaiRequestsFailed: int = 0
    productCount: int
    imageCount: int
    pendingProductCount: int
    processingProductCount: int
    completedProductCount: int
    failedProductCount: int
    publishedProductCount: int = 0
    retryingProductCount: int
    settingsSnapshotJson: dict[str, Any] | None = None
    errorSummary: str | None = None
    createdAt: datetime
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    updatedAt: datetime


class BatchProductOut(BaseModel):
    id: UUID
    batchId: UUID
    shopifyProductGid: str
    productId: UUID | None = None
    title: str | None = None
    handle: str | None = None
    adminUrl: str | None = None
    storefrontUrl: str | None = None
    status: str
    publishStatus: str | None = None
    imageCount: int
    retryCount: int
    errorCode: str | None = None
    errorMessage: str | None = None
    lockedBy: str | None = None
    lockedAt: datetime | None = None
    claimedAt: datetime | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    nextRetryAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime


class BatchImageOut(BaseModel):
    id: UUID
    batchProductId: UUID
    shopifyMediaGid: str
    shopifyFileGid: str | None = None
    cdnUrl: str
    originalFilename: str | None = None
    width: int | None = None
    height: int | None = None
    mimeType: str | None = None
    sourceFingerprint: str | None = None
    deltaType: str
    currentPromptStep: int
    status: str
    attemptCount: int
    outputStorageKey: str | None = None
    outputUrl: str | None = None
    outputMimeType: str | None = None
    outputChecksum: str | None = None
    generatedShopifyFileGid: str | None = None
    generatedShopifyCdnUrl: str | None = None
    generatedImageVersionId: UUID | None = None
    errorCode: str | None = None
    errorMessage: str | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime
    attempts: list[AttemptOut] | None = None

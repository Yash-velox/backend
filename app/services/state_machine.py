"""Centralized state transition maps for Week 2 entities."""

from __future__ import annotations

from app.models.enums import (
    BatchImageStatus,
    BatchProductStatus,
    BatchStatus,
    SecondaryQueueStatus,
)


SECONDARY_TRANSITIONS: dict[SecondaryQueueStatus, set[SecondaryQueueStatus]] = {
    SecondaryQueueStatus.PENDING: {
        SecondaryQueueStatus.CLAIMED,
        SecondaryQueueStatus.CONVERTED,
        SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA,
        SecondaryQueueStatus.FAILED_CONVERSION,
    },
    SecondaryQueueStatus.CLAIMED: {
        SecondaryQueueStatus.CONVERTED,
        SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA,
        SecondaryQueueStatus.FAILED_CONVERSION,
        SecondaryQueueStatus.PENDING,
    },
    SecondaryQueueStatus.CONVERTED: set(),
    SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA: set(),
    SecondaryQueueStatus.FAILED_CONVERSION: {SecondaryQueueStatus.PENDING},
}

BATCH_TRANSITIONS: dict[BatchStatus, set[BatchStatus]] = {
    # Allow terminal outcomes from QUEUED in case work finishes before the batch
    # row was transitioned to PROCESSING (defensive; normal path goes via PROCESSING).
    BatchStatus.QUEUED: {
        BatchStatus.PROCESSING,
        BatchStatus.COMPLETED,
        BatchStatus.PARTIALLY_COMPLETED,
        BatchStatus.FAILED,
        BatchStatus.CANCELLED,
    },
    BatchStatus.PROCESSING: {
        BatchStatus.COMPLETED,
        BatchStatus.PARTIALLY_COMPLETED,
        BatchStatus.FAILED,
        BatchStatus.CANCELLED,
    },
    # Manual reprocess can reopen a finished batch.
    BatchStatus.COMPLETED: {BatchStatus.PROCESSING, BatchStatus.QUEUED},
    BatchStatus.PARTIALLY_COMPLETED: {BatchStatus.PROCESSING, BatchStatus.QUEUED},
    BatchStatus.FAILED: {BatchStatus.PROCESSING, BatchStatus.QUEUED},
    BatchStatus.CANCELLED: set(),
}

BATCH_PRODUCT_TRANSITIONS: dict[BatchProductStatus, set[BatchProductStatus]] = {
    BatchProductStatus.QUEUED: {
        BatchProductStatus.PROCESSING,
        BatchProductStatus.SKIPPED,
        BatchProductStatus.FAILED,
    },
    BatchProductStatus.PROCESSING: {
        BatchProductStatus.COMPLETED,
        BatchProductStatus.FAILED,
        BatchProductStatus.RETRYING,
    },
    BatchProductStatus.RETRYING: {
        BatchProductStatus.PROCESSING,
        BatchProductStatus.FAILED,
        BatchProductStatus.COMPLETED,
    },
    BatchProductStatus.COMPLETED: {BatchProductStatus.QUEUED, BatchProductStatus.RETRYING},
    BatchProductStatus.FAILED: {BatchProductStatus.RETRYING, BatchProductStatus.QUEUED},
    BatchProductStatus.SKIPPED: {BatchProductStatus.QUEUED},
}

BATCH_IMAGE_TRANSITIONS: dict[BatchImageStatus, set[BatchImageStatus]] = {
    BatchImageStatus.QUEUED: {
        BatchImageStatus.DOWNLOADING,
        BatchImageStatus.FAILED,
        BatchImageStatus.RETRYING,
        BatchImageStatus.UPLOADING,
    },
    BatchImageStatus.DOWNLOADING: {
        BatchImageStatus.PROCESSING,
        BatchImageStatus.FAILED,
        BatchImageStatus.RETRYING,
    },
    BatchImageStatus.PROCESSING: {
        BatchImageStatus.WAITING_FOR_PROVIDER,
        BatchImageStatus.UPLOADING,
        BatchImageStatus.FAILED,
        BatchImageStatus.RETRYING,
    },
    BatchImageStatus.WAITING_FOR_PROVIDER: {
        BatchImageStatus.PROCESSING,
        BatchImageStatus.UPLOADING,
        BatchImageStatus.FAILED,
        BatchImageStatus.RETRYING,
    },
    BatchImageStatus.UPLOADING: {
        BatchImageStatus.COMPLETED,
        BatchImageStatus.FAILED,
        BatchImageStatus.RETRYING,
    },
    BatchImageStatus.RETRYING: {
        BatchImageStatus.QUEUED,
        BatchImageStatus.DOWNLOADING,
        BatchImageStatus.UPLOADING,
        BatchImageStatus.FAILED,
    },
    BatchImageStatus.COMPLETED: {BatchImageStatus.QUEUED, BatchImageStatus.RETRYING},
    BatchImageStatus.FAILED: {BatchImageStatus.RETRYING, BatchImageStatus.QUEUED},
}


class InvalidStateTransition(ValueError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"Invalid {entity} transition {current} -> {target}")
        self.entity = entity
        self.current = current
        self.target = target


def assert_transition(entity: str, allowed: dict, current, target) -> None:
    if current == target:
        return
    permitted = allowed.get(current, set())
    if target not in permitted:
        raise InvalidStateTransition(entity, str(current), str(target))

from __future__ import annotations

import enum


class ShopStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class SyncRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SyncRunType(str, enum.Enum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"
    WEBHOOK = "WEBHOOK"


class SecondaryQueueStatus(str, enum.Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    CONVERTED = "CONVERTED"
    SKIPPED_NO_ELIGIBLE_IMAGE_DELTA = "SKIPPED_NO_ELIGIBLE_IMAGE_DELTA"
    FAILED_CONVERSION = "FAILED_CONVERSION"


class BatchStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TriggerType(str, enum.Enum):
    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"
    RETRY = "RETRY"


class BatchProductStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class BatchImageStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeltaType(str, enum.Enum):
    INITIAL = "INITIAL"
    NEW = "NEW"
    REPLACED = "REPLACED"


class AttemptStatus(str, enum.Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class WebhookProcessingResult(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class PromptProductTypeSource(str, enum.Enum):
    SHOPIFY = "SHOPIFY"
    MANUAL = "MANUAL"


class PromptListStatus(str, enum.Enum):
    """Computed status for Prompt Management list filtering/display."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    NOT_READY = "NOT_READY"


class PublishStatus(str, enum.Enum):
    """Collapsed product publish state machine."""

    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    QUEUED = "QUEUED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    PUBLISH_CONFLICT = "PUBLISH_CONFLICT"
    RESTORE_FAILED = "RESTORE_FAILED"


class PublishTriggerSource(str, enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    RETRY = "RETRY"


# Legacy enums kept for old queue compatibility during transition tests
class SourceType(str, enum.Enum):
    SHOPIFY_PRODUCT_MEDIA = "SHOPIFY_PRODUCT_MEDIA"


class QueueItemStatus(str, enum.Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

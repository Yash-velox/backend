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
    UPLOADING = "UPLOADING"
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


class PromptStepType(str, enum.Enum):
    """Discriminates OpenAI endpoint used for a workflow step."""

    IMAGE = "IMAGE"
    DESCRIPTION = "DESCRIPTION"


class AiExecutionMode(str, enum.Enum):
    """Server-side execution path for Primary Queue AI work."""

    SYNC = "SYNC"
    OPENAI_BATCH = "OPENAI_BATCH"


class ProcessingPhase(str, enum.Enum):
    """Detailed Primary Queue phase while top-level status stays PROCESSING."""

    PREPARING_OPENAI_STAGE = "PREPARING_OPENAI_STAGE"
    UPLOADING_BATCH_INPUT = "UPLOADING_BATCH_INPUT"
    OPENAI_BATCH_SUBMITTED = "OPENAI_BATCH_SUBMITTED"
    WAITING_FOR_OPENAI = "WAITING_FOR_OPENAI"
    COLLECTING_OPENAI_RESULTS = "COLLECTING_OPENAI_RESULTS"
    IMPORTING_STAGE_RESULTS = "IMPORTING_STAGE_RESULTS"
    RETRYING_FAILED_REQUESTS = "RETRYING_FAILED_REQUESTS"
    PREPARING_NEXT_STAGE = "PREPARING_NEXT_STAGE"
    AI_WORKFLOW_COMPLETE = "AI_WORKFLOW_COMPLETE"
    UPLOADING_TO_SHOPIFY_FILES = "UPLOADING_TO_SHOPIFY_FILES"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"


class OpenAIBatchStatus(str, enum.Enum):
    """Mirror of OpenAI Platform Batch API statuses plus local draft states."""

    DRAFT = "draft"
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class OpenAIBatchRequestStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OpenAITempFileCleanupStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PENDING_DELETE = "PENDING_DELETE"
    DELETED = "DELETED"
    DELETE_FAILED = "DELETE_FAILED"


class WebhookProcessingResult(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class PromptProductTypeSource(str, enum.Enum):
    SHOPIFY = "SHOPIFY"
    MANUAL = "MANUAL"
    SYSTEM = "SYSTEM"


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


class MediaVersionType(str, enum.Enum):
    ORIGINAL = "ORIGINAL"
    PUBLISHED = "PUBLISHED"
    ROLLBACK = "ROLLBACK"


class ImageVersionType(str, enum.Enum):
    """Per-image lineage version types (product-level still owns PUBLISHED/ROLLBACK)."""

    ORIGINAL = "ORIGINAL"
    GENERATED = "GENERATED"


class ImageVersionEventType(str, enum.Enum):
    ORIGINAL_REGISTERED = "ORIGINAL_REGISTERED"
    VERSION_GENERATED = "VERSION_GENERATED"
    VERSION_UPLOADED = "VERSION_UPLOADED"
    VERSION_PUBLISHED = "VERSION_PUBLISHED"
    VERSION_SUPERSEDED = "VERSION_SUPERSEDED"
    VERSION_INCLUDED_IN_PRODUCT_SNAPSHOT = "VERSION_INCLUDED_IN_PRODUCT_SNAPSHOT"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class RollbackStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    ROLLBACK_CONFLICT = "ROLLBACK_CONFLICT"
    RESTORE_FAILED = "RESTORE_FAILED"


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

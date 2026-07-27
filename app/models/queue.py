from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base


class ShopStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


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


class BatchStatus(str, enum.Enum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TriggerType(str, enum.Enum):
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"
    RETRY = "RETRY"


class AttemptStatus(str, enum.Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ShopStatus] = mapped_column(
        Enum(ShopStatus, name="shop_status", native_enum=False),
        nullable=False,
        default=ShopStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    queue_items: Mapped[list[ProcessingQueueItem]] = relationship(back_populates="shop")
    batches: Mapped[list[ProcessingBatch]] = relationship(back_populates="shop")


class ProcessingBatch(Base):
    __tablename__ = "processing_batches"
    __table_args__ = (
        Index("ix_processing_batches_shop_status", "shop_id", "status"),
        Index("ix_processing_batches_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_type: Mapped[TriggerType] = mapped_column(
        Enum(TriggerType, name="trigger_type", native_enum=False), nullable=False
    )
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, name="batch_status", native_enum=False),
        nullable=False,
        default=BatchStatus.CREATED,
        index=True,
    )
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_pending_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop: Mapped[Shop] = relationship(back_populates="batches")
    items: Mapped[list[ProcessingQueueItem]] = relationship(back_populates="batch")
    attempts: Mapped[list[ProcessingAttempt]] = relationship(back_populates="batch")


class ProcessingQueueItem(Base):
    __tablename__ = "processing_queue_items"
    __table_args__ = (
        Index("ix_queue_items_claim", "shop_id", "status", "priority", "created_at"),
        Index("ix_queue_items_shopify_product", "shopify_product_id"),
        Index("ix_queue_items_shopify_media", "shopify_media_id"),
        Index("ix_queue_items_next_retry", "next_retry_at"),
        Index("ix_queue_items_batch_id", "batch_id"),
        Index(
            "ix_queue_items_active_dup",
            "shop_id",
            "shopify_product_id",
            "shopify_media_id",
            "processing_config_fingerprint",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type", native_enum=False),
        nullable=False,
        default=SourceType.SHOPIFY_PRODUCT_MEDIA,
    )
    shopify_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_media_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_image_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_cdn_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[QueueItemStatus] = mapped_column(
        Enum(QueueItemStatus, name="queue_item_status", native_enum=False),
        nullable=False,
        default=QueueItemStatus.PENDING,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_batches.id", ondelete="SET NULL"), nullable=True
    )
    processing_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    prompt_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    output_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop: Mapped[Shop] = relationship(back_populates="queue_items")
    batch: Mapped[ProcessingBatch | None] = relationship(back_populates="items")
    attempts: Mapped[list[ProcessingAttempt]] = relationship(
        back_populates="queue_item", order_by="ProcessingAttempt.attempt_number"
    )


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"
    __table_args__ = (
        Index("ix_processing_attempts_queue_item", "queue_item_id"),
        Index("ix_processing_attempts_batch", "batch_id"),
        UniqueConstraint("queue_item_id", "attempt_number", name="uq_attempt_item_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    queue_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_queue_items.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_batches.id", ondelete="SET NULL"), nullable=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, name="attempt_status", native_enum=False),
        nullable=False,
        default=AttemptStatus.STARTED,
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    queue_item: Mapped[ProcessingQueueItem] = relationship(back_populates="attempts")
    batch: Mapped[ProcessingBatch | None] = relationship(back_populates="attempts")

"""OpenAI Platform Batch API persistence (distinct from Primary Queue batches)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.enums import (
    OpenAIBatchRequestStatus,
    OpenAIBatchStatus,
    OpenAITempFileCleanupStatus,
    PromptStepType,
)


class OpenAIBatch(Base):
    """One OpenAI Platform batch job for a single Primary Queue workflow stage."""

    __tablename__ = "openai_batches"
    __table_args__ = (
        Index("ix_openai_batches_shop_primary", "shop_id", "primary_batch_id"),
        Index("ix_openai_batches_status", "status"),
        Index("ix_openai_batches_openai_id", "openai_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    primary_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    workflow_step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[PromptStepType] = mapped_column(
        Enum(PromptStepType, name="prompt_step_type", native_enum=False),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    openai_batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    openai_input_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    openai_output_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    openai_error_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[OpenAIBatchStatus] = mapped_column(
        Enum(OpenAIBatchStatus, name="openai_batch_status", native_enum=False),
        nullable=False,
        default=OpenAIBatchStatus.DRAFT,
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_openai_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("openai_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    requests: Mapped[list[OpenAIBatchRequest]] = relationship(
        back_populates="openai_batch",
        cascade="all, delete-orphan",
    )


class OpenAIBatchRequest(Base):
    """One request line inside an OpenAI Platform batch, mapped by custom_id."""

    __tablename__ = "openai_batch_requests"
    __table_args__ = (
        UniqueConstraint("openai_batch_id", "custom_id", name="uq_openai_batch_request_custom_id"),
        Index("ix_openai_batch_requests_batch_image", "batch_image_id"),
        Index("ix_openai_batch_requests_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    openai_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("openai_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    custom_id: Mapped[str] = mapped_column(String(255), nullable=False)
    batch_image_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("batch_images.id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("batch_products.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_media_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    workflow_step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[OpenAIBatchRequestStatus] = mapped_column(
        Enum(OpenAIBatchRequestStatus, name="openai_batch_request_status", native_enum=False),
        nullable=False,
        default=OpenAIBatchRequestStatus.PENDING,
    )
    input_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    openai_batch: Mapped[OpenAIBatch] = relationship(back_populates="requests")


class OpenAITemporaryFile(Base):
    """Intermediate OpenAI Files used between sequential workflow stages."""

    __tablename__ = "openai_temporary_files"
    __table_args__ = (
        Index("ix_openai_temp_files_cleanup", "cleanup_status", "expires_at"),
        Index("ix_openai_temp_files_batch_image", "batch_image_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    openai_file_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    workflow_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_image_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("batch_images.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_status: Mapped[OpenAITempFileCleanupStatus] = mapped_column(
        Enum(OpenAITempFileCleanupStatus, name="openai_temp_file_cleanup_status", native_enum=False),
        nullable=False,
        default=OpenAITempFileCleanupStatus.ACTIVE,
    )
    cleanup_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)

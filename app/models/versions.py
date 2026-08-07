"""Product media version history and rollback operation models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.enums import MediaVersionType, RollbackStatus


class ProductMediaVersion(Base):
    __tablename__ = "product_media_versions"
    __table_args__ = (
        UniqueConstraint("product_id", "version_number", name="uq_product_media_versions_number"),
        Index("ix_product_media_versions_shop_gid", "shop_id", "shopify_product_gid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_type: Mapped[MediaVersionType] = mapped_column(
        Enum(MediaVersionType, name="media_version_type", native_enum=False),
        nullable=False,
    )
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_media_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    processing_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    publish_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_publish_operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    rollback_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rollback_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    items_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prompt_snapshot_json: Mapped[dict[str, Any] | list | None] = mapped_column(JSON, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop = relationship("Shop")
    product = relationship("Product")


class ProductRollbackOperation(Base):
    __tablename__ = "product_rollback_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_product_rollback_operations_idempotency"),
        Index("ix_product_rollback_operations_claim", "status", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    from_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_media_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_media_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_media_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[RollbackStatus] = mapped_column(
        Enum(RollbackStatus, name="rollback_status", native_enum=False),
        nullable=False,
        default=RollbackStatus.QUEUED,
    )
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    pre_rollback_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conflict_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop = relationship("Shop")
    product = relationship("Product")
    from_version = relationship("ProductMediaVersion", foreign_keys=[from_version_id])
    target_version = relationship("ProductMediaVersion", foreign_keys=[target_version_id])
    result_version = relationship("ProductMediaVersion", foreign_keys=[result_version_id])

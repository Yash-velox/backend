from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.enums import PublishStatus, PublishTriggerSource


class ProductPublishOperation(Base):
    __tablename__ = "product_publish_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_product_publish_operations_idempotency"),
        Index("ix_product_publish_operations_shop_status", "shop_id", "status"),
        Index("ix_product_publish_operations_batch", "processing_batch_id"),
        Index("ix_product_publish_operations_batch_product", "batch_product_id"),
        Index("ix_product_publish_operations_product_gid", "shop_id", "shopify_product_gid"),
        Index("ix_product_publish_operations_claim", "status", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    processing_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_batches.id", ondelete="CASCADE"), nullable=False
    )
    batch_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_products.id", ondelete="CASCADE"), nullable=False
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[PublishStatus] = mapped_column(
        Enum(PublishStatus, name="publish_status", native_enum=False),
        nullable=False,
        default=PublishStatus.QUEUED,
        index=True,
    )
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_source: Mapped[PublishTriggerSource] = mapped_column(
        Enum(PublishTriggerSource, name="publish_trigger_source", native_enum=False),
        nullable=False,
        default=PublishTriggerSource.MANUAL,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    output_set_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    baseline_snapshot_json: Mapped[dict[str, Any] | list | None] = mapped_column(JSON, nullable=True)
    pre_publish_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conflict_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assets_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop = relationship("Shop")
    batch = relationship("ProcessingBatch")
    batch_product = relationship("BatchProduct")

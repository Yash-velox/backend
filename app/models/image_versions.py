"""Normalized per-image Shopify CDN version history (under product-level snapshots)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.enums import ImageVersionEventType, ImageVersionType


class ImageVersion(Base):
    __tablename__ = "image_versions"
    __table_args__ = (
        UniqueConstraint(
            "shop_id",
            "product_id",
            "source_media_gid",
            "version_number",
            name="uq_image_versions_lineage_number",
        ),
        Index("ix_image_versions_shop_product", "shop_id", "product_id"),
        Index("ix_image_versions_source_media", "shop_id", "product_id", "source_media_gid"),
        Index("ix_image_versions_file_gid", "shop_id", "shopify_file_gid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_media_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("image_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_type: Mapped[ImageVersionType] = mapped_column(
        Enum(ImageVersionType, name="image_version_type", native_enum=False),
        nullable=False,
    )
    shopify_file_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_media_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_cdn_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stored_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_original: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_batch_image_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("batch_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    product_media_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_media_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    upload_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop = relationship("Shop")
    product = relationship("Product")
    parent_version = relationship("ImageVersion", remote_side=[id], foreign_keys=[parent_version_id])
    product_media_version = relationship("ProductMediaVersion", foreign_keys=[product_media_version_id])


class ImageVersionEvent(Base):
    __tablename__ = "image_version_events"
    __table_args__ = (
        Index("ix_image_version_events_version", "image_version_id", "created_at"),
        Index("ix_image_version_events_shop_product", "shop_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    image_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("image_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[ImageVersionEventType] = mapped_column(
        Enum(ImageVersionEventType, name="image_version_event_type", native_enum=False),
        nullable=False,
    )
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    new_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    product_media_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    shop = relationship("Shop")
    product = relationship("Product")
    image_version = relationship("ImageVersion", foreign_keys=[image_version_id])

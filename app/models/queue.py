from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.enums import (
    AttemptStatus,
    BatchImageStatus,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    PublishStatus,
    SecondaryQueueStatus,
    ShopStatus,
    SyncRunStatus,
    SyncRunType,
    TriggerType,
    WebhookProcessingResult,
)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Encrypted offline Admin API token (Fernet). Legacy plaintext column removed in Week 2 migration.
    encrypted_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ShopStatus] = mapped_column(
        Enum(ShopStatus, name="shop_status", native_enum=False),
        nullable=False,
        default=ShopStatus.ACTIVE,
    )
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    settings: Mapped[ShopSettings | None] = relationship(back_populates="shop", uselist=False)
    products: Mapped[list[Product]] = relationship(back_populates="shop")
    batches: Mapped[list[ProcessingBatch]] = relationship(back_populates="shop")


class ShopSettings(Base):
    __tablename__ = "shop_settings"
    __table_args__ = (UniqueConstraint("shop_id", name="uq_shop_settings_shop"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_publish_processed_images: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    batch_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop: Mapped[Shop] = relationship(back_populates="settings")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_shop_status", "shop_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_type: Mapped[SyncRunType] = mapped_column(
        Enum(SyncRunType, name="sync_run_type", native_enum=False), nullable=False
    )
    status: Mapped[SyncRunStatus] = mapped_column(
        Enum(SyncRunStatus, name="sync_run_status", native_enum=False),
        nullable=False,
        default=SyncRunStatus.PENDING,
    )
    products_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("shop_id", "shopify_product_gid", name="uq_products_shop_gid"),
        Index("ix_products_shop_updated", "shop_id", "shopify_updated_at"),
        Index("ix_products_shop_status", "shop_id", "status"),
        Index("ix_products_shop_has_images", "shop_id", "has_images"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_numeric_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Set at catalog sync: True when product has eligible visible media (active + visible + cdn_url).
    has_images: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    shop: Mapped[Shop] = relationship(back_populates="products")
    variants: Mapped[list[ProductVariant]] = relationship(back_populates="product", cascade="all, delete-orphan")
    media: Mapped[list[ProductMedia]] = relationship(back_populates="product", cascade="all, delete-orphan")
    baseline: Mapped[ProcessingBaseline | None] = relationship(
        back_populates="product", uselist=False, cascade="all, delete-orphan"
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("shop_id", "shopify_variant_gid", name="uq_variants_shop_gid"),
        Index("ix_variants_product", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    shopify_variant_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    product: Mapped[Product] = relationship(back_populates="variants")


class ShopifyFile(Base):
    __tablename__ = "shopify_files"
    __table_args__ = (UniqueConstraint("shop_id", "shopify_file_gid", name="uq_files_shop_gid"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_file_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    media_links: Mapped[list[ProductMedia]] = relationship(back_populates="shopify_file")


class ProductMedia(Base):
    __tablename__ = "product_media"
    __table_args__ = (
        UniqueConstraint("shop_id", "shopify_media_gid", "product_id", name="uq_product_media_rel"),
        Index("ix_product_media_product", "product_id", "position"),
        Index("ix_product_media_file", "shopify_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    shopify_media_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shopify_files.id", ondelete="SET NULL"), nullable=True
    )
    shopify_file_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cdn_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    variant_gids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    product: Mapped[Product] = relationship(back_populates="media")
    shopify_file: Mapped[ShopifyFile | None] = relationship(back_populates="media_links")


class ProcessingBaseline(Base):
    """Last evaluated/successfully processed product+media state for delta comparison."""

    __tablename__ = "processing_baselines"
    __table_args__ = (UniqueConstraint("shop_id", "product_id", name="uq_baseline_shop_product"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    product_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    media_snapshot_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    successfully_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    product: Mapped[Product] = relationship(back_populates="baseline")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("shopify_webhook_id", name="uq_webhook_events_shopify_id"),
        Index("ix_webhook_events_shop_topic", "shop_id", "topic"),
        Index("ix_webhook_events_result_received", "processing_result", "received_at"),
        Index(
            "ix_webhook_events_shop_product_result",
            "shop_id",
            "shopify_product_gid",
            "processing_result",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shopify_webhook_id: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_product_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_result: Mapped[WebhookProcessingResult] = mapped_column(
        Enum(WebhookProcessingResult, name="webhook_processing_result", native_enum=False),
        nullable=False,
        default=WebhookProcessingResult.QUEUED,
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SecondaryQueueItem(Base):
    __tablename__ = "secondary_queue_items"
    __table_args__ = (
        Index("ix_secondary_queue_claim", "shop_id", "status", "first_queued_at"),
        Index("ix_secondary_queue_product", "shop_id", "shopify_product_gid"),
        Index("ix_secondary_queue_batch", "converted_batch_id"),
        # At most one PENDING item per shop+product (enforced in app + partial unique when PG).
        Index(
            "uq_secondary_pending_shop_product",
            "shop_id",
            "shopify_product_gid",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    queue_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[SecondaryQueueStatus] = mapped_column(
        Enum(SecondaryQueueStatus, name="secondary_queue_status", native_enum=False),
        nullable=False,
        default=SecondaryQueueStatus.PENDING,
        index=True,
    )
    eligible_product_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    eligible_media_snapshot_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    first_queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    latest_eligible_webhook_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    webhook_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    conversion_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_batches.id", ondelete="SET NULL"), nullable=True
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


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
        default=BatchStatus.QUEUED,
        index=True,
    )
    processing_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_workflow_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_workflow_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    openai_requests_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    openai_requests_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    openai_requests_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_openai_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrying_product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settings_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    products: Mapped[list[BatchProduct]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class BatchProduct(Base):
    __tablename__ = "batch_products"
    __table_args__ = (
        UniqueConstraint("batch_id", "shopify_product_gid", name="uq_batch_product"),
        Index("ix_batch_products_batch_status", "batch_id", "status"),
        Index("ix_batch_products_claim", "status", "claimed_at"),
        Index("ix_batch_products_shop_product_status", "shop_id", "shopify_product_gid", "status"),
        # At most one QUEUED Primary generation per shop+product (PROCESSING + QUEUED is allowed).
        Index(
            "uq_batch_product_queued_shop_product",
            "shop_id",
            "shopify_product_gid",
            unique=True,
            sqlite_where=text("status = 'QUEUED'"),
            postgresql_where=text("status = 'QUEUED'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("processing_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_product_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    product_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    prompt_snapshot_json: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    prompt_override_json: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    baseline_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[BatchProductStatus] = mapped_column(
        Enum(BatchProductStatus, name="batch_product_status", native_enum=False),
        nullable=False,
        default=BatchProductStatus.QUEUED,
        index=True,
    )
    publish_status: Mapped[PublishStatus | None] = mapped_column(
        Enum(PublishStatus, name="batch_product_publish_status", native_enum=False),
        nullable=True,
        index=True,
    )
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    batch: Mapped[ProcessingBatch] = relationship(back_populates="products")
    images: Mapped[list[BatchImage]] = relationship(back_populates="batch_product", cascade="all, delete-orphan")


class BatchImage(Base):
    __tablename__ = "batch_images"
    __table_args__ = (
        UniqueConstraint(
            "batch_product_id",
            "shopify_media_gid",
            "delta_type",
            name="uq_batch_image_source_delta",
        ),
        Index("ix_batch_images_product_status", "batch_product_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shopify_media_gid: Mapped[str] = mapped_column(String(128), nullable=False)
    shopify_file_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cdn_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delta_type: Mapped[DeltaType] = mapped_column(
        Enum(DeltaType, name="delta_type", native_enum=False), nullable=False
    )
    current_prompt_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_override_json: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    pending_description_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_openai_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[BatchImageStatus] = mapped_column(
        Enum(BatchImageStatus, name="batch_image_status", native_enum=False),
        nullable=False,
        default=BatchImageStatus.QUEUED,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    generated_shopify_file_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    generated_shopify_cdn_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_image_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    manual_reprocess: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    batch_product: Mapped[BatchProduct] = relationship(back_populates="images")
    attempts: Mapped[list[ProcessingAttempt]] = relationship(
        back_populates="batch_image", order_by="ProcessingAttempt.attempt_number", cascade="all, delete-orphan"
    )


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"
    __table_args__ = (
        Index("ix_processing_attempts_batch_image", "batch_image_id"),
        UniqueConstraint("batch_image_id", "attempt_number", name="uq_attempt_image_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_image_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_images.id", ondelete="CASCADE"), nullable=False
    )
    batch_product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_products.id", ondelete="SET NULL"), nullable=True
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
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch_image: Mapped[BatchImage] = relationship(back_populates="attempts")

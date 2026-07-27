"""Initial shops and processing queue tables.

Revision ID: 001_processing_queue
Revises:
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_processing_queue"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_domain", sa.String(length=255), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("ACTIVE", "INACTIVE", name="shop_status", native_enum=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_domain"),
    )
    op.create_index("ix_shops_shop_domain", "shops", ["shop_domain"])

    op.create_table(
        "processing_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column(
            "trigger_type",
            sa.Enum("AUTOMATIC", "MANUAL", "RETRY", name="trigger_type", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "PROCESSING",
                "COMPLETED",
                "PARTIALLY_COMPLETED",
                "FAILED",
                "CANCELLED",
                name="batch_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("pending_items", sa.Integer(), nullable=False),
        sa.Column("queued_items", sa.Integer(), nullable=False),
        sa.Column("processing_items", sa.Integer(), nullable=False),
        sa.Column("completed_items", sa.Integer(), nullable=False),
        sa.Column("retry_pending_items", sa.Integer(), nullable=False),
        sa.Column("failed_items", sa.Integer(), nullable=False),
        sa.Column("cancelled_items", sa.Integer(), nullable=False),
        sa.Column("started_by", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processing_batches_shop_id", "processing_batches", ["shop_id"])
    op.create_index("ix_processing_batches_status", "processing_batches", ["status"])
    op.create_index("ix_processing_batches_shop_status", "processing_batches", ["shop_id", "status"])
    op.create_index("ix_processing_batches_created_at", "processing_batches", ["created_at"])

    op.create_table(
        "processing_queue_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("SHOPIFY_PRODUCT_MEDIA", name="source_type", native_enum=False),
            nullable=False,
        ),
        sa.Column("shopify_product_id", sa.String(length=128), nullable=False),
        sa.Column("shopify_media_id", sa.String(length=128), nullable=False),
        sa.Column("shopify_image_id", sa.String(length=128), nullable=True),
        sa.Column("shopify_cdn_url", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("source_mime_type", sa.String(length=128), nullable=True),
        sa.Column("source_width", sa.Integer(), nullable=True),
        sa.Column("source_height", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "QUEUED",
                "PROCESSING",
                "COMPLETED",
                "RETRY_PENDING",
                "FAILED",
                "CANCELLED",
                name="queue_item_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("processing_config", sa.JSON(), nullable=True),
        sa.Column("processing_config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_data", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("output_storage_key", sa.String(length=1024), nullable=True),
        sa.Column("output_url", sa.Text(), nullable=True),
        sa.Column("output_mime_type", sa.String(length=128), nullable=True),
        sa.Column("output_checksum", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["processing_batches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processing_queue_items_shop_id", "processing_queue_items", ["shop_id"])
    op.create_index("ix_processing_queue_items_status", "processing_queue_items", ["status"])
    op.create_index("ix_processing_queue_items_priority", "processing_queue_items", ["priority"])
    op.create_index("ix_queue_items_claim", "processing_queue_items", ["shop_id", "status", "priority", "created_at"])
    op.create_index("ix_queue_items_shopify_product", "processing_queue_items", ["shopify_product_id"])
    op.create_index("ix_queue_items_shopify_media", "processing_queue_items", ["shopify_media_id"])
    op.create_index("ix_queue_items_next_retry", "processing_queue_items", ["next_retry_at"])
    op.create_index("ix_queue_items_batch_id", "processing_queue_items", ["batch_id"])
    op.create_index(
        "ix_queue_items_active_dup",
        "processing_queue_items",
        ["shop_id", "shopify_product_id", "shopify_media_id", "processing_config_fingerprint", "status"],
    )

    op.create_table(
        "processing_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue_item_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("STARTED", "COMPLETED", "FAILED", "INTERRUPTED", name="attempt_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("shopify_source_url", sa.Text(), nullable=True),
        sa.Column("output_storage_key", sa.String(length=1024), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["processing_batches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["queue_item_id"], ["processing_queue_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("queue_item_id", "attempt_number", name="uq_attempt_item_number"),
    )
    op.create_index("ix_processing_attempts_queue_item", "processing_attempts", ["queue_item_id"])
    op.create_index("ix_processing_attempts_batch", "processing_attempts", ["batch_id"])


def downgrade() -> None:
    op.drop_table("processing_attempts")
    op.drop_table("processing_queue_items")
    op.drop_table("processing_batches")
    op.drop_table("shops")

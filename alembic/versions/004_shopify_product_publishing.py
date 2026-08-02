"""Shopify product publishing tables and settings.

Revision ID: 004_shopify_product_publishing
Revises: 003_prompt_management
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_shopify_product_publishing"
down_revision: Union[str, Sequence[str], None] = "003_prompt_management"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shop_settings",
        sa.Column(
            "auto_publish_processed_images",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "batch_products",
        sa.Column("publish_status", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_batch_products_publish_status",
        "batch_products",
        ["publish_status"],
    )

    op.create_table(
        "product_publish_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "processing_batch_id",
            sa.Uuid(),
            sa.ForeignKey("processing_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "batch_product_id",
            sa.Uuid(),
            sa.ForeignKey("batch_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_stage", sa.String(64), nullable=True),
        sa.Column("trigger_source", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("output_set_checksum", sa.String(128), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("baseline_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("pre_publish_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("conflict_details", sa.JSON(), nullable=True),
        sa.Column("assets_json", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_product_publish_operations_idempotency"),
    )
    op.create_index("ix_product_publish_operations_shop_id", "product_publish_operations", ["shop_id"])
    op.create_index("ix_product_publish_operations_status", "product_publish_operations", ["status"])
    op.create_index(
        "ix_product_publish_operations_shop_status",
        "product_publish_operations",
        ["shop_id", "status"],
    )
    op.create_index(
        "ix_product_publish_operations_batch",
        "product_publish_operations",
        ["processing_batch_id"],
    )
    op.create_index(
        "ix_product_publish_operations_batch_product",
        "product_publish_operations",
        ["batch_product_id"],
    )
    op.create_index(
        "ix_product_publish_operations_product_gid",
        "product_publish_operations",
        ["shop_id", "shopify_product_gid"],
    )
    op.create_index(
        "ix_product_publish_operations_claim",
        "product_publish_operations",
        ["status", "queued_at"],
    )
    # Partial unique: at most one active publish op per batch product (PostgreSQL).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_publish_ops_active_batch_product
        ON product_publish_operations (batch_product_id)
        WHERE status IN ('QUEUED', 'PUBLISHING')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_publish_ops_active_batch_product")
    op.drop_index("ix_product_publish_operations_claim", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_product_gid", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_batch_product", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_batch", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_shop_status", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_status", table_name="product_publish_operations")
    op.drop_index("ix_product_publish_operations_shop_id", table_name="product_publish_operations")
    op.drop_table("product_publish_operations")
    op.drop_index("ix_batch_products_publish_status", table_name="batch_products")
    op.drop_column("batch_products", "publish_status")
    op.drop_column("shop_settings", "auto_publish_processed_images")

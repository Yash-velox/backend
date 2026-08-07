"""Product media versions and rollback operations.

Revision ID: 005_media_versions_rollback
Revises: 004_shopify_product_publishing
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_media_versions_rollback"
down_revision: Union[str, Sequence[str], None] = "004_shopify_product_publishing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_media_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_type", sa.String(32), nullable=False),
        sa.Column(
            "source_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_media_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "processing_batch_id",
            sa.Uuid(),
            sa.ForeignKey("processing_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "publish_operation_id",
            sa.Uuid(),
            sa.ForeignKey("product_publish_operations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rollback_operation_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rollback_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.String(128), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("prompt_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("quality", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("product_id", "version_number", name="uq_product_media_versions_number"),
    )
    op.create_index("ix_product_media_versions_shop_id", "product_media_versions", ["shop_id"])
    op.create_index(
        "ix_product_media_versions_product",
        "product_media_versions",
        ["product_id"],
    )
    op.create_index(
        "ix_product_media_versions_shop_gid",
        "product_media_versions",
        ["shop_id", "shopify_product_gid"],
    )
    op.create_index(
        "ix_product_media_versions_active",
        "product_media_versions",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )

    op.create_table(
        "product_rollback_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column(
            "from_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_media_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_media_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "result_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_media_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_stage", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("pre_rollback_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("conflict_details", sa.JSON(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_product_rollback_operations_idempotency"),
    )
    op.create_index("ix_product_rollback_operations_shop_id", "product_rollback_operations", ["shop_id"])
    op.create_index(
        "ix_product_rollback_operations_product",
        "product_rollback_operations",
        ["product_id"],
    )
    op.create_index(
        "ix_product_rollback_operations_status",
        "product_rollback_operations",
        ["status"],
    )
    op.create_index(
        "ix_product_rollback_operations_claim",
        "product_rollback_operations",
        ["status", "queued_at"],
    )
    op.create_index(
        "ix_product_rollback_ops_active_product",
        "product_rollback_operations",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'ROLLING_BACK')"),
    )

    # FK from versions.rollback_operation_id after rollback table exists
    op.create_foreign_key(
        "fk_product_media_versions_rollback_op",
        "product_media_versions",
        "product_rollback_operations",
        ["rollback_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_product_media_versions_rollback_op",
        "product_media_versions",
        type_="foreignkey",
    )
    op.drop_index("ix_product_rollback_ops_active_product", table_name="product_rollback_operations")
    op.drop_index("ix_product_rollback_operations_claim", table_name="product_rollback_operations")
    op.drop_index("ix_product_rollback_operations_status", table_name="product_rollback_operations")
    op.drop_index("ix_product_rollback_operations_product", table_name="product_rollback_operations")
    op.drop_index("ix_product_rollback_operations_shop_id", table_name="product_rollback_operations")
    op.drop_table("product_rollback_operations")

    op.drop_index("ix_product_media_versions_active", table_name="product_media_versions")
    op.drop_index("ix_product_media_versions_shop_gid", table_name="product_media_versions")
    op.drop_index("ix_product_media_versions_product", table_name="product_media_versions")
    op.drop_index("ix_product_media_versions_shop_id", table_name="product_media_versions")
    op.drop_table("product_media_versions")

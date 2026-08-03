"""OpenAI Batch API tables and Primary Queue processing phase fields.

Revision ID: 008_openai_batch_api
Revises: 007_prompt_override_reprocess
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_openai_batch_api"
down_revision: Union[str, Sequence[str], None] = "007_prompt_override_reprocess"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("processing_batches", sa.Column("processing_phase", sa.String(length=64), nullable=True))
    op.add_column(
        "processing_batches",
        sa.Column("current_workflow_step", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processing_batches",
        sa.Column("total_workflow_steps", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processing_batches",
        sa.Column("openai_requests_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processing_batches",
        sa.Column("openai_requests_completed", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processing_batches",
        sa.Column("openai_requests_failed", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("processing_batches", sa.Column("active_openai_batch_id", sa.Uuid(as_uuid=True), nullable=True))

    op.add_column(
        "prompt_steps",
        sa.Column("step_type", sa.String(length=32), nullable=False, server_default="IMAGE"),
    )

    op.add_column("batch_images", sa.Column("pending_description_context", sa.Text(), nullable=True))
    op.add_column("batch_images", sa.Column("current_openai_file_id", sa.String(length=128), nullable=True))

    op.create_table(
        "openai_batches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "primary_batch_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("processing_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_step_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("workflow_step_order", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("openai_batch_id", sa.String(length=128), nullable=True),
        sa.Column("openai_input_file_id", sa.String(length=128), nullable=True),
        sa.Column("openai_output_file_id", sa.String(length=128), nullable=True),
        sa.Column("openai_error_file_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_retry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "parent_openai_batch_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("openai_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_openai_batches_shop_id", "openai_batches", ["shop_id"])
    op.create_index("ix_openai_batches_primary_batch_id", "openai_batches", ["primary_batch_id"])
    op.create_index("ix_openai_batches_shop_primary", "openai_batches", ["shop_id", "primary_batch_id"])
    op.create_index("ix_openai_batches_status", "openai_batches", ["status"])
    op.create_index("ix_openai_batches_openai_id", "openai_batches", ["openai_batch_id"])

    op.create_table(
        "openai_batch_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "openai_batch_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("openai_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("custom_id", sa.String(length=255), nullable=False),
        sa.Column(
            "batch_image_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("batch_images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "batch_product_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("batch_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_media_gid", sa.String(length=128), nullable=False),
        sa.Column("workflow_step_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("workflow_step_order", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_reference", sa.Text(), nullable=True),
        sa.Column("output_reference", sa.Text(), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("openai_batch_id", "custom_id", name="uq_openai_batch_request_custom_id"),
    )
    op.create_index("ix_openai_batch_requests_openai_batch_id", "openai_batch_requests", ["openai_batch_id"])
    op.create_index("ix_openai_batch_requests_batch_image", "openai_batch_requests", ["batch_image_id"])
    op.create_index("ix_openai_batch_requests_status", "openai_batch_requests", ["status"])

    op.create_table(
        "openai_temporary_files",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("openai_file_id", sa.String(length=128), nullable=False),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("workflow_step_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("workflow_step_order", sa.Integer(), nullable=True),
        sa.Column(
            "batch_image_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("batch_images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_status", sa.String(length=32), nullable=False),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_cleanup_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("openai_file_id", name="uq_openai_temporary_files_file_id"),
    )
    op.create_index("ix_openai_temp_files_shop_id", "openai_temporary_files", ["shop_id"])
    op.create_index("ix_openai_temp_files_cleanup", "openai_temporary_files", ["cleanup_status", "expires_at"])
    op.create_index("ix_openai_temp_files_batch_image", "openai_temporary_files", ["batch_image_id"])


def downgrade() -> None:
    op.drop_table("openai_temporary_files")
    op.drop_table("openai_batch_requests")
    op.drop_table("openai_batches")
    op.drop_column("batch_images", "current_openai_file_id")
    op.drop_column("batch_images", "pending_description_context")
    op.drop_column("prompt_steps", "step_type")
    op.drop_column("processing_batches", "active_openai_batch_id")
    op.drop_column("processing_batches", "openai_requests_failed")
    op.drop_column("processing_batches", "openai_requests_completed")
    op.drop_column("processing_batches", "openai_requests_total")
    op.drop_column("processing_batches", "total_workflow_steps")
    op.drop_column("processing_batches", "current_workflow_step")
    op.drop_column("processing_batches", "processing_phase")

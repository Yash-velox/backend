"""Week 2 domain schema - resets incompatible queue-phase tables.

Revision ID: 002_week2_domain
Revises: 001_processing_queue
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_week2_domain"
down_revision: Union[str, Sequence[str], None] = "001_processing_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Development reset: drop incompatible image-level queue tables.
    op.drop_table("processing_attempts")
    op.drop_table("processing_queue_items")
    op.drop_table("processing_batches")

    # Rebuild shops with encrypted token columns.
    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("encrypted_access_token", sa.Text(), nullable=True))
        batch.add_column(sa.Column("encrypted_refresh_token", sa.Text(), nullable=True))
        batch.add_column(sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("scopes", sa.Text(), nullable=True))
        batch.add_column(sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("uninstalled_at", sa.DateTime(timezone=True), nullable=True))

    # Best-effort migrate plaintext tokens into encrypted column name (values remain until handoff re-encrypts).
    # Leave access_token for one revision then drop.
    conn = op.get_bind()
    try:
        conn.execute(sa.text("UPDATE shops SET encrypted_access_token = access_token WHERE access_token IS NOT NULL"))
    except Exception:
        pass

    with op.batch_alter_table("shops") as batch:
        batch.drop_column("access_token")

    op.create_table(
        "shop_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_products_per_batch", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("batch_interval_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", name="uq_shop_settings_shop"),
    )
    op.create_index("ix_shop_settings_shop_id", "shop_settings", ["shop_id"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("products_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("media_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sync_runs_shop_status", "sync_runs", ["shop_id", "status"])

    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column("shopify_numeric_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(1024), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("handle", sa.String(255), nullable=True),
        sa.Column("status", sa.String(64), nullable=True),
        sa.Column("product_type", sa.String(255), nullable=True),
        sa.Column("vendor", sa.String(255), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("category_json", sa.JSON(), nullable=True),
        sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "shopify_product_gid", name="uq_products_shop_gid"),
    )
    op.create_index("ix_products_shop_updated", "products", ["shop_id", "shopify_updated_at"])
    op.create_index("ix_products_shop_status", "products", ["shop_id", "status"])

    op.create_table(
        "product_variants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_variant_gid", sa.String(128), nullable=False),
        sa.Column("sku", sa.String(255), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "shopify_variant_gid", name="uq_variants_shop_gid"),
    )

    op.create_table(
        "shopify_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_file_gid", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("content_fingerprint", sa.String(128), nullable=True),
        sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "shopify_file_gid", name="uq_files_shop_gid"),
    )

    op.create_table(
        "product_media",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_media_gid", sa.String(128), nullable=False),
        sa.Column("shopify_file_id", sa.Uuid(), sa.ForeignKey("shopify_files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("shopify_file_gid", sa.String(128), nullable=True),
        sa.Column("cdn_url", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.String(512), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("variant_gids_json", sa.JSON(), nullable=True),
        sa.Column("content_fingerprint", sa.String(128), nullable=True),
        sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "shopify_media_gid", "product_id", name="uq_product_media_rel"),
    )

    op.create_table(
        "processing_baselines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("media_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("successfully_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "product_id", name="uq_baseline_shop_product"),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="SET NULL"), nullable=True),
        sa.Column("shopify_webhook_id", sa.String(128), nullable=False),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=True),
        sa.Column("payload_hash", sa.String(128), nullable=True),
        sa.Column("processing_result", sa.String(32), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shopify_webhook_id", name="uq_webhook_events_shopify_id"),
    )

    op.create_table(
        "processing_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processing_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retrying_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("settings_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_processing_batches_shop_status", "processing_batches", ["shop_id", "status"])

    op.create_table(
        "secondary_queue_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("queue_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("eligible_product_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("eligible_media_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("first_queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("latest_eligible_webhook_id", sa.String(128), nullable=True),
        sa.Column("webhook_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("conversion_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "converted_batch_id",
            sa.Uuid(),
            sa.ForeignKey("processing_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_secondary_queue_claim",
        "secondary_queue_items",
        ["shop_id", "status", "first_queued_at"],
    )
    op.create_index(
        "ix_secondary_queue_product",
        "secondary_queue_items",
        ["shop_id", "shopify_product_gid"],
    )

    op.create_table(
        "batch_products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("processing_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_product_gid", sa.String(128), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("prompt_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("baseline_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("batch_id", "shopify_product_gid", name="uq_batch_product"),
    )
    op.create_index("ix_batch_products_batch_status", "batch_products", ["batch_id", "status"])

    op.create_table(
        "batch_images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "batch_product_id",
            sa.Uuid(),
            sa.ForeignKey("batch_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shopify_media_gid", sa.String(128), nullable=False),
        sa.Column("shopify_file_gid", sa.String(128), nullable=True),
        sa.Column("cdn_url", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("source_fingerprint", sa.String(128), nullable=True),
        sa.Column("delta_type", sa.String(32), nullable=False),
        sa.Column("current_prompt_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_storage_key", sa.String(1024), nullable=True),
        sa.Column("output_url", sa.Text(), nullable=True),
        sa.Column("output_mime_type", sa.String(128), nullable=True),
        sa.Column("output_checksum", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "batch_product_id",
            "shopify_media_gid",
            "delta_type",
            name="uq_batch_image_source_delta",
        ),
    )
    op.create_index("ix_batch_images_product_status", "batch_images", ["batch_product_id", "status"])

    op.create_table(
        "processing_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_image_id", sa.Uuid(), sa.ForeignKey("batch_images.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "batch_product_id",
            sa.Uuid(),
            sa.ForeignKey("batch_products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("shopify_source_url", sa.Text(), nullable=True),
        sa.Column("output_storage_key", sa.String(1024), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("batch_image_id", "attempt_number", name="uq_attempt_image_number"),
    )


def downgrade() -> None:
    op.drop_table("processing_attempts")
    op.drop_table("batch_images")
    op.drop_table("batch_products")
    op.drop_table("secondary_queue_items")
    op.drop_table("processing_batches")
    op.drop_table("webhook_events")
    op.drop_table("processing_baselines")
    op.drop_table("product_media")
    op.drop_table("shopify_files")
    op.drop_table("product_variants")
    op.drop_table("products")
    op.drop_table("sync_runs")
    op.drop_table("shop_settings")
    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("access_token", sa.Text(), nullable=True))
        batch.drop_column("uninstalled_at")
        batch.drop_column("installed_at")
        batch.drop_column("scopes")
        batch.drop_column("token_expires_at")
        batch.drop_column("encrypted_refresh_token")
        batch.drop_column("encrypted_access_token")

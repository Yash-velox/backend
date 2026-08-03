"""Normalized per-image Shopify CDN versions.

Revision ID: 006_image_versions
Revises: 005_media_versions_rollback
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_image_versions"
down_revision: Union[str, Sequence[str], None] = "005_media_versions_rollback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("batch_images", sa.Column("generated_shopify_file_gid", sa.String(128), nullable=True))
    op.add_column("batch_images", sa.Column("generated_shopify_cdn_url", sa.Text(), nullable=True))
    op.add_column("batch_images", sa.Column("generated_image_version_id", sa.Uuid(), nullable=True))

    op.create_table(
        "image_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_media_gid", sa.String(128), nullable=False),
        sa.Column(
            "parent_version_id",
            sa.Uuid(),
            sa.ForeignKey("image_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_type", sa.String(32), nullable=False),
        sa.Column("shopify_file_gid", sa.String(128), nullable=True),
        sa.Column("shopify_media_gid", sa.String(128), nullable=True),
        sa.Column("shopify_cdn_url", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.String(512), nullable=True),
        sa.Column("stored_filename", sa.String(512), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_original", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by_batch_id",
            sa.Uuid(),
            sa.ForeignKey("processing_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_batch_image_id",
            sa.Uuid(),
            sa.ForeignKey("batch_images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("processing_attempts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "product_media_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_media_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("upload_idempotency_key", sa.String(128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "shop_id",
            "product_id",
            "source_media_gid",
            "version_number",
            name="uq_image_versions_lineage_number",
        ),
    )
    op.create_index("ix_image_versions_shop_id", "image_versions", ["shop_id"])
    op.create_index("ix_image_versions_product_id", "image_versions", ["product_id"])
    op.create_index("ix_image_versions_shop_product", "image_versions", ["shop_id", "product_id"])
    op.create_index(
        "ix_image_versions_source_media",
        "image_versions",
        ["shop_id", "product_id", "source_media_gid"],
    )
    op.create_index("ix_image_versions_file_gid", "image_versions", ["shop_id", "shopify_file_gid"])
    op.create_index(
        "ix_image_versions_product_media_version_id",
        "image_versions",
        ["product_media_version_id"],
    )
    op.create_index(
        "ix_image_versions_current",
        "image_versions",
        ["shop_id", "product_id", "source_media_gid"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE"),
    )
    op.create_index(
        "ix_image_versions_shop_file_unique",
        "image_versions",
        ["shop_id", "shopify_file_gid"],
        unique=True,
        postgresql_where=sa.text("shopify_file_gid IS NOT NULL"),
    )
    op.create_index(
        "ix_image_versions_upload_idempotency",
        "image_versions",
        ["shop_id", "upload_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "image_version_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "image_version_id",
            sa.Uuid(),
            sa.ForeignKey("image_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("previous_version_id", sa.Uuid(), nullable=True),
        sa.Column("new_version_id", sa.Uuid(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("product_media_version_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(64), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_image_version_events_shop_id", "image_version_events", ["shop_id"])
    op.create_index("ix_image_version_events_product_id", "image_version_events", ["product_id"])
    op.create_index(
        "ix_image_version_events_version",
        "image_version_events",
        ["image_version_id", "created_at"],
    )
    op.create_index(
        "ix_image_version_events_shop_product",
        "image_version_events",
        ["shop_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_image_version_events_shop_product", table_name="image_version_events")
    op.drop_index("ix_image_version_events_version", table_name="image_version_events")
    op.drop_index("ix_image_version_events_product_id", table_name="image_version_events")
    op.drop_index("ix_image_version_events_shop_id", table_name="image_version_events")
    op.drop_table("image_version_events")

    op.drop_index("ix_image_versions_upload_idempotency", table_name="image_versions")
    op.drop_index("ix_image_versions_shop_file_unique", table_name="image_versions")
    op.drop_index("ix_image_versions_current", table_name="image_versions")
    op.drop_index("ix_image_versions_product_media_version_id", table_name="image_versions")
    op.drop_index("ix_image_versions_file_gid", table_name="image_versions")
    op.drop_index("ix_image_versions_source_media", table_name="image_versions")
    op.drop_index("ix_image_versions_shop_product", table_name="image_versions")
    op.drop_index("ix_image_versions_product_id", table_name="image_versions")
    op.drop_index("ix_image_versions_shop_id", table_name="image_versions")
    op.drop_table("image_versions")

    op.drop_column("batch_images", "generated_image_version_id")
    op.drop_column("batch_images", "generated_shopify_cdn_url")
    op.drop_column("batch_images", "generated_shopify_file_gid")

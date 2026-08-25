"""Denormalize has_images on products for picker filter.

Revision ID: 014_product_has_images
Revises: 013_manual_reprocess_flag
Create Date: 2026-08-25

Set at catalog sync (1/true = eligible media, 0/false = none).
Backfill from product_media using the same eligibility as manual batch create.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_product_has_images"
down_revision: Union[str, None] = "013_manual_reprocess_flag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("has_images", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_products_shop_has_images",
        "products",
        ["shop_id", "has_images"],
    )
    # Backfill: eligible = active + visible + non-empty cdn_url
    op.execute(
        """
        UPDATE products AS p
        SET has_images = EXISTS (
            SELECT 1
            FROM product_media AS m
            WHERE m.product_id = p.id
              AND m.is_active IS TRUE
              AND m.is_visible IS TRUE
              AND m.cdn_url IS NOT NULL
              AND m.cdn_url <> ''
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_products_shop_has_images", table_name="products")
    op.drop_column("products", "has_images")

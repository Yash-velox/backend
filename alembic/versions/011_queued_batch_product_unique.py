"""Partial unique index: one QUEUED BatchProduct per shop+product.

Revision ID: 011_queued_batch_product_unique
Revises: 010_rollback_force_conflict
Create Date: 2026-08-12

Allows one PROCESSING generation and one QUEUED generation for the same product,
but prevents multiple concurrent QUEUED generations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_queued_batch_product_unique"
down_revision: Union[str, None] = "010_rollback_force_conflict"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_batch_products_shop_product_status",
        "batch_products",
        ["shop_id", "shopify_product_gid", "status"],
        unique=False,
    )
    op.create_index(
        "uq_batch_product_queued_shop_product",
        "batch_products",
        ["shop_id", "shopify_product_gid"],
        unique=True,
        postgresql_where=sa.text("status = 'QUEUED'"),
        sqlite_where=sa.text("status = 'QUEUED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_batch_product_queued_shop_product", table_name="batch_products")
    op.drop_index("ix_batch_products_shop_product_status", table_name="batch_products")

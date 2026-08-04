"""Drop max_products_per_batch — automatic batches are time-triggered only.

Revision ID: 005_drop_max_products_per_batch
Revises: 004_shopify_product_publishing
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_drop_max_products_per_batch"
down_revision: Union[str, None] = "004_shopify_product_publishing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("shop_settings", "max_products_per_batch")


def downgrade() -> None:
    op.add_column(
        "shop_settings",
        sa.Column(
            "max_products_per_batch",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
    )
    op.alter_column("shop_settings", "max_products_per_batch", server_default=None)

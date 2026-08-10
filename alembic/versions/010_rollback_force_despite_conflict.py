"""Add force_despite_conflict on product rollback operations.

Revision ID: 010_rollback_force_despite_conflict
Revises: 009_drop_max_products_per_batch
Create Date: 2026-08-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_rollback_force_despite_conflict"
down_revision: Union[str, None] = "009_drop_max_products_per_batch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_rollback_operations",
        sa.Column(
            "force_despite_conflict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("product_rollback_operations", "force_despite_conflict")

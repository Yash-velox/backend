"""Add one-time prompt override columns for reprocess.

Revision ID: 007_prompt_override_reprocess
Revises: 006_image_versions
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_prompt_override_reprocess"
down_revision: Union[str, Sequence[str], None] = "006_image_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "batch_products",
        sa.Column("prompt_override_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "batch_images",
        sa.Column("prompt_override_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("batch_images", "prompt_override_json")
    op.drop_column("batch_products", "prompt_override_json")

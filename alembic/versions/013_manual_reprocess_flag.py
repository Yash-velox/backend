"""Mark merchant reprocess separately from automatic retry.

Revision ID: 013_manual_reprocess_flag
Revises: 012_webhook_async_intake
Create Date: 2026-08-19

Retry may reuse an existing unpublished GENERATED Shopify file.
Merchant Reprocess must ignore that file and run AI again.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_manual_reprocess_flag"
down_revision: Union[str, None] = "012_webhook_async_intake"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "batch_images",
        sa.Column("manual_reprocess", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("batch_images", "manual_reprocess")

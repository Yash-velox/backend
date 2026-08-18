"""Async webhook intake: store payload and claim metadata.

Revision ID: 012_webhook_async_intake
Revises: 011_queued_batch_product_unique
Create Date: 2026-08-18

HTTP products/update only inserts/dedupes webhook_events. A background worker
claims QUEUED rows (with a concurrency cap) and does Shopify GraphQL + catalog
+ Secondary Queue work.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_webhook_async_intake"
down_revision: Union[str, None] = "011_queued_batch_product_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("payload_json", sa.JSON(), nullable=True))
    op.add_column("webhook_events", sa.Column("claimed_by", sa.String(length=64), nullable=True))
    op.add_column("webhook_events", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "webhook_events",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_webhook_events_result_received",
        "webhook_events",
        ["processing_result", "received_at"],
        unique=False,
    )
    op.create_index(
        "ix_webhook_events_shop_product_result",
        "webhook_events",
        ["shop_id", "shopify_product_gid", "processing_result"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_shop_product_result", table_name="webhook_events")
    op.drop_index("ix_webhook_events_result_received", table_name="webhook_events")
    op.drop_column("webhook_events", "attempt_count")
    op.drop_column("webhook_events", "claimed_at")
    op.drop_column("webhook_events", "claimed_by")
    op.drop_column("webhook_events", "payload_json")

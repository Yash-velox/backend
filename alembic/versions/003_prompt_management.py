"""Prompt management tables for product-type sequential prompts.

Revision ID: 003_prompt_management
Revises: 002_week2_domain
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_prompt_management"
down_revision: Union[str, Sequence[str], None] = "002_week2_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_product_types",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "normalized_name", name="uq_prompt_product_types_shop_normalized"),
    )
    op.create_index("ix_prompt_product_types_shop_id", "prompt_product_types", ["shop_id"])
    op.create_index(
        "ix_prompt_product_types_shop_source",
        "prompt_product_types",
        ["shop_id", "source"],
    )

    op.create_table(
        "prompt_configurations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shop_id", sa.Uuid(), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "prompt_product_type_id",
            sa.Uuid(),
            sa.ForeignKey("prompt_product_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "prompt_product_type_id", name="uq_prompt_configurations_shop_type"),
    )
    op.create_index("ix_prompt_configurations_shop_id", "prompt_configurations", ["shop_id"])
    op.create_index(
        "ix_prompt_configurations_prompt_product_type_id",
        "prompt_configurations",
        ["prompt_product_type_id"],
    )

    op.create_table(
        "prompt_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "prompt_configuration_id",
            sa.Uuid(),
            sa.ForeignKey("prompt_configurations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("prompt_configuration_id", "step_order", name="uq_prompt_steps_config_order"),
    )
    op.create_index("ix_prompt_steps_prompt_configuration_id", "prompt_steps", ["prompt_configuration_id"])
    op.create_index(
        "ix_prompt_steps_config_order",
        "prompt_steps",
        ["prompt_configuration_id", "step_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_steps_config_order", table_name="prompt_steps")
    op.drop_index("ix_prompt_steps_prompt_configuration_id", table_name="prompt_steps")
    op.drop_table("prompt_steps")

    op.drop_index("ix_prompt_configurations_prompt_product_type_id", table_name="prompt_configurations")
    op.drop_index("ix_prompt_configurations_shop_id", table_name="prompt_configurations")
    op.drop_table("prompt_configurations")

    op.drop_index("ix_prompt_product_types_shop_source", table_name="prompt_product_types")
    op.drop_index("ix_prompt_product_types_shop_id", table_name="prompt_product_types")
    op.drop_table("prompt_product_types")

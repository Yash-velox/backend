from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.enums import PromptProductTypeSource


class PromptProductType(Base):
    __tablename__ = "prompt_product_types"
    __table_args__ = (
        UniqueConstraint("shop_id", "normalized_name", name="uq_prompt_product_types_shop_normalized"),
        Index("ix_prompt_product_types_shop_source", "shop_id", "source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[PromptProductTypeSource] = mapped_column(
        Enum(PromptProductTypeSource, name="prompt_product_type_source", native_enum=False),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    configuration: Mapped[PromptConfiguration | None] = relationship(
        back_populates="product_type",
        uselist=False,
        cascade="all, delete-orphan",
    )


class PromptConfiguration(Base):
    __tablename__ = "prompt_configurations"
    __table_args__ = (
        UniqueConstraint("shop_id", "prompt_product_type_id", name="uq_prompt_configurations_shop_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prompt_product_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prompt_product_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    product_type: Mapped[PromptProductType] = relationship(back_populates="configuration")
    steps: Mapped[list[PromptStep]] = relationship(
        back_populates="configuration",
        cascade="all, delete-orphan",
        order_by="PromptStep.step_order",
    )


class PromptStep(Base):
    __tablename__ = "prompt_steps"
    __table_args__ = (
        UniqueConstraint("prompt_configuration_id", "step_order", name="uq_prompt_steps_config_order"),
        Index("ix_prompt_steps_config_order", "prompt_configuration_id", "step_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_configuration_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prompt_configurations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    configuration: Mapped[PromptConfiguration] = relationship(back_populates="steps")

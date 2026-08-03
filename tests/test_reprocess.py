"""Manual reprocess with one-time prompt overrides."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ProcessingBatch,
    Product,
    TriggerType,
)
from app.services.prompt_configuration import PromptConfigurationService
from app.services.prompt_product_types import PromptProductTypeService
from app.services.prompt_resolver import PromptResolver
from app.services.reprocess_service import ReprocessError, ReprocessService
from app.services.state_machine import assert_transition, BATCH_PRODUCT_TRANSITIONS, BATCH_IMAGE_TRANSITIONS
from app.models import BatchProductStatus as BPS, BatchImageStatus as BIS


def _product(db_session, shop, *, title="Ring", product_type="Rings") -> Product:
    row = Product(
        shop_id=shop.id,
        shopify_product_gid=f"gid://shopify/Product/{uuid4().hex[:8]}",
        title=title,
        product_type=product_type,
        vendor="Aone",
        handle=title.lower().replace(" ", "-"),
        is_deleted=False,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _configure_rings(db_session, shop) -> None:
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.commit()
    ppt = types.find_by_normalized_name("rings")
    assert ppt is not None
    cfg = PromptConfigurationService(db_session, shop)
    cfg.add_step(ppt.id, name="Bg", prompt_text="Remove background for {{product_title}}", is_enabled=True)


def _batch_with_product(db_session, shop, product: Product, *, status=BatchProductStatus.COMPLETED):
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=1,
        completed_product_count=1,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        product_snapshot_json={"product_type": product.product_type, "title": product.title},
        status=status,
        image_count=1,
        completed_at=datetime.now(timezone.utc) if status == BatchProductStatus.COMPLETED else None,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid=f"gid://shopify/MediaImage/{uuid4().hex[:8]}",
        cdn_url="https://cdn.shopify.com/s/files/1/test.png",
        original_filename="ring.png",
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.COMPLETED if status == BatchProductStatus.COMPLETED else BatchImageStatus.FAILED,
        attempt_count=1,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(image)
    db_session.commit()
    db_session.refresh(batch)
    db_session.refresh(bp)
    db_session.refresh(image)
    return batch, bp, image


def test_completed_can_transition_to_queued():
    assert_transition("batch_product", BATCH_PRODUCT_TRANSITIONS, BPS.COMPLETED, BPS.QUEUED)
    assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, BIS.COMPLETED, BIS.QUEUED)


def test_preview_and_reprocess_product_with_override(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    batch, bp, image = _batch_with_product(db_session, shop, product)

    svc = ReprocessService(db_session, shop)
    preview = svc.preview_for_product(bp.id)
    assert preview["scope"] == "product"
    assert len(preview["steps"]) == 1
    assert "Ring" in preview["steps"][0]["renderedPrompt"]
    assert preview["oneTimeOverride"] is True

    result = svc.reprocess_product(
        bp.id,
        steps=[{"name": "Custom", "promptTemplate": "Only remove background for {{product_title}}"}],
    )
    db_session.refresh(bp)
    db_session.refresh(image)
    assert result["usedPromptOverride"] is True
    assert bp.status == BatchProductStatus.QUEUED
    assert image.status == BatchImageStatus.QUEUED
    assert bp.prompt_override_json is not None
    assert bp.prompt_override_json[0]["promptTemplate"].startswith("Only remove")
    assert image.output_storage_key is None

    db_session.refresh(batch)
    assert batch.status == BatchStatus.PROCESSING


def test_reprocess_image_sets_image_override(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    _batch, bp, image = _batch_with_product(db_session, shop, product)

    svc = ReprocessService(db_session, shop)
    svc.reprocess_image(
        image.id,
        steps=[{"name": "Img", "promptTemplate": "Cutout {{image_filename}}"}],
    )
    db_session.refresh(bp)
    db_session.refresh(image)
    assert image.status == BatchImageStatus.QUEUED
    assert bp.status == BatchProductStatus.QUEUED
    assert image.prompt_override_json[0]["promptTemplate"] == "Cutout {{image_filename}}"
    assert bp.prompt_override_json is None


def test_override_render_at_process_time(db_session, shop):
    product = _product(db_session, shop, title="Halo")
    _configure_rings(db_session, shop)
    resolved = PromptResolver(db_session, shop).resolve_from_override(
        [{"name": "X", "promptTemplate": "Do {{product_title}} at {{image_position}}"}],
        product=product,
        product_type_display="Rings",
        image_position=3,
    )
    assert resolved[0].rendered_prompt == "Do Halo at 3"


def test_reprocess_rejects_processing_product(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    _batch, bp, _image = _batch_with_product(db_session, shop, product, status=BatchProductStatus.COMPLETED)
    bp.status = BatchProductStatus.PROCESSING
    db_session.commit()

    with pytest.raises(ReprocessError) as exc:
        ReprocessService(db_session, shop).reprocess_product(bp.id)
    assert exc.value.code == "REPROCESS_NOT_ELIGIBLE"

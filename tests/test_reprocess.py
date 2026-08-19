"""Manual reprocess with one-time prompt overrides."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ImageVersion,
    ImageVersionType,
    MediaVersionType,
    ProcessingBatch,
    Product,
    ProductMedia,
    ProductMediaVersion,
    PublishStatus,
    TriggerType,
)
from app.services.prompt_configuration import PromptConfigurationService
from app.services.prompt_product_types import PromptProductTypeService
from app.services.prompt_resolver import PromptResolver
from app.services.reprocess_service import ReprocessError, ReprocessService
from app.services.retry_service import find_generated_version_for_batch_image
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
    assert image.manual_reprocess is True

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


def _add_live_media(db_session, shop, product: Product, *, count: int = 2) -> list[ProductMedia]:
    rows: list[ProductMedia] = []
    for i in range(count):
        row = ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid=f"gid://shopify/MediaImage/{uuid4().hex[:8]}",
            shopify_file_gid=f"gid://shopify/MediaImage/{uuid4().hex[:8]}",
            cdn_url=f"https://cdn.shopify.com/live-{i}.png",
            original_filename=f"live-{i}.png",
            is_visible=True,
            is_active=True,
            is_primary=i == 0,
            position=i,
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    for row in rows:
        db_session.refresh(row)
    return rows


def test_preview_and_reprocess_live_selected_images(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=3)

    svc = ReprocessService(db_session, shop)
    preview = svc.preview_live(product.id)
    assert preview["scope"] == "live"
    assert preview["autoPublish"] is True
    assert len(preview["images"]) == 3
    assert len(preview["steps"]) == 1

    selected = [media[0].shopify_media_gid, media[2].shopify_media_gid]
    result = svc.reprocess_live(
        product.id,
        media_gids=selected,
        steps=[{"name": "Live", "promptTemplate": "Fix {{product_title}} only"}],
    )
    assert result["scope"] == "live"
    assert result["autoPublish"] is True
    assert result["imageCount"] == 2
    assert result["usedPromptOverride"] is True

    batch = db_session.query(ProcessingBatch).filter_by(id=UUID(result["batchId"])).one()
    assert batch.settings_snapshot_json["live_reprocess"] is True
    assert batch.settings_snapshot_json["auto_publish"] is True
    bp = db_session.query(BatchProduct).filter_by(batch_id=batch.id).one()
    assert bp.prompt_override_json[0]["promptTemplate"].startswith("Fix")
    images = db_session.query(BatchImage).filter_by(batch_product_id=bp.id).all()
    assert len(images) == 2
    assert {i.shopify_media_gid for i in images} == set(selected)
    baseline_media = (bp.baseline_snapshot_json or {}).get("media") or []
    assert len(baseline_media) == 3


def test_live_reprocess_rejects_inflight_product(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=1)
    _batch_with_product(db_session, shop, product, status=BatchProductStatus.PROCESSING)

    with pytest.raises(ReprocessError) as exc:
        ReprocessService(db_session, shop).reprocess_live(
            product.id, media_gids=[media[0].shopify_media_gid]
        )
    assert exc.value.code == "REPROCESS_NOT_ELIGIBLE"


def test_live_reprocess_rejects_unknown_media(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    _add_live_media(db_session, shop, product, count=1)

    with pytest.raises(ReprocessError) as exc:
        ReprocessService(db_session, shop).reprocess_live(
            product.id, media_gids=["gid://shopify/MediaImage/missing"]
        )
    assert exc.value.code == "REPROCESS_NOT_ELIGIBLE"


def test_live_reprocess_skips_unknown_media_and_continues(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=2)

    result = ReprocessService(db_session, shop).reprocess_live(
        product.id,
        media_gids=[
            media[0].shopify_media_gid,
            "gid://shopify/MediaImage/missing",
        ],
    )
    assert result["imageCount"] == 1
    assert result["warnings"]
    assert "skipped" in result["warnings"][0].lower()


def test_live_reprocess_preview_and_apply_api(client, db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=2)

    preview = client.get(f"/api/products/{product.id}/live-reprocess/preview")
    assert preview.status_code == 200
    body = preview.json()["data"]
    assert body["scope"] == "live"
    assert len(body["images"]) == 2
    assert body["steps"]

    versions = client.get(f"/api/products/{product.id}/media-versions")
    assert versions.status_code == 200
    assert len(versions.json()["data"]["liveMedia"]) == 2

    applied = client.post(
        f"/api/products/{product.id}/live-reprocess",
        json={
            "mediaGids": [media[0].shopify_media_gid],
            "steps": [{"name": "Live", "promptTemplate": "Clean {{product_title}}"}],
        },
    )
    assert applied.status_code == 202
    data = applied.json()["data"]
    assert data["imageCount"] == 1
    assert data["autoPublish"] is True


def _generated_version(db_session, shop, product: Product, batch, image: BatchImage, *, file_gid: str) -> ImageVersion:
    version = ImageVersion(
        shop_id=shop.id,
        product_id=product.id,
        source_media_gid=image.shopify_media_gid,
        version_number=1,
        version_type=ImageVersionType.GENERATED,
        shopify_file_gid=file_gid,
        shopify_cdn_url="https://cdn.shopify.com/generated-v1.png",
        is_current=True,
        created_by_batch_id=batch.id,
        created_by_batch_image_id=image.id,
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    return version


def test_reprocess_does_not_reuse_existing_generated_version(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    batch, bp, image = _batch_with_product(db_session, shop, product)
    bp.publish_status = PublishStatus.READY_TO_PUBLISH
    db_session.commit()
    version = _generated_version(
        db_session, shop, product, batch, image, file_gid="gid://shopify/MediaImage/v1"
    )

    assert find_generated_version_for_batch_image(db_session, image) is not None

    ReprocessService(db_session, shop).reprocess_product(
        bp.id,
        steps=[{"name": "Custom", "promptTemplate": "Make {{product_title}} brighter"}],
    )
    db_session.refresh(image)
    db_session.refresh(bp)

    assert image.manual_reprocess is True
    assert image.status == BatchImageStatus.QUEUED
    assert image.generated_shopify_file_gid is None
    assert find_generated_version_for_batch_image(db_session, image) is None
    assert bp.publish_status is None
    still = (
        db_session.query(ImageVersion)
        .filter(ImageVersion.created_by_batch_image_id == image.id)
        .count()
    )
    assert still == 1
    db_session.refresh(version)
    assert version.shopify_file_gid == "gid://shopify/MediaImage/v1"


def test_reprocess_unpublished_does_not_create_published_snapshot(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    _batch, bp, _image = _batch_with_product(db_session, shop, product)
    bp.publish_status = PublishStatus.READY_TO_PUBLISH
    db_session.commit()

    ReprocessService(db_session, shop).reprocess_product(bp.id)
    count = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == product.id)
        .count()
    )
    assert count == 0


def test_reprocess_published_preserves_existing_history(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=1)
    batch, bp, image = _batch_with_product(db_session, shop, product)
    bp.publish_status = PublishStatus.PUBLISHED
    db_session.commit()
    existing = ProductMediaVersion(
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        version_number=1,
        version_type=MediaVersionType.PUBLISHED,
        is_active=True,
        rollback_eligible=True,
        snapshot_hash="published-existing",
        items_json={
            "product_gid": product.shopify_product_gid,
            "media": [
                {
                    "media_gid": media[0].shopify_media_gid,
                    "file_gid": media[0].shopify_file_gid,
                    "position": 0,
                }
            ],
        },
        created_by="publish",
    )
    db_session.add(existing)
    db_session.commit()

    ReprocessService(db_session, shop).reprocess_product(bp.id)
    rows = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == product.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id == existing.id
    assert rows[0].version_type == MediaVersionType.PUBLISHED
    db_session.refresh(image)
    assert image.manual_reprocess is True
    db_session.refresh(bp)
    assert bp.publish_status is None


def test_reprocess_published_creates_history_when_missing(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    media = _add_live_media(db_session, shop, product, count=1)
    _batch, bp, image = _batch_with_product(db_session, shop, product)
    bp.publish_status = PublishStatus.PUBLISHED
    db_session.commit()

    ReprocessService(db_session, shop).reprocess_product(bp.id)
    rows = (
        db_session.query(ProductMediaVersion)
        .filter(
            ProductMediaVersion.product_id == product.id,
            ProductMediaVersion.version_type == MediaVersionType.PUBLISHED,
        )
        .all()
    )
    assert len(rows) == 1
    items = rows[0].items_json or {}
    assert any(
        (m.get("media_gid") == media[0].shopify_media_gid)
        for m in (items.get("media") or [])
    )
    db_session.refresh(image)
    assert image.manual_reprocess is True


def test_retry_still_reuses_generated_version_without_reprocess_flag(db_session, shop):
    product = _product(db_session, shop)
    _configure_rings(db_session, shop)
    batch, _bp, image = _batch_with_product(db_session, shop, product)
    _generated_version(db_session, shop, product, batch, image, file_gid="gid://shopify/MediaImage/retry-v1")
    image.status = BatchImageStatus.QUEUED
    image.generated_shopify_file_gid = None
    image.manual_reprocess = False
    db_session.commit()
    db_session.refresh(image)

    found = find_generated_version_for_batch_image(db_session, image)
    assert found is not None
    assert found.shopify_file_gid == "gid://shopify/MediaImage/retry-v1"


def test_reprocess_batch_handles_published_and_unpublished_products(db_session, shop):
    published = _product(db_session, shop, title="Published Ring")
    unpublished = _product(db_session, shop, title="Draft Ring", product_type="Rings")
    _configure_rings(db_session, shop)
    batch, bp_pub, img_pub = _batch_with_product(db_session, shop, published)
    bp_unpub = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=unpublished.shopify_product_gid,
        product_id=unpublished.id,
        product_snapshot_json={"product_type": unpublished.product_type, "title": unpublished.title},
        status=BatchProductStatus.COMPLETED,
        image_count=1,
        publish_status=PublishStatus.READY_TO_PUBLISH,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(bp_unpub)
    db_session.flush()
    img_unpub = BatchImage(
        batch_product_id=bp_unpub.id,
        shop_id=shop.id,
        shopify_media_gid=f"gid://shopify/MediaImage/{uuid4().hex[:8]}",
        cdn_url="https://cdn.shopify.com/s/files/1/draft.png",
        original_filename="draft.png",
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.COMPLETED,
        attempt_count=1,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(img_unpub)
    bp_pub.publish_status = PublishStatus.PUBLISHED
    _add_live_media(db_session, shop, published, count=1)
    db_session.commit()

    result = ReprocessService(db_session, shop).reprocess_batch(batch.id)
    assert result["retriedCount"] == 2
    db_session.refresh(img_pub)
    db_session.refresh(img_unpub)
    assert img_pub.manual_reprocess is True
    assert img_unpub.manual_reprocess is True
    published_history = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == published.id)
        .count()
    )
    unpublished_history = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == unpublished.id)
        .count()
    )
    assert published_history == 1
    assert unpublished_history == 0


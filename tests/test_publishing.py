"""Minimal Shopify publishing tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from app.core.shop_resolver import ensure_shop_settings
from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ProcessingBaseline,
    ProcessingBatch,
    Product,
    ProductPublishOperation,
    PublishStatus,
    PublishTriggerSource,
    Shop,
    ShopSettings,
    TriggerType,
)
from app.services.output_storage import LocalFilesystemOutputStorage
from app.services.product_publisher import ProductPublisher
from app.services.publish_conflict import compare_publish_snapshots, heal_empty_publish_baseline
from app.services.publish_snapshot import normalize_publish_snapshot, snapshot_hash
from app.services.publish_trigger import (
    PublishEnqueueError,
    PublishTriggerService,
    is_batch_processing_terminal,
)
from app.services.shopify_file_upload import sanitize_png_filename, validate_png_file

# Minimal 1x1 PNG
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _media(gid: str, position: int, *, alt: str = "", featured: bool = False) -> dict:
    return {
        "media_gid": gid,
        "file_gid": gid,
        "position": position,
        "alt_text": alt,
        "is_primary": featured or position == 0,
        "cdn_url": f"https://cdn.shopify.com/{gid}.png",
        "filename": f"{gid}.png",
    }


def _snapshot(media: list[dict], *, variants: list[dict] | None = None) -> dict:
    featured = next((m["media_gid"] for m in media if m.get("is_primary")), media[0]["media_gid"] if media else None)
    return {
        "product_gid": "gid://shopify/Product/1",
        "updated_at": "2026-07-31T00:00:00Z",
        "featured_media_gid": featured,
        "media": media,
        "variants": variants or [],
    }


def _seed_completed_batch(db, shop, tmp_path, *, product_status=BatchProductStatus.COMPLETED, with_png=True):
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=1,
        completed_product_count=1 if product_status == BatchProductStatus.COMPLETED else 0,
        failed_product_count=1 if product_status == BatchProductStatus.FAILED else 0,
    )
    db.add(batch)
    db.flush()
    media_gid = "gid://shopify/MediaImage/10"
    product = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/1",
        status=product_status,
        image_count=1,
        baseline_snapshot_json={
            "product_gid": "gid://shopify/Product/1",
            "media": [_media(media_gid, 0, alt="front", featured=True)],
            "variants": [{"variant_gid": "gid://shopify/ProductVariant/1", "media_gid": media_gid}],
        },
    )
    db.add(product)
    db.flush()

    storage = LocalFilesystemOutputStorage(tmp_path / "processed")
    key = f"{shop.id}/{product.id}/out.png"
    if with_png:
        storage.save_bytes(key=key, data=PNG_BYTES, content_type="image/png")

    image = BatchImage(
        batch_product_id=product.id,
        shop_id=shop.id,
        shopify_media_gid=media_gid,
        shopify_file_gid=media_gid,
        cdn_url="https://cdn.shopify.com/a.png",
        original_filename="ring-front.jpg",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.COMPLETED if with_png else BatchImageStatus.FAILED,
        output_storage_key=key if with_png else None,
        output_mime_type="image/png" if with_png else None,
        # CDN path (Option C): completed images are durable in Shopify Files.
        generated_shopify_file_gid=f"gid://shopify/File/gen-{media_gid.split('/')[-1]}" if with_png else None,
        generated_shopify_cdn_url="https://cdn.shopify.com/generated-a.png" if with_png else None,
    )
    db.add(image)
    db.commit()
    db.refresh(batch)
    db.refresh(product)
    return batch, product, image


def test_auto_publish_setting_defaults_false(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    res = client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["autoPublishProcessedImages"] is False


def test_auto_publish_setting_can_enable(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    res = client.put(
        "/api/settings",
        json={"autoPublishProcessedImages": True},
    )
    assert res.status_code == 200
    assert res.json()["data"]["autoPublishProcessedImages"] is True
    row = db_session.query(ShopSettings).filter(ShopSettings.shop_id == shop.id).one()
    assert row.auto_publish_processed_images is True


def test_completed_product_can_publish_while_batch_still_processing(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    batch.status = BatchStatus.PROCESSING
    batch.pending_product_count = 1
    batch.completed_product_count = 1
    db_session.commit()
    assert not is_batch_processing_terminal(batch)
    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    assert result["status"] == PublishStatus.QUEUED.value
    db_session.refresh(product)
    assert product.publish_status == PublishStatus.QUEUED


def test_publish_all_still_requires_batch_terminal(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    batch.status = BatchStatus.PROCESSING
    batch.pending_product_count = 1
    db_session.commit()
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_ready_for_batch(batch.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code == "PUBLISH_BATCH_NOT_TERMINAL"


def test_failed_processing_excluded(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(
        db_session, shop, tmp_path, product_status=BatchProductStatus.FAILED, with_png=False
    )
    assert is_batch_processing_terminal(batch)
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code == "PUBLISH_PRODUCT_NOT_PROCESSED"


def test_missing_output_blocks_publish(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    image.output_storage_key = None
    image.generated_shopify_file_gid = None
    image.generated_shopify_cdn_url = None
    db_session.commit()
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code in {"PUBLISH_OUTPUT_MISSING", "PUBLISH_OUTPUT_INCOMPLETE"}


def test_invalid_png_blocks_publish_when_no_cdn(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    # Force legacy local-only path (no Shopify CDN file yet).
    image.generated_shopify_file_gid = None
    image.generated_shopify_cdn_url = None
    storage = LocalFilesystemOutputStorage(tmp_path / "processed")
    path = storage.resolve_path(image.output_storage_key)
    path.write_bytes(b"not-a-png")
    db_session.commit()
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code == "PUBLISH_OUTPUT_NOT_PNG"


def test_cdn_ready_allows_publish_without_local_file(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    image.output_storage_key = None  # temp deleted after CDN upload
    db_session.commit()
    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    assert result["status"] == PublishStatus.QUEUED.value
    op = db_session.query(ProductPublishOperation).filter_by(batch_product_id=product.id).one()
    assert op.assets_json[0]["shopify_file_gid"] == image.generated_shopify_file_gid
    assert op.assets_json[0]["upload_status"] == "READY"


def test_manual_enqueue_idempotent(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    svc = PublishTriggerService(db_session, shop)
    first = svc.enqueue_product(product.id, trigger=PublishTriggerSource.MANUAL)
    second = svc.enqueue_product(product.id, trigger=PublishTriggerSource.MANUAL)
    assert first["operationId"] == second["operationId"]
    assert first["status"] == PublishStatus.QUEUED.value
    ops = db_session.query(ProductPublishOperation).filter_by(batch_product_id=product.id).all()
    assert len(ops) == 1


def test_terminal_marks_ready_without_auto_enqueue(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    ensure_shop_settings(db_session, shop)
    settings = db_session.query(ShopSettings).filter_by(shop_id=shop.id).one()
    settings.auto_publish_processed_images = False
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    result = PublishTriggerService(db_session, shop).on_batch_terminal(batch)
    db_session.refresh(product)
    assert product.publish_status == PublishStatus.READY_TO_PUBLISH
    assert result["queued"] == 0
    assert db_session.query(ProductPublishOperation).count() == 0


def test_auto_publish_enqueues_on_terminal(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    ensure_shop_settings(db_session, shop)
    settings = db_session.query(ShopSettings).filter_by(shop_id=shop.id).one()
    settings.auto_publish_processed_images = True
    db_session.commit()
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    result = PublishTriggerService(db_session, shop).on_batch_terminal(batch)
    db_session.refresh(product)
    assert result["queued"] >= 1
    assert product.publish_status == PublishStatus.QUEUED
    assert db_session.query(ProductPublishOperation).count() == 1


def test_auto_publish_enqueues_completed_product_while_batch_processing(
    db_session, shop, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    ensure_shop_settings(db_session, shop)
    settings = db_session.query(ShopSettings).filter_by(shop_id=shop.id).one()
    settings.auto_publish_processed_images = True
    db_session.commit()
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    batch.status = BatchStatus.PROCESSING
    batch.pending_product_count = 1
    batch.processing_product_count = 0
    batch.completed_product_count = 1
    db_session.commit()

    result = PublishTriggerService(db_session, shop).maybe_auto_publish_completed_products(batch)
    db_session.refresh(product)
    assert result["queued"] >= 1
    assert product.publish_status == PublishStatus.QUEUED
    assert db_session.query(ProductPublishOperation).count() == 1


def test_conflict_detects_added_removed_reorder_alt(db_session):
    baseline = _snapshot([_media("gid://shopify/MediaImage/1", 0, alt="a"), _media("gid://shopify/MediaImage/2", 1, alt="b")])
    added = _snapshot(
        [
            _media("gid://shopify/MediaImage/1", 0, alt="a"),
            _media("gid://shopify/MediaImage/2", 1, alt="b"),
            _media("gid://shopify/MediaImage/3", 2, alt="c"),
        ]
    )
    diff = compare_publish_snapshots(baseline, added)
    assert diff["hasConflict"] and diff["membershipChanged"]
    assert "gid://shopify/MediaImage/3" in diff["addedMediaIds"]

    removed = _snapshot([_media("gid://shopify/MediaImage/1", 0, alt="a")])
    diff2 = compare_publish_snapshots(baseline, removed)
    assert "gid://shopify/MediaImage/2" in diff2["removedMediaIds"]

    reordered = _snapshot([_media("gid://shopify/MediaImage/2", 0, alt="b"), _media("gid://shopify/MediaImage/1", 1, alt="a")])
    diff3 = compare_publish_snapshots(baseline, reordered)
    assert diff3["orderChanged"]

    alt = _snapshot([_media("gid://shopify/MediaImage/1", 0, alt="changed"), _media("gid://shopify/MediaImage/2", 1, alt="b")])
    diff4 = compare_publish_snapshots(baseline, alt)
    assert diff4["altChanges"]


def test_heal_empty_baseline_when_sources_match_live():
    empty = {"product_gid": "gid://shopify/Product/1", "media": [], "variants": []}
    live = _snapshot([_media("gid://shopify/MediaImage/10", 0, alt="front", featured=True)])
    assets = [{"source_media_gid": "gid://shopify/MediaImage/10"}]
    healed, did = heal_empty_publish_baseline(empty, live, assets)
    assert did is True
    assert len(healed["media"]) == 1
    assert compare_publish_snapshots(healed, live)["hasConflict"] is False


def test_heal_empty_baseline_skips_when_live_differs():
    empty = {"product_gid": "gid://shopify/Product/1", "media": [], "variants": []}
    live = _snapshot([_media("gid://shopify/MediaImage/99", 0, alt="other", featured=True)])
    assets = [{"source_media_gid": "gid://shopify/MediaImage/10"}]
    healed, did = heal_empty_publish_baseline(empty, live, assets)
    assert did is False
    assert healed.get("media") == []


def test_publisher_heals_empty_baseline_false_conflict(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    monkeypatch.setattr("app.config.settings.shopify_file_status_poll_seconds", 0.01)
    monkeypatch.setattr("app.config.settings.shopify_file_ready_timeout_seconds", 1)
    monkeypatch.setattr("app.config.settings.shopify_reorder_timeout_seconds", 1)

    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    # Legacy bug: empty ProcessingBaseline copied into publish baseline.
    product.baseline_snapshot_json = {"product_gid": "gid://shopify/Product/1", "media": []}
    db_session.commit()

    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    op_id = UUID(result["operationId"])
    op = db_session.query(ProductPublishOperation).filter_by(id=op_id).one()
    op.baseline_snapshot_json = {"product_gid": "gid://shopify/Product/1", "media": []}
    db_session.commit()

    generated_gid = image.generated_shopify_file_gid
    original_media = {
        "id": "gid://shopify/MediaImage/10",
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
    }
    new_media = {
        "id": generated_gid,
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/generated-a.png", "width": 10, "height": 10},
    }
    snap_original = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {"nodes": [original_media]},
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/10"}]},
                }
            ]
        },
    }
    snap_both = {
        **snap_original,
        "media": {"nodes": [original_media, new_media]},
        "featuredMedia": {"id": generated_gid},
    }
    snap_final = {
        **snap_original,
        "media": {"nodes": [new_media]},
        "featuredMedia": {"id": generated_gid},
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": generated_gid}]},
                }
            ]
        },
    }

    client = MagicMock()
    client.get_product_media_snapshot.side_effect = [
        snap_original,
        snap_original,
        snap_both,
        snap_both,
        snap_final,
    ]
    client.add_file_product_references.return_value = [{"id": generated_gid}]
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_file_statuses.return_value = [
        {
            "id": generated_gid,
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/generated-a.png", "width": 10, "height": 10},
        }
    ]

    publisher = ProductPublisher(db_session, shop, client=client)
    with patch.object(publisher.uploader, "upload_png") as upload_mock:
        out = publisher.run(op_id)
        upload_mock.assert_not_called()

    assert out.status == PublishStatus.PUBLISHED, (out.last_error_code, out.last_error_message)
    assert out.baseline_snapshot_json and (out.baseline_snapshot_json.get("media") or [])


def test_snapshot_hash_stable():
    snap = _snapshot([_media("gid://shopify/MediaImage/1", 0)])
    assert snapshot_hash(snap) == snapshot_hash(dict(snap))


def test_sanitize_png_filename():
    assert sanitize_png_filename("ring-front.jpg") == "ring-front.png"
    assert sanitize_png_filename("weird name!!.JPG") == "weird_name.png"


def test_validate_png_file(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(PNG_BYTES)
    size, data = validate_png_file(path)
    assert size == len(PNG_BYTES)
    assert data.startswith(b"\x89PNG")


def test_publish_api_enqueue(client, shop, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    res = client.post(f"/api/batches/products/{product.id}/publish")
    assert res.status_code == 202
    body = res.json()
    assert body["success"] is True
    assert body["data"]["status"] == "QUEUED"

    # Idempotent second click
    res2 = client.post(f"/api/batches/products/{product.id}/publish")
    assert res2.status_code == 202
    assert res2.json()["data"]["operationId"] == body["data"]["operationId"]


def test_publish_ready_batch_api(client, shop, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    product.publish_status = PublishStatus.READY_TO_PUBLISH
    db_session.commit()
    res = client.post(f"/api/batches/{batch.id}/publish-ready")
    assert res.status_code == 202
    data = res.json()["data"]
    assert data["queued"] >= 1


def test_shop_isolation_publish(client, shop, db_session, tmp_path, monkeypatch, SessionLocal):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    other = Shop(shop_domain="other-shop.myshopify.com", encrypted_access_token=None)
    db_session.add(other)
    db_session.commit()
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    # Create op for primary shop
    PublishTriggerService(db_session, shop).enqueue_product(product.id)
    op = db_session.query(ProductPublishOperation).one()

    # Client is bound to `shop`; accessing other shop's fabricated UUID returns 404
    res = client.get(f"/api/publish-operations/{uuid4()}")
    assert res.status_code == 404

    # Own operation is visible
    res_ok = client.get(f"/api/publish-operations/{op.id}")
    assert res_ok.status_code == 200
    assert res_ok.json()["data"]["operationId"] == str(op.id)


def test_retry_publish_reuses_file_gid(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    svc = PublishTriggerService(db_session, shop)
    result = svc.enqueue_product(product.id)
    op = db_session.query(ProductPublishOperation).filter_by(id=UUID(result["operationId"])).one()
    assets = [dict(a) for a in (op.assets_json or [])]
    assets[0]["shopify_file_gid"] = "gid://shopify/MediaImage/uploaded"
    assets[0]["shopify_file_status"] = "READY"
    assets[0]["upload_status"] = "READY"
    op.assets_json = assets
    op.status = PublishStatus.PUBLISH_FAILED
    op.last_error_code = "SHOPIFY_FILE_ASSOCIATION_FAILED"
    product.publish_status = PublishStatus.PUBLISH_FAILED
    db_session.commit()

    # Reload to ensure JSON persisted
    db_session.expire_all()
    op = db_session.query(ProductPublishOperation).filter_by(id=UUID(result["operationId"])).one()
    assert op.assets_json[0]["shopify_file_gid"] == "gid://shopify/MediaImage/uploaded"

    retried = svc.enqueue_product(product.id, force_retry=True, trigger=PublishTriggerSource.RETRY)
    assert retried["status"] == PublishStatus.QUEUED.value
    db_session.expire_all()
    op = db_session.query(ProductPublishOperation).filter_by(id=UUID(result["operationId"])).one()
    assert op.attempt_number == 2
    assert op.assets_json[0]["shopify_file_gid"] == "gid://shopify/MediaImage/uploaded"


def test_publisher_conflict_stops_before_upload(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    op_id = UUID(result["operationId"])

    live = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T01:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/99"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/99",
                    "mediaContentType": "IMAGE",
                    "alt": "new",
                    "image": {"url": "https://cdn.shopify.com/new.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {"nodes": []},
    }
    client = MagicMock()
    client.get_product_media_snapshot.return_value = live

    publisher = ProductPublisher(db_session, shop, client=client)
    with patch.object(publisher.uploader, "upload_png") as upload_mock:
        out = publisher.run(op_id)
        upload_mock.assert_not_called()
    assert out.status == PublishStatus.PUBLISH_CONFLICT


def test_publisher_success_never_file_delete(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    monkeypatch.setattr("app.config.settings.shopify_file_status_poll_seconds", 0.01)
    monkeypatch.setattr("app.config.settings.shopify_file_ready_timeout_seconds", 1)
    monkeypatch.setattr("app.config.settings.shopify_reorder_timeout_seconds", 1)

    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    op_id = UUID(result["operationId"])
    generated_gid = image.generated_shopify_file_gid
    assert generated_gid

    original_media = {
        "id": "gid://shopify/MediaImage/10",
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
    }
    new_media = {
        "id": generated_gid,
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/generated-a.png", "width": 10, "height": 10},
    }

    snap_original = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {"nodes": [original_media]},
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/10"}]},
                }
            ]
        },
    }
    snap_both = {
        **snap_original,
        "media": {"nodes": [original_media, new_media]},
        "featuredMedia": {"id": generated_gid},
    }
    snap_final = {
        **snap_original,
        "media": {"nodes": [new_media]},
        "featuredMedia": {"id": generated_gid},
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": generated_gid}]},
                }
            ]
        },
    }

    client = MagicMock()
    client.get_product_media_snapshot.side_effect = [
        snap_original,
        snap_original,
        snap_both,
        snap_both,
        snap_final,
    ]
    client.add_file_product_references.return_value = [{"id": generated_gid}]
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_file_statuses.return_value = [
        {
            "id": generated_gid,
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/generated-a.png", "width": 10, "height": 10},
        }
    ]

    publisher = ProductPublisher(db_session, shop, client=client)
    with patch.object(publisher.uploader, "upload_png") as upload_mock:
        out = publisher.run(op_id)
        upload_mock.assert_not_called()

    assert out.status == PublishStatus.PUBLISHED, (out.last_error_code, out.last_error_message)
    assert not hasattr(client, "fileDelete") or not getattr(client, "fileDelete", MagicMock()).called
    remove_calls = client.remove_file_product_references.call_args_list
    assert remove_calls
    for call in remove_calls:
        kwargs = call.kwargs
        assert kwargs["product_gid"] == "gid://shopify/Product/1"
        assert "gid://shopify/Product/" not in str(kwargs["file_gids"])
    assert out.assets_json[0]["shopify_file_gid"] == generated_gid
    assert out.assets_json[0]["shopify_cdn_url"]


def test_normalize_publish_snapshot_from_graphql():
    raw = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/1"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/1",
                    "mediaContentType": "IMAGE",
                    "alt": "x",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 1, "height": 1},
                }
            ]
        },
        "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1", "media": {"nodes": [{"id": "gid://shopify/MediaImage/1"}]}}]},
    }
    snap = normalize_publish_snapshot(raw)
    assert snap["product_gid"] == "gid://shopify/Product/1"
    assert len(snap["media"]) == 1
    assert snap["variants"][0]["media_gid"] == "gid://shopify/MediaImage/1"


def test_advance_processing_baseline_after_publish(db_session, shop):
    """After publish, ProcessingBaseline must match final Shopify media (new GIDs/CDN)."""
    catalog = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/1",
        title="Pub",
        status="ACTIVE",
    )
    db_session.add(catalog)
    db_session.flush()
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=catalog.id,
            media_snapshot_json=[_media("gid://shopify/MediaImage/10", 0)],
        )
    )
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        product_id=catalog.id,
        shopify_product_gid=catalog.shopify_product_gid,
        status=BatchProductStatus.COMPLETED,
        image_count=1,
        product_snapshot_json={"product_gid": catalog.shopify_product_gid, "title": "Pub"},
        baseline_snapshot_json={"media": [_media("gid://shopify/MediaImage/10", 0)]},
    )
    db_session.add(bp)
    db_session.commit()

    final = _snapshot(
        [
            _media("gid://shopify/MediaImage/999", 0, alt="ai", featured=True),
            _media("gid://shopify/MediaImage/11", 1, alt="other"),
        ]
    )
    ProductPublisher(db_session, shop, client=MagicMock())._advance_processing_baseline_after_publish(bp, final)
    db_session.commit()

    baseline = (
        db_session.query(ProcessingBaseline)
        .filter(ProcessingBaseline.product_id == catalog.id)
        .one()
    )
    gids = {m["media_gid"] for m in (baseline.media_snapshot_json or [])}
    assert gids == {"gid://shopify/MediaImage/999", "gid://shopify/MediaImage/11"}
    assert "gid://shopify/MediaImage/10" not in gids


def _seed_published_operation(
    db_session,
    shop,
    *,
    pre_media: list[dict],
    published_media: list[dict],
    assets: list[dict],
):
    from datetime import datetime, timezone

    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/publish-echo",
        title="Echo Product",
        status="ACTIVE",
    )
    db_session.add(product)
    db_session.flush()

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=len(assets),
    )
    db_session.add(batch)
    db_session.flush()

    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.COMPLETED,
        image_count=len(assets),
        publish_status=PublishStatus.PUBLISHED,
    )
    db_session.add(bp)
    db_session.flush()

    db_session.add(
        ProductPublishOperation(
            shop_id=shop.id,
            processing_batch_id=batch.id,
            batch_product_id=bp.id,
            shopify_product_gid=product.shopify_product_gid,
            status=PublishStatus.PUBLISHED,
            trigger_source=PublishTriggerSource.MANUAL,
            idempotency_key=f"pub-echo-{uuid4()}",
            completed_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            pre_publish_snapshot_json={"media": pre_media},
            assets_json=assets,
        )
    )
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=published_media,
        )
    )
    db_session.commit()
    return product


def test_secondary_queue_skips_publish_echo_after_completed(db_session, shop):
    from app.core.shop_resolver import ensure_shop_settings
    from app.services.secondary_queue import SecondaryQueueService

    ensure_shop_settings(db_session, shop)
    pre = [
        _media("gid://shopify/MediaImage/10", 0),
        _media("gid://shopify/MediaImage/11", 1),
        _media("gid://shopify/MediaImage/12", 2),
        _media("gid://shopify/MediaImage/13", 3),
    ]
    published = [
        _media("gid://shopify/MediaImage/999", 0, featured=True),
        _media("gid://shopify/MediaImage/998", 1),
        _media("gid://shopify/MediaImage/997", 2),
        _media("gid://shopify/MediaImage/996", 3),
    ]
    assets = [
        {
            "shopify_file_gid": "gid://shopify/MediaImage/999",
            "shopify_media_gid": "gid://shopify/MediaImage/999",
            "shopify_cdn_url": "https://cdn.shopify.com/999.png",
            "source_media_gid": "gid://shopify/MediaImage/10",
        }
    ]
    product = _seed_published_operation(
        db_session,
        shop,
        pre_media=pre,
        published_media=published,
        assets=assets,
    )

    result = SecondaryQueueService(db_session, shop).upsert_from_webhook(
        product_gid=product.shopify_product_gid,
        product_snapshot={
            "product_gid": product.shopify_product_gid,
            "title": "Echo Product",
            "status": "ACTIVE",
        },
        media_snapshot=pre,
        webhook_id="wh-after-publish-old-gallery",
    )
    assert result is None


def test_secondary_queue_skips_mixed_publish_echo(db_session, shop):
    from app.core.shop_resolver import ensure_shop_settings
    from app.services.secondary_queue import SecondaryQueueService

    ensure_shop_settings(db_session, shop)
    pre = [_media("gid://shopify/MediaImage/10", 0), _media("gid://shopify/MediaImage/11", 1)]
    published = [_media("gid://shopify/MediaImage/999", 0, featured=True), _media("gid://shopify/MediaImage/11", 1)]
    assets = [
        {
            "shopify_file_gid": "gid://shopify/MediaImage/999",
            "shopify_media_gid": "gid://shopify/MediaImage/999",
            "shopify_cdn_url": "https://cdn.shopify.com/999.png",
            "source_media_gid": "gid://shopify/MediaImage/10",
        }
    ]
    product = _seed_published_operation(
        db_session,
        shop,
        pre_media=pre,
        published_media=published,
        assets=assets,
    )

    result = SecondaryQueueService(db_session, shop).upsert_from_webhook(
        product_gid=product.shopify_product_gid,
        product_snapshot={
            "product_gid": product.shopify_product_gid,
            "title": "Echo Product",
            "status": "ACTIVE",
        },
        media_snapshot=pre + [published[0]],
        webhook_id="wh-after-publish-mixed",
    )
    assert result is None


def test_secondary_queue_enqueues_new_image_after_publish(db_session, shop):
    from app.core.shop_resolver import ensure_shop_settings
    from app.services.secondary_queue import SecondaryQueueService

    ensure_shop_settings(db_session, shop)
    pre = [_media("gid://shopify/MediaImage/10", 0)]
    published = [_media("gid://shopify/MediaImage/999", 0, featured=True)]
    assets = [
        {
            "shopify_file_gid": "gid://shopify/MediaImage/999",
            "shopify_media_gid": "gid://shopify/MediaImage/999",
            "shopify_cdn_url": "https://cdn.shopify.com/999.png",
            "source_media_gid": "gid://shopify/MediaImage/10",
        }
    ]
    product = _seed_published_operation(
        db_session,
        shop,
        pre_media=pre,
        published_media=published,
        assets=assets,
    )

    result = SecondaryQueueService(db_session, shop).upsert_from_webhook(
        product_gid=product.shopify_product_gid,
        product_snapshot={
            "product_gid": product.shopify_product_gid,
            "title": "Echo Product",
            "status": "ACTIVE",
        },
        media_snapshot=[_media("gid://shopify/MediaImage/777", 0, alt="merchant-added")],
        webhook_id="wh-after-publish-real-edit",
    )
    assert result is not None


def test_publish_echo_skips_conversion_for_stale_pending_item(db_session, shop):
    from datetime import datetime, timezone

    from app.core.shop_resolver import ensure_shop_settings
    from app.models import SecondaryQueueItem, SecondaryQueueStatus
    from app.services.primary_batch import PrimaryBatchService
    from app.services.secondary_queue import PUBLISH_ECHO_SKIP_REASON, SecondaryQueueService
    from tests.test_week2 import _configure_central

    ensure_shop_settings(db_session, shop)
    _configure_central(db_session, shop)
    pre = [
        _media("gid://shopify/MediaImage/10", 0),
        _media("gid://shopify/MediaImage/11", 1),
    ]
    published = [
        _media("gid://shopify/MediaImage/999", 0, featured=True),
        _media("gid://shopify/MediaImage/998", 1),
    ]
    assets = [
        {
            "shopify_file_gid": "gid://shopify/MediaImage/999",
            "shopify_media_gid": "gid://shopify/MediaImage/999",
            "shopify_cdn_url": "https://cdn.shopify.com/999.png",
            "source_media_gid": "gid://shopify/MediaImage/10",
        }
    ]
    product = _seed_published_operation(
        db_session,
        shop,
        pre_media=pre,
        published_media=published,
        assets=assets,
    )

    now = datetime.now(timezone.utc)
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        queue_revision=1,
        status=SecondaryQueueStatus.PENDING,
        eligible_product_snapshot_json={
            "product_gid": product.shopify_product_gid,
            "title": "Echo Product",
            "status": "ACTIVE",
        },
        eligible_media_snapshot_json=pre,
        first_queued_at=now,
        last_queued_at=now,
        latest_eligible_webhook_id="wh-stale-before-publish-echo",
        webhook_count=1,
    )
    db_session.add(item)
    db_session.commit()

    assert SecondaryQueueService(db_session, shop).is_publish_webhook_echo(
        product.shopify_product_gid,
        pre,
    )

    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert batch is None
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA
    assert item.skip_reason == PUBLISH_ECHO_SKIP_REASON

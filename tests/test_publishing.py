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
    ProcessingBatch,
    ProductPublishOperation,
    PublishStatus,
    PublishTriggerSource,
    Shop,
    ShopSettings,
    TriggerType,
)
from app.services.output_storage import LocalFilesystemOutputStorage
from app.services.product_publisher import ProductPublisher
from app.services.publish_conflict import compare_publish_snapshots
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


def test_batch_not_terminal_blocks_publish(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    batch.status = BatchStatus.PROCESSING
    batch.pending_product_count = 1
    batch.completed_product_count = 0
    db_session.commit()
    assert not is_batch_processing_terminal(batch)
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
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
    db_session.commit()
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code in {"PUBLISH_OUTPUT_MISSING", "PUBLISH_OUTPUT_INCOMPLETE"}


def test_invalid_png_blocks_publish(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    batch, product, image = _seed_completed_batch(db_session, shop, tmp_path)
    storage = LocalFilesystemOutputStorage(tmp_path / "processed")
    path = storage.resolve_path(image.output_storage_key)
    path.write_bytes(b"not-a-png")
    svc = PublishTriggerService(db_session, shop)
    try:
        svc.enqueue_product(product.id)
        assert False, "expected error"
    except PublishEnqueueError as exc:
        assert exc.code == "PUBLISH_OUTPUT_NOT_PNG"


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

    batch, product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    result = PublishTriggerService(db_session, shop).enqueue_product(product.id)
    op_id = UUID(result["operationId"])

    original_media = {
        "id": "gid://shopify/MediaImage/10",
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
    }
    new_media = {
        "id": "gid://shopify/MediaImage/999",
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/new.png", "width": 10, "height": 10},
    }

    # Snapshots: baseline match → after attach both → after detach only new
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
        "featuredMedia": {"id": "gid://shopify/MediaImage/999"},
    }
    snap_final = {
        **snap_original,
        "media": {"nodes": [new_media]},
        "featuredMedia": {"id": "gid://shopify/MediaImage/999"},
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/999"}]},
                }
            ]
        },
    }

    client = MagicMock()
    client.get_product_media_snapshot.side_effect = [
        snap_original,  # conflict check
        snap_original,  # pre-attach recheck
        snap_both,  # after attach
        snap_both,  # verify new
        snap_final,  # final verify
    ]
    client.add_file_product_references.return_value = [{"id": "gid://shopify/MediaImage/999"}]
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}

    publisher = ProductPublisher(db_session, shop, client=client)

    def fake_upload(*, path, filename, existing_file_gid=None):
        return {
            "file_gid": "gid://shopify/MediaImage/999",
            "file_status": "READY",
            "cdn_url": "https://cdn.shopify.com/new.png",
        }

    with patch.object(publisher.uploader, "upload_png", side_effect=fake_upload):
        out = publisher.run(op_id)

    assert out.status == PublishStatus.PUBLISHED
    # Never delete files — only remove product references
    assert not hasattr(client, "fileDelete") or not getattr(client, "fileDelete", MagicMock()).called
    remove_calls = client.remove_file_product_references.call_args_list
    assert remove_calls
    for call in remove_calls:
        kwargs = call.kwargs
        assert kwargs["product_gid"] == "gid://shopify/Product/1"
        assert "gid://shopify/Product/" not in str(kwargs["file_gids"])


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

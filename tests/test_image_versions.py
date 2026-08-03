"""Normalized per-image Shopify CDN version tests (Option C)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ImageVersion,
    ImageVersionEvent,
    ImageVersionEventType,
    ImageVersionType,
    Product,
    ProductMedia,
    TriggerType,
    ProcessingBatch,
)
from app.services.image_versions import ImageVersionsService, backfill_originals_for_shop
from app.services.output_storage import LocalFilesystemOutputStorage, cleanup_expired_temp_outputs
from app.services.shopify_file_upload import validate_generated_png_for_shopify, PublishUploadError
from app.services.image_processor import ImageProcessor, ProcessingError
from app.services.publish_trigger import PublishTriggerService
from app.services.product_publisher import ProductPublisher
from tests.test_publishing import PNG_BYTES, _media, _seed_completed_batch


def _seed_catalog(db, shop, *, gid="gid://shopify/Product/1", media_gid="gid://shopify/MediaImage/10"):
    product = Product(
        shop_id=shop.id,
        shopify_product_gid=gid,
        title="Ring",
        handle="ring",
        status="ACTIVE",
    )
    db.add(product)
    db.flush()
    media = ProductMedia(
        shop_id=shop.id,
        product_id=product.id,
        shopify_media_gid=media_gid,
        shopify_file_gid="gid://shopify/File/10",
        cdn_url="https://cdn.shopify.com/original.png",
        original_filename="original.png",
        width=100,
        height=100,
        mime_type="image/png",
        alt_text="front",
        position=0,
        is_primary=True,
        is_active=True,
    )
    db.add(media)
    db.commit()
    db.refresh(product)
    db.refresh(media)
    return product, media


def test_original_registered_once_and_idempotent(db_session, shop):
    product, media = _seed_catalog(db_session, shop)
    svc = ImageVersionsService(db_session, shop)
    v1 = svc.ensure_original_from_media(media)
    db_session.commit()
    v2 = svc.ensure_original_from_media(media)
    db_session.commit()
    assert v1.id == v2.id
    assert v1.version_number == 0
    assert v1.is_original is True
    assert v1.is_protected is True
    count = (
        db_session.query(ImageVersion)
        .filter(
            ImageVersion.shop_id == shop.id,
            ImageVersion.product_id == product.id,
            ImageVersion.source_media_gid == media.shopify_media_gid,
        )
        .count()
    )
    assert count == 1
    events = (
        db_session.query(ImageVersionEvent)
        .filter(ImageVersionEvent.event_type == ImageVersionEventType.ORIGINAL_REGISTERED)
        .count()
    )
    assert events == 1


def test_backfill_originals_repeatable(db_session, shop):
    _seed_catalog(db_session, shop)
    first = backfill_originals_for_shop(db_session, shop)
    second = backfill_originals_for_shop(db_session, shop)
    assert first["originalsCreated"] == 1
    assert second["originalsCreated"] == 0


def test_create_generated_keeps_old_versions_one_current(db_session, shop):
    product, media = _seed_catalog(db_session, shop)
    svc = ImageVersionsService(db_session, shop)
    svc.ensure_original_from_media(media)
    db_session.commit()

    g1 = svc.create_generated_after_upload(
        product_id=product.id,
        source_media_gid=media.shopify_media_gid,
        shopify_file_gid="gid://shopify/File/gen1",
        shopify_cdn_url="https://cdn.shopify.com/g1.png",
        width=10,
        height=10,
        file_size_bytes=100,
        checksum="abc",
        upload_idempotency_key="upload:1:abc",
    )
    db_session.commit()
    g2 = svc.create_generated_after_upload(
        product_id=product.id,
        source_media_gid=media.shopify_media_gid,
        shopify_file_gid="gid://shopify/File/gen2",
        shopify_cdn_url="https://cdn.shopify.com/g2.png",
        width=10,
        height=10,
        file_size_bytes=120,
        checksum="def",
        upload_idempotency_key="upload:1:def",
    )
    db_session.commit()

    rows = (
        db_session.query(ImageVersion)
        .filter(
            ImageVersion.product_id == product.id,
            ImageVersion.source_media_gid == media.shopify_media_gid,
        )
        .order_by(ImageVersion.version_number.asc())
        .all()
    )
    assert [r.version_number for r in rows] == [0, 1, 2]
    assert sum(1 for r in rows if r.is_current) == 1
    assert g2.is_current is True
    assert g1.is_current is False
    # Idempotent retry
    again = svc.create_generated_after_upload(
        product_id=product.id,
        source_media_gid=media.shopify_media_gid,
        shopify_file_gid="gid://shopify/File/gen2",
        shopify_cdn_url="https://cdn.shopify.com/g2.png",
        width=10,
        height=10,
        file_size_bytes=120,
        checksum="def",
        upload_idempotency_key="upload:1:def",
    )
    assert again.id == g2.id


def test_reject_oversized_generated_png(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.shopify_image_reject_mb", 0)
    path = tmp_path / "big.png"
    path.write_bytes(PNG_BYTES)
    try:
        validate_generated_png_for_shopify(path)
        assert False, "expected reject"
    except PublishUploadError as exc:
        assert exc.code == "GENERATED_IMAGE_TOO_LARGE"


def test_upload_retry_idempotent_no_duplicate_version(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    product, media = _seed_catalog(db_session, shop)
    storage = LocalFilesystemOutputStorage(tmp_path / "processed")
    key = f"{shop.id}/out.png"
    storage.save_bytes(key=key, data=PNG_BYTES, content_type="image/png")

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid=media.shopify_media_gid,
        shopify_file_gid=media.shopify_file_gid,
        cdn_url=media.cdn_url or "https://cdn.shopify.com/x.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.RETRYING,
        output_storage_key=key,
        output_checksum="abc",
        attempt_count=1,
        error_code="SHOPIFY_BINARY_UPLOAD_FAILED",
    )
    db_session.add(image)
    db_session.commit()

    upload_calls = {"n": 0}

    def fake_upload(*, path, filename, existing_file_gid=None):
        upload_calls["n"] += 1
        return {
            "file_gid": "gid://shopify/File/genX",
            "file_status": "READY",
            "cdn_url": "https://cdn.shopify.com/genX.png",
            "width": 1,
            "height": 1,
        }

    with patch("app.services.image_processor.resolve_shop_access_token", return_value="tok"), patch(
        "app.services.image_processor.ShopifyGraphQLClient"
    ), patch(
        "app.services.image_processor.ShopifyFileUploadService.upload_png",
        side_effect=fake_upload,
    ):
        processor = ImageProcessor(db_session, storage=storage)
        processor._process_single_batch_image(image, bp, worker_id="test")
        db_session.refresh(image)
        assert image.status == BatchImageStatus.COMPLETED
        assert image.generated_shopify_file_gid == "gid://shopify/File/genX"
        assert image.output_storage_key is None  # deleted after success
        assert not storage.exists(key)

        # Second call with same idempotency should not create another version
        image.status = BatchImageStatus.RETRYING
        image.output_storage_key = key
        storage.save_bytes(key=key, data=PNG_BYTES, content_type="image/png")
        image.output_checksum = "abc"
        db_session.commit()
        processor._process_single_batch_image(image, bp, worker_id="test")

    versions = (
        db_session.query(ImageVersion)
        .filter(
            ImageVersion.product_id == product.id,
            ImageVersion.version_type == ImageVersionType.GENERATED,
        )
        .all()
    )
    assert len(versions) == 1
    assert upload_calls["n"] >= 1


def test_publish_reuses_ready_file_without_local(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    catalog, media = _seed_catalog(db_session, shop)
    batch, batch_product, image = _seed_completed_batch(db_session, shop, tmp_path)
    batch_product.product_id = catalog.id
    image.generated_shopify_file_gid = "gid://shopify/File/already"
    image.generated_shopify_cdn_url = "https://cdn.shopify.com/already.png"
    image.output_checksum = "chk"
    # Simulate local deleted after CDN upload
    image.output_storage_key = None
    db_session.commit()

    svc = PublishTriggerService(db_session, shop)
    result = svc.enqueue_product(batch_product.id)
    op_id = UUID(result["operationId"])

    original_media = {
        "id": media.shopify_media_gid,
        "mediaContentType": "IMAGE",
        "alt": "front",
        "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
    }
    new_media = {
        "id": "gid://shopify/MediaImage/999",
        "mediaContentType": "IMAGE",
        "alt": "front",
        "file_gid": "gid://shopify/File/already",
        "image": {"url": "https://cdn.shopify.com/already.png", "width": 10, "height": 10},
        "preview": {"image": {"url": "https://cdn.shopify.com/already.png"}},
    }
    # Attach file gid on new media via mock snapshot helper expectations in publisher
    snap_original = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": media.shopify_media_gid},
        "media": {"nodes": [original_media]},
        "variants": {"nodes": []},
    }
    snap_both = {
        **snap_original,
        "media": {
            "nodes": [
                original_media,
                {
                    **new_media,
                    "id": "gid://shopify/MediaImage/999",
                },
            ]
        },
    }
    snap_final = {
        **snap_original,
        "media": {"nodes": [new_media]},
        "featuredMedia": {"id": "gid://shopify/MediaImage/999"},
    }

    client = MagicMock()
    client.get_product_media_snapshot.side_effect = [
        snap_original,
        snap_original,
        snap_both,
        snap_both,
        snap_final,
    ]
    client.add_file_product_references.return_value = []
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_file_statuses.return_value = [
        {
            "id": "gid://shopify/File/already",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/already.png", "width": 1, "height": 1},
        }
    ]

    publisher = ProductPublisher(db_session, shop, client=client)
    with patch.object(publisher.uploader, "upload_png") as upload_mock:
        upload_mock.side_effect = AssertionError("should not re-upload READY file")
        op = publisher.run(op_id)
    assert op.status.value == "PUBLISHED", (op.last_error_code, op.last_error_message, op.current_stage)


def test_storage_summary_and_api_shop_scope(client, db_session, shop):
    product, media = _seed_catalog(db_session, shop)
    ImageVersionsService(db_session, shop).ensure_original_from_media(media)
    db_session.commit()

    res = client.get("/api/shops/me/image-storage-summary")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["estimateOnly"] is True
    assert body["data"]["totalVersions"] >= 1

    res2 = client.get(f"/api/products/{product.id}/image-versions")
    assert res2.status_code == 200
    assert res2.json()["data"]["total"] >= 1


def test_cross_shop_image_version_blocked(client, db_session, shop, SessionLocal):
    from app.core.crypto import encrypt_token
    from app.models import Shop as ShopModel

    other = ShopModel(shop_domain="other.myshopify.com", encrypted_access_token=encrypt_token("x"))
    db_session.add(other)
    db_session.commit()
    product, media = _seed_catalog(db_session, other, gid="gid://shopify/Product/99")
    ImageVersionsService(db_session, other).ensure_original_from_media(media)
    db_session.commit()

    # client is authenticated as `shop`, not `other`
    res = client.get(f"/api/products/{product.id}/image-versions")
    # Product lookup is shop-scoped via list filter returning empty or 200 with 0
    assert res.status_code == 200
    assert res.json()["data"]["total"] == 0


def test_cleanup_expired_temps(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    monkeypatch.setattr("app.config.settings.processing_temp_retry_retention_hours", 0)
    storage = LocalFilesystemOutputStorage(tmp_path / "processed")
    storage.save_bytes(key="a/b/out.png", data=PNG_BYTES)
    stats = cleanup_expired_temp_outputs(max_age_hours=0)
    assert stats["deleted"] >= 1

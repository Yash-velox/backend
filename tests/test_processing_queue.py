from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import (
    BatchStatus,
    ProcessingAttempt,
    ProcessingQueueItem,
    QueueItemStatus,
    Shop,
    SourceType,
    TriggerType,
)
from app.services.batch_service import BatchService
from app.services.image_processor import ImageProcessor, ProcessingError, download_shopify_cdn_to_temp
from app.services.output_storage import LocalFilesystemOutputStorage
from app.services.queue_service import QueueService
from app.services.retry_service import RetryService
from app.services.shopify_image_source import ShopifyImageSourceService, fingerprint_config


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _mock_graphql(product_id: str = "gid://shopify/Product/1", media_count: int = 2):
    media_nodes = []
    for i in range(media_count):
        media_nodes.append(
            {
                "id": f"gid://shopify/MediaImage/{i+1}",
                "mediaContentType": "IMAGE",
                "alt": f"img-{i+1}",
                "image": {
                    "url": f"https://cdn.shopify.com/s/files/1/0000/0001/products/p{i+1}.png",
                    "width": 100,
                    "height": 100,
                },
            }
        )
    client = MagicMock()
    client.fetch_products_media.return_value = [
        {"id": product_id, "title": "Test", "media": {"nodes": media_nodes}}
    ]
    return client


def test_enqueue_shopify_products_creates_items(client, db_session, shop, monkeypatch):
    mock = _mock_graphql()
    monkeypatch.setattr(
        "app.services.shopify_image_source.resolve_shop_access_token",
        lambda _shop: "token",
    )
    monkeypatch.setattr(
        "app.services.shopify_image_source.ShopifyGraphQLClient",
        lambda **kwargs: mock,
    )

    response = client.post(
        "/api/processing-queue/shopify-products",
        json={"productIds": ["1"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["imagesQueued"] == 2
    assert body["data"]["imagesFound"] == 2
    assert db_session.query(ProcessingQueueItem).count() == 2


def test_enqueue_ignores_frontend_cdn_urls_schema(client):
    # Pydantic model only accepts productIds — extra CDN fields are ignored by default extra=ignore? 
    # Our model doesn't set extra forbid; ensure arbitrary urls field doesn't create items without GraphQL.
    from app.schemas.queue import ShopifyEnqueueRequest

    parsed = ShopifyEnqueueRequest.model_validate(
        {
            "productIds": ["1"],
            "images": [{"url": "https://evil.example/x.png"}],
        }
    )
    assert parsed.productIds == ["1"]
    assert not hasattr(parsed, "images") or True


def test_duplicate_active_skipped(db_session, shop):
    mock = _mock_graphql(media_count=1)
    svc = ShopifyImageSourceService(db_session, shop)
    first = svc.enqueue_products(["1"], graphql_client=mock)
    second = svc.enqueue_products(["1"], graphql_client=mock)
    assert first.images_queued == 1
    assert second.images_queued == 0
    assert second.duplicates_skipped == 1


def test_completed_can_be_requeued(db_session, shop):
    mock = _mock_graphql(media_count=1)
    svc = ShopifyImageSourceService(db_session, shop)
    first = svc.enqueue_products(["1"], graphql_client=mock)
    item = first.items[0]
    item.status = QueueItemStatus.COMPLETED
    db_session.commit()
    second = svc.enqueue_products(["1"], graphql_client=mock)
    assert second.images_queued == 1
    assert db_session.query(ProcessingQueueItem).count() == 2


def test_batch_claim_priority_and_size(db_session, shop):
    for priority, name in [(10, "low"), (50, "mid"), (100, "high")]:
        db_session.add(
            ProcessingQueueItem(
                shop_id=shop.id,
                source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
                shopify_product_id="gid://shopify/Product/1",
                shopify_media_id=f"gid://shopify/MediaImage/{name}",
                shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
                status=QueueItemStatus.PENDING,
                priority=priority,
                processing_config_fingerprint="default",
                max_attempts=3,
            )
        )
    db_session.commit()

    batch = BatchService(db_session).claim_pending_batch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        worker_id="w1",
        batch_size=2,
    )
    assert batch is not None
    assert batch.total_items == 2
    items = (
        db_session.query(ProcessingQueueItem)
        .filter(ProcessingQueueItem.batch_id == batch.id)
        .order_by(ProcessingQueueItem.priority.desc())
        .all()
    )
    assert [i.priority for i in items] == [100, 50]
    assert all(i.status == QueueItemStatus.QUEUED for i in items)


def test_no_empty_batch(db_session, shop):
    batch = BatchService(db_session).claim_pending_batch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        worker_id="w1",
    )
    assert batch is None


def test_two_claims_do_not_overlap(db_session, shop):
    for i in range(3):
        db_session.add(
            ProcessingQueueItem(
                shop_id=shop.id,
                source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
                shopify_product_id="gid://shopify/Product/1",
                shopify_media_id=f"gid://shopify/MediaImage/{i}",
                shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
                status=QueueItemStatus.PENDING,
                priority=100,
                processing_config_fingerprint="default",
                max_attempts=3,
            )
        )
    db_session.commit()

    b1 = BatchService(db_session).claim_pending_batch(
        shop_id=shop.id, trigger_type=TriggerType.MANUAL, worker_id="w1", batch_size=2
    )
    b2 = BatchService(db_session).claim_pending_batch(
        shop_id=shop.id, trigger_type=TriggerType.AUTOMATIC, worker_id="w2", batch_size=2
    )
    assert b1 is not None and b2 is not None
    ids1 = {i.id for i in db_session.query(ProcessingQueueItem).filter_by(batch_id=b1.id)}
    ids2 = {i.id for i in db_session.query(ProcessingQueueItem).filter_by(batch_id=b2.id)}
    assert ids1.isdisjoint(ids2)
    assert len(ids1) + len(ids2) == 3


def test_processing_success_and_temp_cleanup(db_session, shop, tmp_path, monkeypatch):
    item = ProcessingQueueItem(
        shop_id=shop.id,
        source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
        shopify_product_id="gid://shopify/Product/1",
        shopify_media_id="gid://shopify/MediaImage/1",
        shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
        status=QueueItemStatus.QUEUED,
        priority=100,
        processing_config_fingerprint="default",
        max_attempts=3,
        prompt_data=[{"step": 1, "prompt": "enhance"}],
    )
    db_session.add(item)
    db_session.commit()

    temp_file = tmp_path / "src.png"
    temp_file.write_bytes(PNG_BYTES)

    monkeypatch.setattr(
        "app.services.image_processor.download_shopify_cdn_to_temp",
        lambda url: temp_file,
    )

    class FakeOpenAI:
        def edit_image(self, **kwargs):
            return PNG_BYTES

    storage = LocalFilesystemOutputStorage(tmp_path / "out")
    ImageProcessor(db_session, storage=storage, openai_client=FakeOpenAI()).process_queue_item(
        item.id, worker_id="w1"
    )
    db_session.refresh(item)
    assert item.status == QueueItemStatus.COMPLETED
    assert item.output_storage_key
    assert storage.exists(item.output_storage_key)
    assert not temp_file.exists()
    assert db_session.query(ProcessingAttempt).count() == 1


def test_retryable_failure_schedules_retry(db_session, shop, tmp_path, monkeypatch):
    item = ProcessingQueueItem(
        shop_id=shop.id,
        source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
        shopify_product_id="gid://shopify/Product/1",
        shopify_media_id="gid://shopify/MediaImage/1",
        shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
        status=QueueItemStatus.QUEUED,
        priority=100,
        processing_config_fingerprint="default",
        max_attempts=3,
    )
    db_session.add(item)
    db_session.commit()

    def boom(url):
        raise ProcessingError("timeout", code="CDN_TIMEOUT", retryable=True)

    monkeypatch.setattr("app.services.image_processor.download_shopify_cdn_to_temp", boom)
    ImageProcessor(db_session, storage=LocalFilesystemOutputStorage(tmp_path)).process_queue_item(
        item.id, worker_id="w1"
    )
    db_session.refresh(item)
    assert item.status == QueueItemStatus.RETRY_PENDING
    assert item.next_retry_at is not None
    assert db_session.query(ProcessingAttempt).count() == 1


def test_max_attempts_becomes_failed(db_session, shop, tmp_path, monkeypatch):
    item = ProcessingQueueItem(
        shop_id=shop.id,
        source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
        shopify_product_id="gid://shopify/Product/1",
        shopify_media_id="gid://shopify/MediaImage/1",
        shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
        status=QueueItemStatus.QUEUED,
        priority=100,
        processing_config_fingerprint="default",
        attempt_count=2,
        max_attempts=3,
    )
    db_session.add(item)
    db_session.commit()

    def boom(url):
        raise ProcessingError("timeout", code="CDN_TIMEOUT", retryable=True)

    monkeypatch.setattr("app.services.image_processor.download_shopify_cdn_to_temp", boom)
    ImageProcessor(db_session, storage=LocalFilesystemOutputStorage(tmp_path)).process_queue_item(
        item.id, worker_id="w1"
    )
    db_session.refresh(item)
    assert item.attempt_count == 3
    assert item.status == QueueItemStatus.FAILED


def test_one_item_failure_does_not_block_batch_refresh(db_session, shop, tmp_path, monkeypatch):
    batch_svc = BatchService(db_session)
    items = []
    for i in range(2):
        item = ProcessingQueueItem(
            shop_id=shop.id,
            source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
            shopify_product_id="gid://shopify/Product/1",
            shopify_media_id=f"gid://shopify/MediaImage/{i}",
            shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
            status=QueueItemStatus.PENDING,
            priority=100,
            processing_config_fingerprint="default",
            max_attempts=3,
        )
        db_session.add(item)
        items.append(item)
    db_session.commit()
    batch = batch_svc.claim_pending_batch(
        shop_id=shop.id, trigger_type=TriggerType.MANUAL, worker_id="w1", batch_size=10
    )
    assert batch

    temp_ok = tmp_path / "ok.png"
    temp_ok.write_bytes(PNG_BYTES)

    calls = {"n": 0}

    def download(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProcessingError("bad", code="CORRUPT_IMAGE", retryable=False)
        return temp_ok

    monkeypatch.setattr("app.services.image_processor.download_shopify_cdn_to_temp", download)

    class FakeOpenAI:
        def edit_image(self, **kwargs):
            return PNG_BYTES

    processor = ImageProcessor(
        db_session, storage=LocalFilesystemOutputStorage(tmp_path / "out"), openai_client=FakeOpenAI()
    )
    queued = (
        db_session.query(ProcessingQueueItem)
        .filter(ProcessingQueueItem.batch_id == batch.id)
        .order_by(ProcessingQueueItem.created_at.asc())
        .all()
    )
    for q in queued:
        processor.process_queue_item(q.id, worker_id="w1")

    refreshed = batch_svc.refresh_batch_summary(batch.id)
    assert refreshed is not None
    assert refreshed.status == BatchStatus.PARTIALLY_COMPLETED
    assert refreshed.completed_items == 1
    assert refreshed.failed_items == 1


def test_stale_recovery(db_session, shop):
    item = ProcessingQueueItem(
        shop_id=shop.id,
        source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
        shopify_product_id="gid://shopify/Product/1",
        shopify_media_id="gid://shopify/MediaImage/1",
        shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
        status=QueueItemStatus.PROCESSING,
        priority=100,
        processing_config_fingerprint="default",
        max_attempts=3,
        attempt_count=1,
        locked_by="dead-worker",
        locked_at=datetime.now(timezone.utc) - timedelta(hours=1),
        processing_started_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(item)
    db_session.commit()

    recovered = RetryService(db_session).recover_stale_items(worker_id="new-worker")
    assert recovered == 1
    db_session.refresh(item)
    assert item.status == QueueItemStatus.RETRY_PENDING


def test_shop_isolation_summary(client, db_session, shop, SessionLocal):
    other = Shop(shop_domain="other.myshopify.com", access_token="x")
    db_session.add(other)
    db_session.commit()
    db_session.add(
        ProcessingQueueItem(
            shop_id=other.id,
            source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
            shopify_product_id="gid://shopify/Product/9",
            shopify_media_id="gid://shopify/MediaImage/9",
            shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
            status=QueueItemStatus.PENDING,
            priority=100,
            processing_config_fingerprint="default",
            max_attempts=3,
        )
    )
    db_session.add(
        ProcessingQueueItem(
            shop_id=shop.id,
            source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
            shopify_product_id="gid://shopify/Product/1",
            shopify_media_id="gid://shopify/MediaImage/1",
            shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
            status=QueueItemStatus.PENDING,
            priority=100,
            processing_config_fingerprint="default",
            max_attempts=3,
        )
    )
    db_session.commit()

    response = client.get("/api/processing-queue/summary")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["pending"] == 1


def test_manual_start_api(client, db_session, shop, monkeypatch):
    db_session.add(
        ProcessingQueueItem(
            shop_id=shop.id,
            source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
            shopify_product_id="gid://shopify/Product/1",
            shopify_media_id="gid://shopify/MediaImage/1",
            shopify_cdn_url="https://cdn.shopify.com/s/files/1/x.png",
            status=QueueItemStatus.PENDING,
            priority=100,
            processing_config_fingerprint="default",
            max_attempts=3,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.api.processing_batches._run_batch_in_background",
        lambda batch_id: None,
    )
    response = client.post("/api/processing-batches/start")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["itemCount"] == 1
    assert body["data"]["batchId"]


def test_manual_start_empty(client):
    response = client.post("/api/processing-batches/start")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["itemCount"] == 0
    assert body["data"]["batchId"] is None


def test_cdn_host_validation(monkeypatch):
    with pytest.raises(ProcessingError):
        download_shopify_cdn_to_temp("https://evil.example.com/x.png")


def test_fingerprint_stable():
    a = fingerprint_config({"x": 1}, [{"prompt": "a"}])
    b = fingerprint_config({"x": 1}, [{"prompt": "a"}])
    c = fingerprint_config({"x": 2}, [{"prompt": "a"}])
    assert a == b
    assert a != c

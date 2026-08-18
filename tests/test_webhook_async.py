from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.core.crypto import sign_internal_payload
from app.models import WebhookEvent, WebhookProcessingResult
from app.services.webhook_intake import WebhookIntakeService


def _payload(product_id: int, title: str = "Webhook Product") -> dict:
    return {
        "admin_graphql_api_id": f"gid://shopify/Product/{product_id}",
        "id": product_id,
        "title": title,
        "status": "active",
        "handle": f"webhook-product-{product_id}",
    }


def test_http_webhook_enqueues_without_shopify_fetch(client, shop, db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_handoff_secret", "handoff-secret")
    monkeypatch.setattr("app.core.crypto.settings.internal_handoff_secret", "handoff-secret")
    body = {
        "shop": shop.shop_domain,
        "topic": "products/update",
        "webhookId": "wh-http-enqueue-1",
        "payload": _payload(101),
    }
    raw = json.dumps(body).encode("utf-8")
    ts, sig = sign_internal_payload(raw)

    with patch(
        "app.services.webhook_intake.create_shopify_graphql_client",
        side_effect=AssertionError("GraphQL must not run during HTTP enqueue"),
    ):
        res = client.post(
            "/internal/webhooks/products-update",
            content=raw,
            headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig},
        )

    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["processingResult"] == WebhookProcessingResult.QUEUED.value
    db_session.expire_all()
    event = db_session.get(WebhookEvent, UUID(data["eventId"]))
    assert event is not None
    assert event.payload_json["title"] == "Webhook Product"
    assert event.shopify_product_gid == "gid://shopify/Product/101"


def test_http_webhook_dedupes_same_id(client, shop, db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_handoff_secret", "handoff-secret")
    monkeypatch.setattr("app.core.crypto.settings.internal_handoff_secret", "handoff-secret")
    body = {
        "shop": shop.shop_domain,
        "topic": "products/update",
        "webhookId": "wh-http-dup-1",
        "payload": _payload(202),
    }
    raw = json.dumps(body).encode("utf-8")
    ts, sig = sign_internal_payload(raw)
    headers = {"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig}

    first = client.post("/internal/webhooks/products-update", content=raw, headers=headers)
    second = client.post("/internal/webhooks/products-update", content=raw, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["eventId"] == second.json()["data"]["eventId"]
    db_session.expire_all()
    rows = db_session.query(WebhookEvent).filter(WebhookEvent.shopify_webhook_id == "wh-http-dup-1").all()
    assert len(rows) == 1


def test_claim_caps_global_concurrency(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.config.settings.webhook_process_concurrency", 1)
    monkeypatch.setattr("app.config.settings.webhook_process_concurrency_per_shop", 2)
    monkeypatch.setattr("app.config.settings.webhook_claim_limit", 10)
    svc = WebhookIntakeService(db_session)
    first = svc.enqueue_products_update(shop.shop_domain, "wh-cap-1", "products/update", _payload(1), "h1")
    second = svc.enqueue_products_update(shop.shop_domain, "wh-cap-2", "products/update", _payload(2), "h2")
    third = svc.enqueue_products_update(shop.shop_domain, "wh-cap-3", "products/update", _payload(3), "h3")
    assert {first.processing_result, second.processing_result, third.processing_result} == {
        WebhookProcessingResult.QUEUED
    }

    claimed = svc.claim_queued(worker_id="worker-cap")
    assert len(claimed) == 1
    db_session.expire_all()
    claimed_row = db_session.get(WebhookEvent, claimed[0])
    assert claimed_row.processing_result == WebhookProcessingResult.PROCESSING
    waiting = (
        db_session.query(WebhookEvent)
        .filter(WebhookEvent.processing_result == WebhookProcessingResult.QUEUED)
        .count()
    )
    assert waiting == 2

    none_more = svc.claim_queued(worker_id="worker-cap")
    assert none_more == []


def test_claim_caps_per_shop_and_coalesces_same_product(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.config.settings.webhook_process_concurrency", 4)
    monkeypatch.setattr("app.config.settings.webhook_process_concurrency_per_shop", 1)
    monkeypatch.setattr("app.config.settings.webhook_claim_limit", 10)
    svc = WebhookIntakeService(db_session)
    older = svc.enqueue_products_update(
        shop.shop_domain, "wh-same-old", "products/update", _payload(9, "Older"), "old"
    )
    newer = svc.enqueue_products_update(
        shop.shop_domain, "wh-same-new", "products/update", _payload(9, "Newer"), "new"
    )
    other = svc.enqueue_products_update(
        shop.shop_domain, "wh-other", "products/update", _payload(10, "Other"), "other"
    )
    older.received_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    newer.received_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    other.received_at = datetime.now(timezone.utc)
    db_session.commit()

    claimed = svc.claim_queued(worker_id="worker-coalesce")
    assert len(claimed) == 1
    db_session.expire_all()
    claimed_row = db_session.get(WebhookEvent, claimed[0])
    older_row = db_session.get(WebhookEvent, older.id)
    other_row = db_session.get(WebhookEvent, other.id)
    assert claimed_row.id == newer.id
    assert claimed_row.processing_result == WebhookProcessingResult.PROCESSING
    assert older_row.processing_result == WebhookProcessingResult.IGNORED
    assert "Superseded" in (older_row.error_summary or "")
    assert other_row.processing_result == WebhookProcessingResult.QUEUED


def test_process_event_runs_after_enqueue(db_session, shop, monkeypatch):
    graphql_node = {
        "id": "gid://shopify/Product/4243",
        "title": "Queued Ring",
        "status": "ACTIVE",
        "productType": "Rings",
        "handle": "queued-ring",
        "tags": [],
        "featuredMedia": None,
        "media": {"nodes": []},
        "variants": {"nodes": []},
    }
    mock_client = MagicMock()
    mock_client.fetch_product_by_gid.return_value = graphql_node
    monkeypatch.setattr(
        "app.services.webhook_intake.create_shopify_graphql_client",
        lambda _db, _shop: mock_client,
    )
    svc = WebhookIntakeService(db_session)
    queued = svc.enqueue_products_update(
        shop.shop_domain,
        "wh-process-later",
        "products/update",
        _payload(4243, "Queued Ring"),
        "hash-later",
    )
    assert queued.processing_result == WebhookProcessingResult.QUEUED
    mock_client.fetch_product_by_gid.assert_not_called()

    processed = svc.process_event(queued.id)
    assert processed.processing_result == WebhookProcessingResult.ACCEPTED
    mock_client.fetch_product_by_gid.assert_called_once()


def test_stale_processing_lock_is_requeued(db_session, shop):
    event = WebhookEvent(
        shop_id=shop.id,
        shopify_webhook_id="wh-stale-1",
        topic="products/update",
        shopify_product_gid="gid://shopify/Product/1",
        payload_json=_payload(1),
        processing_result=WebhookProcessingResult.PROCESSING,
        claimed_by="dead-worker",
        claimed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        attempt_count=1,
    )
    db_session.add(event)
    db_session.commit()

    recovered = WebhookIntakeService(db_session).recover_stale_processing(worker_id="live-worker")
    assert recovered == 1
    db_session.refresh(event)
    assert event.processing_result == WebhookProcessingResult.QUEUED
    assert event.claimed_by is None


def test_unknown_shop_is_ignored_without_queueing_work(db_session):
    event = WebhookIntakeService(db_session).enqueue_products_update(
        "missing-shop.myshopify.com",
        "wh-missing-shop",
        "products/update",
        _payload(1),
        "hash",
    )
    assert event.processing_result == WebhookProcessingResult.IGNORED
    assert event.payload_json is None
    assert event.error_summary == "Shop not found"

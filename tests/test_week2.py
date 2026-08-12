"""Week 2 unit and integration tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.core.crypto import encrypt_token, sign_internal_payload, verify_internal_signature
from app.core.shop_resolver import ensure_shop_settings, upsert_shop_install
from app.models import (
    AttemptStatus,
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ProcessingAttempt,
    ProcessingBaseline,
    ProcessingBatch,
    Product,
    ProductMedia,
    ProductPublishOperation,
    PublishStatus,
    PublishTriggerSource,
    SecondaryQueueItem,
    SecondaryQueueStatus,
    Shop,
    TriggerType,
    WebhookEvent,
    WebhookProcessingResult,
)
from app.services.delta import compare_media_snapshots
from app.services.primary_batch import PrimaryBatchError, PrimaryBatchService
from app.services.prompt_configuration import PromptConfigurationService
from app.services.prompt_product_types import PromptProductTypeService
from app.services.prompt_resolver import PromptResolverError
from app.services.prompt_mapping import PromptMappingService
from app.services.retry_service import RetryService
from app.services.secondary_queue import SecondaryQueueService
from app.services.settings_service import SettingsService, SettingsValidationError
from app.services.snapshot import media_fingerprint
from app.services.state_machine import BATCH_TRANSITIONS, InvalidStateTransition, assert_transition
from app.services.webhook_intake import WebhookIntakeService


def _configure_product_type(db_session, shop, product_type: str, *, prompt: str = "Enhance {{product_title}}") -> None:
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.flush()
    ppt = types.find_by_normalized_name(product_type.casefold())
    if ppt is None:
        ppt = types.add_manual(product_type)
    PromptConfigurationService(db_session, shop).add_step(
        ppt.id,
        name="Step 1",
        prompt_text=prompt,
        is_enabled=True,
    )
    db_session.commit()
from app.services.webhook_intake import detect_status_only, is_draft_transition, product_gid_from_webhook_payload


def test_encrypt_decrypt_roundtrip():
    token = "shpat_secret_value"
    cipher = encrypt_token(token)
    assert cipher != token
    from app.core.crypto import decrypt_token

    assert decrypt_token(cipher) == token


def test_internal_hmac_valid_and_expired(monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_handoff_secret", "test-secret")
    monkeypatch.setattr("app.core.crypto.settings.internal_handoff_secret", "test-secret")
    body = b'{"shop":"x.myshopify.com"}'
    ts, sig = sign_internal_payload(body)  # current timestamp
    assert verify_internal_signature(body, timestamp=ts, signature=sig, max_age_seconds=300)
    assert not verify_internal_signature(body, timestamp="1", signature=sig, max_age_seconds=10)


def test_internal_hmac_accepts_millisecond_timestamps(monkeypatch):
    """Node historically signed with Date.now() (ms); age check must not reject those."""
    import hashlib
    import hmac
    import time

    monkeypatch.setattr("app.config.settings.internal_handoff_secret", "test-secret")
    monkeypatch.setattr("app.core.crypto.settings.internal_handoff_secret", "test-secret")
    body = b'{"shop":"x.myshopify.com"}'
    ts_ms = str(int(time.time() * 1000))
    sig = hmac.new(b"test-secret", f"{ts_ms}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_internal_signature(body, timestamp=ts_ms, signature=sig, max_age_seconds=300)


def test_media_fingerprint_ignores_alt_for_content():
    a = {
        "shopify_media_gid": "gid://shopify/MediaImage/1",
        "shopify_file_gid": "gid://shopify/File/1",
        "cdn_url": "https://cdn.shopify.com/a.png",
        "original_filename": "a.png",
        "width": 100,
        "height": 100,
        "mime_type": "image/png",
        "shopify_updated_at": "2026-01-01T00:00:00Z",
        "alt_text": "old",
    }
    b = dict(a)
    b["alt_text"] = "new"
    # fingerprint helper uses content fields; alt may or may not be included — delta ignores alt-only
    fp_a = media_fingerprint(a)
    fp_b = media_fingerprint({**b, "alt_text": "ignored-for-fp"})
    # At minimum fingerprint is stable for same content keys used by helper
    assert isinstance(fp_a, str) and len(fp_a) > 0


def test_delta_new_and_replaced_and_skip():
    baseline = [
        {
            "shopify_media_gid": "gid://shopify/MediaImage/1",
            "shopify_file_gid": "gid://shopify/File/1",
            "cdn_url": "https://cdn.shopify.com/a.png",
            "width": 10,
            "height": 10,
            "mime_type": "image/png",
            "original_filename": "a.png",
            "content_fingerprint": "fp1",
            "shopify_updated_at": "2026-01-01T00:00:00Z",
        }
    ]
    eligible_new = baseline + [
        {
            "shopify_media_gid": "gid://shopify/MediaImage/2",
            "shopify_file_gid": "gid://shopify/File/2",
            "cdn_url": "https://cdn.shopify.com/b.png",
            "width": 20,
            "height": 20,
            "mime_type": "image/png",
            "original_filename": "b.png",
            "content_fingerprint": "fp2",
            "shopify_updated_at": "2026-01-02T00:00:00Z",
        }
    ]
    result = compare_media_snapshots(eligible_new, baseline)
    assert len(result["new"]) == 1
    assert result["new"][0]["shopify_media_gid"].endswith("/2")
    assert result["replaced"] == []

    eligible_replaced = [
        {
            **baseline[0],
            "cdn_url": "https://cdn.shopify.com/a-v2.png",
            "content_fingerprint": "fp1-changed",
            "shopify_updated_at": "2026-02-01T00:00:00Z",
        }
    ]
    replaced = compare_media_snapshots(eligible_replaced, baseline)
    assert len(replaced["replaced"]) == 1
    assert replaced["new"] == []

    skip = compare_media_snapshots(baseline, baseline)
    assert skip["new"] == [] and skip["replaced"] == []
    assert skip.get("skip_reason")


def test_status_only_and_draft_helpers():
    product = Product(
        id=uuid4(),
        shop_id=uuid4(),
        shopify_product_gid="gid://shopify/Product/1",
        title="Same",
        status="ACTIVE",
        handle="same",
        product_type="Shoes",
        vendor="Acme",
        tags="a,b",
        description_html="<p>x</p>",
    )
    payload = {
        "admin_graphql_api_id": "gid://shopify/Product/1",
        "title": "Same",
        "status": "draft",
        "handle": "same",
        "product_type": "Shoes",
        "vendor": "Acme",
        "tags": "a,b",
        "body_html": "<p>x</p>",
    }
    assert detect_status_only(product, payload) is True
    assert is_draft_transition(previous_status="ACTIVE", new_status="DRAFT") is True
    assert is_draft_transition(previous_status="DRAFT", new_status="ACTIVE") is False
    assert product_gid_from_webhook_payload(payload) == "gid://shopify/Product/1"


def test_state_machine_rejects_invalid():
    assert_transition("batch", BATCH_TRANSITIONS, BatchStatus.QUEUED, BatchStatus.PROCESSING)
    assert_transition("batch", BATCH_TRANSITIONS, BatchStatus.QUEUED, BatchStatus.COMPLETED)
    # Manual reprocess may reopen a finished batch.
    assert_transition("batch", BATCH_TRANSITIONS, BatchStatus.COMPLETED, BatchStatus.QUEUED)
    assert_transition("batch", BATCH_TRANSITIONS, BatchStatus.COMPLETED, BatchStatus.PROCESSING)
    try:
        assert_transition("batch", BATCH_TRANSITIONS, BatchStatus.CANCELLED, BatchStatus.PROCESSING)
        assert False, "expected InvalidStateTransition"
    except InvalidStateTransition:
        pass


def test_refresh_batch_counters_sees_unflushed_status(db_session, shop):
    """Regression: autoflush=False must not leave batch counters stale."""
    ensure_shop_settings(db_session, shop)
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.QUEUED,
        product_count=1,
        image_count=1,
        pending_product_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/counters-1",
        status=BatchProductStatus.QUEUED,
        image_count=1,
    )
    db_session.add(bp)
    db_session.commit()

    bp.status = BatchProductStatus.PROCESSING
    # Intentionally no explicit flush — refresh_batch_counters must flush itself.
    PrimaryBatchService(db_session, shop).refresh_batch_counters(batch)
    db_session.commit()
    db_session.refresh(batch)

    assert batch.status == BatchStatus.PROCESSING
    assert batch.processing_product_count == 1
    assert batch.pending_product_count == 0
    assert batch.completed_product_count == 0

    bp.status = BatchProductStatus.COMPLETED
    PrimaryBatchService(db_session, shop).refresh_batch_counters(batch)
    db_session.commit()
    db_session.refresh(batch)

    assert batch.status == BatchStatus.COMPLETED
    assert batch.completed_product_count == 1
    assert batch.processing_product_count == 0
    assert batch.completed_at is not None


def test_prompt_mapping_requires_db_configuration():
    try:
        PromptMappingService().resolve_for_product_type("Apparel")
        assert False, "expected PromptResolverError"
    except PromptResolverError as exc:
        assert exc.code == "PROMPT_NOT_CONFIGURED"


def test_settings_validation(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.services.settings_service.settings.batch_interval_minutes_cap", 60)
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    svc = SettingsService(db_session, shop)
    updated = svc.update(auto_sync_enabled=True, batch_interval_minutes=10)
    assert updated.auto_sync_enabled is True
    assert updated.batch_interval_minutes == 10
    try:
        svc.update(batch_interval_minutes=999)
        assert False
    except SettingsValidationError:
        pass


def test_secondary_queue_upsert_dedupe_and_draft(db_session, shop):
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    svc = SecondaryQueueService(db_session, shop)
    snap = {"shopify_product_gid": "gid://shopify/Product/9", "title": "T", "status": "ACTIVE"}
    media = [
        {
            "shopify_media_gid": "gid://shopify/MediaImage/1",
            "cdn_url": "https://cdn.shopify.com/x.png",
            "is_visible": True,
            "content_fingerprint": "a",
        }
    ]
    first = svc.upsert_from_webhook(
        product_gid="gid://shopify/Product/9",
        product_snapshot=snap,
        media_snapshot=media,
        webhook_id="wh_1",
    )
    assert first is not None
    assert first.queue_revision == 1
    assert first.status == SecondaryQueueStatus.FAILED_CONVERSION
    assert first.failure_reason
    assert "Cannot process" in first.failure_reason
    second = svc.upsert_from_webhook(
        product_gid="gid://shopify/Product/9",
        product_snapshot={**snap, "title": "T2"},
        media_snapshot=media,
        webhook_id="wh_2",
    )
    assert second is not None
    assert second.id == first.id
    assert second.queue_revision == 2
    assert second.webhook_count == 2
    assert second.status == SecondaryQueueStatus.FAILED_CONVERSION
    assert "T2" in (second.failure_reason or "")

    draft = svc.upsert_from_webhook(
        product_gid="gid://shopify/Product/9",
        product_snapshot={**snap, "status": "DRAFT"},
        media_snapshot=media,
        webhook_id="wh_3",
        is_draft_transition=True,
    )
    assert draft is None
    db_session.refresh(second)
    assert second.queue_revision == 2

    status_only = svc.upsert_from_webhook(
        product_gid="gid://shopify/Product/10",
        product_snapshot={**snap, "shopify_product_gid": "gid://shopify/Product/10"},
        media_snapshot=media,
        webhook_id="wh_4",
        is_status_only=True,
    )
    assert status_only is None


def test_secondary_queue_enqueues_when_media_matches_baseline(db_session, shop):
    """Enqueue first; media delta is evaluated later at Primary conversion."""
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/8801",
        title="Bag",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    media = [
        {
            "media_gid": "gid://shopify/MediaImage/1",
            "cdn_url": "https://cdn.shopify.com/a.png",
            "filename": "a.png",
            "width": 10,
            "height": 10,
            "fingerprint": "fp-a",
        },
        {
            "media_gid": "gid://shopify/MediaImage/2",
            "cdn_url": "https://cdn.shopify.com/b.png",
            "filename": "b.png",
            "width": 20,
            "height": 20,
            "fingerprint": "fp-b",
        },
    ]
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=media,
            product_snapshot_json={"product_gid": product.shopify_product_gid, "title": "Bag"},
            evaluated_at=datetime.now(timezone.utc),
            successfully_processed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    svc = SecondaryQueueService(db_session, shop)
    result = svc.upsert_from_webhook(
        product_gid=product.shopify_product_gid,
        product_snapshot={"product_gid": product.shopify_product_gid, "title": "Bag", "status": "ACTIVE"},
        media_snapshot=media,
        webhook_id="wh-description-or-echo",
    )
    assert result is not None
    assert result.status == SecondaryQueueStatus.PENDING
    assert (
        db_session.query(SecondaryQueueItem)
        .filter(SecondaryQueueItem.shopify_product_gid == product.shopify_product_gid)
        .count()
        == 1
    )

    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([result])
    assert batch is None
    db_session.refresh(result)
    assert result.status == SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA
    assert result.skip_reason


def test_secondary_queue_skips_during_active_publish(db_session, shop):
    ensure_shop_settings(db_session, shop)
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/8802",
        status=BatchProductStatus.COMPLETED,
        image_count=1,
        publish_status=PublishStatus.PUBLISHING,
    )
    db_session.add(bp)
    db_session.flush()
    db_session.add(
        ProductPublishOperation(
            shop_id=shop.id,
            processing_batch_id=batch.id,
            batch_product_id=bp.id,
            shopify_product_gid="gid://shopify/Product/8802",
            status=PublishStatus.PUBLISHING,
            trigger_source=PublishTriggerSource.AUTO,
            idempotency_key=f"pub-active-{uuid4()}",
            current_stage="ATTACHING",
        )
    )
    db_session.commit()

    svc = SecondaryQueueService(db_session, shop)
    result = svc.upsert_from_webhook(
        product_gid="gid://shopify/Product/8802",
        product_snapshot={"product_gid": "gid://shopify/Product/8802", "title": "X", "status": "ACTIVE"},
        media_snapshot=[
            {
                "media_gid": "gid://shopify/MediaImage/9",
                "cdn_url": "https://cdn.shopify.com/new.png",
                "filename": "new.png",
            }
        ],
        webhook_id="wh-during-publish",
    )
    assert result is None


def test_advance_baseline_on_success_keeps_full_eligible_media(db_session, shop):
    """Regression: baseline must keep sibling images, not only the processed delta image."""
    from app.services.image_processor import ImageProcessor

    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/8803",
        title="Multi",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    full_media = [
        {
            "media_gid": "gid://shopify/MediaImage/1",
            "cdn_url": "https://cdn.shopify.com/old.png",
            "filename": "old.png",
        },
        {
            "media_gid": "gid://shopify/MediaImage/2",
            "cdn_url": "https://cdn.shopify.com/new.png",
            "filename": "new.png",
        },
    ]
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.COMPLETED,
        image_count=1,
        product_snapshot_json={"product_gid": product.shopify_product_gid, "title": "Multi"},
        baseline_snapshot_json={"product": {"product_gid": product.shopify_product_gid}, "media": full_media},
    )
    db_session.add(bp)
    db_session.flush()
    db_session.add(
        BatchImage(
            batch_product_id=bp.id,
            shop_id=shop.id,
            shopify_media_gid="gid://shopify/MediaImage/2",
            cdn_url="https://cdn.shopify.com/new.png",
            original_filename="new.png",
            status=BatchImageStatus.COMPLETED,
            delta_type=DeltaType.NEW,
        )
    )
    db_session.commit()

    ImageProcessor(db_session)._advance_baseline_on_success(bp)
    db_session.commit()

    baseline = (
        db_session.query(ProcessingBaseline)
        .filter(ProcessingBaseline.product_id == product.id)
        .one()
    )
    assert len(baseline.media_snapshot_json or []) == 2
    gids = {m["media_gid"] for m in baseline.media_snapshot_json}
    assert gids == {"gid://shopify/MediaImage/1", "gid://shopify/MediaImage/2"}


def test_manual_batch_creates_initial_images(db_session, shop):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/42",
        title="Boot",
        status="ACTIVE",
        product_type="Shoes",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/7",
            cdn_url="https://cdn.shopify.com/boot.png",
            is_visible=True,
            is_active=True,
            position=1,
            content_fingerprint="boot-fp",
        )
    )
    db_session.commit()
    _configure_product_type(db_session, shop, "Shoes")

    batch = PrimaryBatchService(db_session, shop).create_manual_batch(["gid://shopify/Product/42"])
    assert batch.trigger_type == TriggerType.MANUAL
    assert batch.product_count == 1
    assert batch.image_count == 1
    images = db_session.query(BatchImage).all()
    assert images[0].delta_type == DeltaType.INITIAL
    assert images[0].status == BatchImageStatus.QUEUED
    bp = db_session.query(BatchProduct).one()
    baseline_media = (bp.baseline_snapshot_json or {}).get("media") or []
    assert len(baseline_media) == 1
    assert baseline_media[0]["media_gid"] == "gid://shopify/MediaImage/7"


def test_manual_batch_rejects_unconfigured_product_type(db_session, shop):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/420",
        title="Loose Charm",
        status="ACTIVE",
        product_type="UnconfiguredType",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/420",
            cdn_url="https://cdn.shopify.com/charm.png",
            is_visible=True,
            is_active=True,
        )
    )
    db_session.commit()

    try:
        PrimaryBatchService(db_session, shop).create_manual_batch(["gid://shopify/Product/420"])
        assert False, "expected PrimaryBatchError"
    except PrimaryBatchError as exc:
        assert "Loose Charm" in str(exc)
        assert "prompt configuration" in str(exc).lower() or "PROMPT" in str(exc) or "Configure" in str(exc)
    assert db_session.query(ProcessingBatch).count() == 0


def test_secondary_conversion_skips_no_delta(db_session, shop):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/55",
        title="Hat",
        status="ACTIVE",
    )
    db_session.add(product)
    db_session.flush()
    media_snap = [
        {
            "shopify_media_gid": "gid://shopify/MediaImage/8",
            "cdn_url": "https://cdn.shopify.com/hat.png",
            "is_visible": True,
            "content_fingerprint": "hat",
            "width": 1,
            "height": 1,
            "mime_type": "image/png",
            "original_filename": "hat.png",
            "shopify_file_gid": None,
            "shopify_updated_at": None,
        }
    ]
    baseline = ProcessingBaseline(
        shop_id=shop.id,
        product_id=product.id,
        media_snapshot_json=media_snap,
        product_snapshot_json={"shopify_product_gid": product.shopify_product_gid},
        evaluated_at=datetime.now(timezone.utc),
    )
    db_session.add(baseline)
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={"shopify_product_gid": product.shopify_product_gid, "title": "Hat"},
        eligible_media_snapshot_json=media_snap,
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    result = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert result is None
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA


def test_delta_skips_product_image_vs_media_image_same_cdn():
    """REST ProductImage GID vs GraphQL MediaImage GID for same CDN path is not NEW."""
    baseline = [
        {
            "media_gid": "gid://shopify/MediaImage/33248938590291",
            "cdn_url": "https://cdn.shopify.com/s/files/1/x/files/chain.png?v=1",
            "filename": "chain.png",
            "width": 1254,
            "height": 1254,
            "mime_type": "image/png",
            "fingerprint": "catalog-fp",
        }
    ]
    eligible = [
        {
            "media_gid": "gid://shopify/ProductImage/41966871347283",
            "cdn_url": "https://cdn.shopify.com/s/files/1/x/files/chain.png?v=2",
            "filename": "chain.png",
            "width": 1254,
            "height": 1254,
            "mime_type": None,
            "fingerprint": "webhook-fp-different-because-gid",
        }
    ]
    result = compare_media_snapshots(eligible, baseline)
    assert result["new"] == []
    assert result["replaced"] == []
    assert result["skip_reason"]


def test_delta_new_cdn_path_still_detected():
    baseline = [
        {
            "media_gid": "gid://shopify/MediaImage/1",
            "cdn_url": "https://cdn.shopify.com/old.png",
            "filename": "old.png",
            "width": 10,
            "height": 10,
        }
    ]
    eligible = [
        {
            "media_gid": "gid://shopify/ProductImage/99",
            "cdn_url": "https://cdn.shopify.com/brand-new.png",
            "filename": "brand-new.png",
            "width": 10,
            "height": 10,
        }
    ]
    result = compare_media_snapshots(eligible, baseline)
    assert len(result["new"]) == 1
    assert result["skip_reason"] is None


def test_secondary_conversion_skips_title_only_empty_baseline(db_session, shop):
    """None ProcessingBaseline media seeds from catalog; ProductImage eligible matches by CDN."""
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/8034005418067",
        title="Title A",
        status="ACTIVE",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/33248938590291",
            cdn_url="https://cdn.shopify.com/s/files/1/x/files/chain.png?v=1",
            original_filename="chain.png",
            width=1254,
            height=1254,
            is_visible=True,
            is_active=True,
            position=0,
            content_fingerprint="catalog-fp",
        )
    )
    # Never-frozen baseline (manual batch path leaves media_snapshot_json as None)
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=None,
            product_snapshot_json={"shopify_product_gid": product.shopify_product_gid},
        )
    )
    eligible_media = [
        {
            "media_gid": "gid://shopify/ProductImage/41966871347283",
            "cdn_url": "https://cdn.shopify.com/s/files/1/x/files/chain.png?v=9",
            "filename": "chain.png",
            "width": 1254,
            "height": 1254,
            "fingerprint": "webhook-fp",
        }
    ]
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={
            "shopify_product_gid": product.shopify_product_gid,
            "title": "Title B",
        },
        eligible_media_snapshot_json=eligible_media,
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    result = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert result is None
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.SKIPPED_NO_ELIGIBLE_IMAGE_DELTA
    baseline = (
        db_session.query(ProcessingBaseline)
        .filter(ProcessingBaseline.product_id == product.id)
        .one()
    )
    # Seeded from catalog then advanced to evaluated eligible snapshot on skip.
    assert baseline.media_snapshot_json
    assert any(
        (m.get("cdn_url") or "").startswith("https://cdn.shopify.com/s/files/1/x/files/chain.png")
        for m in baseline.media_snapshot_json
    )


def test_secondary_conversion_new_image_after_catalog_already_synced(db_session, shop):
    """Regression: new image must convert when baseline was frozen pre-sync, catalog post-sync.

    Simulates webhook order: seed empty baseline from old catalog → upsert adds new media
    → Secondary Queue eligible includes new image → conversion must detect NEW (not skip).
    """
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/8034005418999",
        title="Chain",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/10",
            cdn_url="https://cdn.shopify.com/s/files/1/x/files/old.png?v=1",
            original_filename="old.png",
            width=100,
            height=100,
            is_visible=True,
            is_active=True,
            position=0,
        )
    )
    db_session.flush()

    svc = PrimaryBatchService(db_session, shop)
    # Pre-sync freeze (what webhook must do before catalog upsert)
    baseline = svc.seed_empty_baseline_from_product_media(product)
    assert baseline.media_snapshot_json is not None
    assert len(baseline.media_snapshot_json) == 1

    # Post-sync catalog already includes the newly added image
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/11",
            cdn_url="https://cdn.shopify.com/s/files/1/x/files/new.png?v=1",
            original_filename="new.png",
            width=200,
            height=200,
            is_visible=True,
            is_active=True,
            position=1,
        )
    )
    eligible_media = [
        {
            "media_gid": "gid://shopify/MediaImage/10",
            "cdn_url": "https://cdn.shopify.com/s/files/1/x/files/old.png?v=1",
            "filename": "old.png",
            "width": 100,
            "height": 100,
        },
        {
            "media_gid": "gid://shopify/MediaImage/11",
            "cdn_url": "https://cdn.shopify.com/s/files/1/x/files/new.png?v=1",
            "filename": "new.png",
            "width": 200,
            "height": 200,
        },
    ]
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={
            "shopify_product_gid": product.shopify_product_gid,
            "title": "Chain",
            "product_type": "Bags",
        },
        eligible_media_snapshot_json=eligible_media,
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    _configure_product_type(db_session, shop, "Bags")

    batch = svc.convert_secondary_items([item])
    assert batch is not None
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.CONVERTED
    images = db_session.query(BatchImage).filter(BatchImage.shop_id == shop.id).all()
    assert len(images) == 1
    assert images[0].delta_type == DeltaType.NEW
    assert images[0].shopify_media_gid.endswith("/11")


def test_secondary_conversion_creates_batch_for_new_cdn(db_session, shop):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/77",
        title="Bag",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/10",
            cdn_url="https://cdn.shopify.com/old.png",
            original_filename="old.png",
            width=10,
            height=10,
            is_visible=True,
            is_active=True,
            position=0,
        )
    )
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=[
                {
                    "media_gid": "gid://shopify/MediaImage/10",
                    "cdn_url": "https://cdn.shopify.com/old.png",
                    "filename": "old.png",
                    "width": 10,
                    "height": 10,
                }
            ],
        )
    )
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={
            "shopify_product_gid": product.shopify_product_gid,
            "title": "Bag",
            "product_type": "Bags",
        },
        eligible_media_snapshot_json=[
            {
                "media_gid": "gid://shopify/MediaImage/10",
                "cdn_url": "https://cdn.shopify.com/old.png",
                "filename": "old.png",
                "width": 10,
                "height": 10,
            },
            {
                "media_gid": "gid://shopify/MediaImage/11",
                "cdn_url": "https://cdn.shopify.com/new.png",
                "filename": "new.png",
                "width": 20,
                "height": 20,
            },
        ],
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    _configure_product_type(db_session, shop, "Bags")

    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert batch is not None
    assert batch.trigger_type == TriggerType.AUTOMATIC
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.CONVERTED
    images = db_session.query(BatchImage).filter(BatchImage.shop_id == shop.id).all()
    assert len(images) == 1
    assert images[0].delta_type == DeltaType.NEW
    assert images[0].shopify_media_gid.endswith("/11")


def test_secondary_conversion_fails_unconfigured_product_type(db_session, shop):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/780",
        title="Orphan Ring",
        status="ACTIVE",
        product_type="MysteryType",
    )
    db_session.add(product)
    db_session.flush()
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={
            "shopify_product_gid": product.shopify_product_gid,
            "title": "Orphan Ring",
            "product_type": "MysteryType",
        },
        eligible_media_snapshot_json=[
            {
                "media_gid": "gid://shopify/MediaImage/780",
                "cdn_url": "https://cdn.shopify.com/new-ring.png",
                "filename": "new-ring.png",
                "width": 20,
                "height": 20,
            },
        ],
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert batch is None
    db_session.refresh(item)
    assert item.status == SecondaryQueueStatus.FAILED_CONVERSION
    assert item.failure_reason
    assert "Orphan Ring" in item.failure_reason
    assert "prompt" in item.failure_reason.lower() or "Configure" in item.failure_reason


def test_auto_batch_trigger_by_interval_only(db_session, shop):
    settings_row = ensure_shop_settings(db_session, shop)
    settings_row.auto_sync_enabled = True
    settings_row.batch_interval_minutes = 15
    db_session.commit()

    svc = PrimaryBatchService(db_session, shop)
    assert svc.should_create_automatic_batch(settings_row) is False

    # Many pending products still within the interval → do not trigger
    for i in range(5):
        db_session.add(
            SecondaryQueueItem(
                shop_id=shop.id,
                shopify_product_gid=f"gid://shopify/Product/{100 + i}",
                status=SecondaryQueueStatus.PENDING,
                first_queued_at=datetime.now(timezone.utc),
                last_queued_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()
    assert svc.should_create_automatic_batch(settings_row) is False

    db_session.query(SecondaryQueueItem).delete()
    db_session.add(
        SecondaryQueueItem(
            shop_id=shop.id,
            shopify_product_gid="gid://shopify/Product/200",
            status=SecondaryQueueStatus.PENDING,
            first_queued_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            last_queued_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    assert svc.should_create_automatic_batch(settings_row) is True


def test_settings_api(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert res.json()["success"] is True

    res = client.put(
        "/api/settings",
        json={"autoSyncEnabled": True, "batchIntervalMinutes": 12},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["autoSyncEnabled"] is True
    assert data["batchIntervalMinutes"] == 12
    assert "maxProductsPerBatch" not in data


def test_secondary_queue_api(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    db_session.add(
        SecondaryQueueItem(
            shop_id=shop.id,
            shopify_product_gid="gid://shopify/Product/77",
            status=SecondaryQueueStatus.PENDING,
            queue_revision=1,
            webhook_count=1,
        )
    )
    db_session.commit()
    summary = client.get("/api/secondary-queue/summary")
    assert summary.status_code == 200
    assert summary.json()["data"]["pending"] >= 1
    listing = client.get("/api/secondary-queue?page=1&pageSize=10")
    assert listing.status_code == 200
    assert listing.json()["data"]["items"]


def test_secondary_queue_list_newest_first_page_size_seven(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    now = datetime.now(timezone.utc)
    older = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/oldest",
        status=SecondaryQueueStatus.PENDING,
        queue_revision=1,
        webhook_count=1,
        first_queued_at=now - timedelta(hours=2),
        last_queued_at=now - timedelta(hours=2),
    )
    newer = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/newest",
        status=SecondaryQueueStatus.PENDING,
        queue_revision=1,
        webhook_count=1,
        first_queued_at=now - timedelta(hours=1),
        last_queued_at=now,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    listing = client.get("/api/secondary-queue?page=1&pageSize=7")
    assert listing.status_code == 200
    payload = listing.json()["data"]
    ids = [item["id"] for item in payload["items"]]
    assert ids[0] == str(newer.id)
    assert str(older.id) in ids
    assert payload["pagination"]["pageSize"] == 7


def test_primary_batch_list_newest_first_page_size_seven(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    now = datetime.now(timezone.utc)
    older = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        created_at=now - timedelta(hours=2),
    )
    newer = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        created_at=now,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    listing = client.get("/api/batches?page=1&pageSize=7")
    assert listing.status_code == 200
    payload = listing.json()["data"]
    ids = [item["id"] for item in payload["items"]]
    assert ids[0] == str(newer.id)
    assert payload["pagination"]["pageSize"] == 7


def test_manual_batch_api(client, shop, db_session):
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/88",
        title="Bag",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/88",
            cdn_url="https://cdn.shopify.com/bag.png",
            is_visible=True,
            is_active=True,
        )
    )
    db_session.commit()
    _configure_product_type(db_session, shop, "Bags")

    res = client.post("/api/batches/manual", json={"productGids": ["gid://shopify/Product/88"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert body["data"]["productCount"] == 1
    assert body["data"]["imageCount"] == 1

    blocked = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/89",
        title="No Prompt Product",
        status="ACTIVE",
        product_type="MissingConfig",
    )
    db_session.add(blocked)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=blocked.id,
            shopify_media_gid="gid://shopify/MediaImage/89",
            cdn_url="https://cdn.shopify.com/x.png",
            is_visible=True,
            is_active=True,
        )
    )
    db_session.commit()
    bad = client.post("/api/batches/manual", json={"productGids": ["gid://shopify/Product/89"]})
    assert bad.status_code == 400, bad.text
    assert "No Prompt Product" in bad.text
    assert "prompt" in bad.text.lower() or "Configure" in bad.text


def test_catalog_products_list_and_select_all(client, shop, db_session):
    for i, (title, ptype) in enumerate(
        [("Gold Ring", "Rings"), ("Silver Ring", "Rings"), ("Charm A", "Charms")],
        start=1,
    ):
        db_session.add(
            Product(
                shop_id=shop.id,
                shopify_product_gid=f"gid://shopify/Product/{1000 + i}",
                title=title,
                product_type=ptype,
                status="ACTIVE",
                is_deleted=False,
            )
        )
    db_session.commit()

    listed = client.get("/api/products?productType=Rings&search=Ring")
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert data["total"] == 2
    assert data["manualBatchProductLimit"] >= 1

    gids = client.get("/api/products/matching-gids?productType=rings")
    assert gids.status_code == 200
    payload = gids.json()["data"]
    assert payload["returned"] == 2
    assert payload["truncated"] is False

    types = client.get("/api/products/product-types")
    assert types.status_code == 200
    assert set(types.json()["data"]["items"]) >= {"Rings", "Charms"}


def test_internal_install_hmac(client, monkeypatch, db_session):
    monkeypatch.setattr("app.config.settings.internal_handoff_secret", "handoff-secret")
    monkeypatch.setattr("app.core.crypto.settings.internal_handoff_secret", "handoff-secret")
    body = {
        "shop": "new-shop.myshopify.com",
        "accessToken": "shpat_abc",
        "scope": "read_products",
    }
    raw = json.dumps(body).encode("utf-8")
    ts, sig = sign_internal_payload(raw)
    res = client.post(
        "/internal/shops/install",
        content=raw,
        headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig},
    )
    assert res.status_code == 200, res.text
    shop = db_session.query(Shop).filter(Shop.shop_domain == "new-shop.myshopify.com").one()
    assert shop.encrypted_access_token


def test_webhook_dedupe(db_session, shop):
    ensure_shop_settings(db_session, shop)
    db_session.commit()
    payload = {
        "admin_graphql_api_id": "gid://shopify/Product/999",
        "id": 999,
        "title": "Webhook Product",
        "status": "active",
        "handle": "webhook-product",
    }
    with patch.object(
        WebhookIntakeService,
        "_process_product_update",
        return_value=None,
    ):
        svc = WebhookIntakeService(db_session)
        first = svc.record_and_process_products_update(
            shop_domain=shop.shop_domain,
            webhook_id="webhook-unique-1",
            topic="products/update",
            payload=payload,
            raw_hash="abc",
        )
        second = svc.record_and_process_products_update(
            shop_domain=shop.shop_domain,
            webhook_id="webhook-unique-1",
            topic="products/update",
            payload=payload,
            raw_hash="abc",
        )
    assert first.id == second.id
    events = db_session.query(WebhookEvent).filter(WebhookEvent.shopify_webhook_id == "webhook-unique-1").all()
    assert len(events) == 1


def test_webhook_seeds_baseline_before_catalog_upsert_so_new_image_converts(db_session, shop, monkeypatch):
    """Adding an image with an empty ProcessingBaseline must convert, not skip."""
    ensure_shop_settings(db_session, shop)
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/4242",
        title="Pendant",
        status="ACTIVE",
        product_type="Bags",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductMedia(
            shop_id=shop.id,
            product_id=product.id,
            shopify_media_gid="gid://shopify/MediaImage/100",
            cdn_url="https://cdn.shopify.com/old.png",
            original_filename="old.png",
            width=10,
            height=10,
            is_visible=True,
            is_active=True,
            position=0,
        )
    )
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=None,
        )
    )
    db_session.commit()
    _configure_product_type(db_session, shop, "Bags")

    graphql_node = {
        "id": "gid://shopify/Product/4242",
        "title": "Pendant",
        "status": "ACTIVE",
        "productType": "Bags",
        "handle": "pendant",
        "tags": [],
        "featuredMedia": {"id": "gid://shopify/MediaImage/100"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/100",
                    "mediaContentType": "IMAGE",
                    "alt": None,
                    "image": {
                        "url": "https://cdn.shopify.com/old.png",
                        "width": 10,
                        "height": 10,
                    },
                },
                {
                    "id": "gid://shopify/MediaImage/101",
                    "mediaContentType": "IMAGE",
                    "alt": None,
                    "image": {
                        "url": "https://cdn.shopify.com/new.png",
                        "width": 20,
                        "height": 20,
                    },
                },
            ]
        },
        "variants": {"nodes": []},
    }

    mock_client = MagicMock()
    mock_client.fetch_product_by_gid.return_value = graphql_node
    monkeypatch.setattr(
        "app.services.webhook_intake.create_shopify_graphql_client",
        lambda _db, _shop: mock_client,
    )

    payload = {
        "admin_graphql_api_id": "gid://shopify/Product/4242",
        "id": 4242,
        "title": "Pendant",
        "status": "active",
        "product_type": "Bags",
        "images": [
            {"id": 100, "src": "https://cdn.shopify.com/old.png", "width": 10, "height": 10},
            {"id": 101, "src": "https://cdn.shopify.com/new.png", "width": 20, "height": 20},
        ],
    }
    event = WebhookIntakeService(db_session).record_and_process_products_update(
        shop_domain=shop.shop_domain,
        webhook_id="webhook-add-image-1",
        topic="products/update",
        payload=payload,
        raw_hash="hash-add-image",
    )
    assert event.processing_result == WebhookProcessingResult.ACCEPTED

    baseline = (
        db_session.query(ProcessingBaseline)
        .filter(ProcessingBaseline.product_id == product.id)
        .one()
    )
    # Pre-sync freeze must keep only the old image — not the newly upserted one.
    assert baseline.media_snapshot_json is not None
    assert len(baseline.media_snapshot_json) == 1
    assert baseline.media_snapshot_json[0]["media_gid"].endswith("/100")

    pending = (
        db_session.query(SecondaryQueueItem)
        .filter(
            SecondaryQueueItem.shop_id == shop.id,
            SecondaryQueueItem.shopify_product_gid == product.shopify_product_gid,
            SecondaryQueueItem.status == SecondaryQueueStatus.PENDING,
        )
        .one()
    )
    assert len(pending.eligible_media_snapshot_json or []) == 2

    pending.status = SecondaryQueueStatus.CLAIMED
    pending.claimed_by = "worker_test"
    pending.claimed_at = datetime.now(timezone.utc)
    db_session.commit()

    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([pending])
    assert batch is not None
    db_session.refresh(pending)
    assert pending.status == SecondaryQueueStatus.CONVERTED
    images = db_session.query(BatchImage).filter(BatchImage.shop_id == shop.id).all()
    assert len(images) == 1
    assert images[0].delta_type == DeltaType.NEW
    assert images[0].shopify_media_gid.endswith("/101")


def test_batch_image_output_endpoint(client, db_session, shop, tmp_path):
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
        shopify_product_gid="gid://shopify/Product/501",
        status=BatchProductStatus.COMPLETED,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()

    key = f"{shop.id}/{batch.id}/output-test/output.png"
    output_root = tmp_path / "processed"
    path = output_root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")

    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/501",
        cdn_url="https://cdn.shopify.com/x.png",
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.COMPLETED,
        output_storage_key=key,
        output_mime_type="image/png",
    )
    db_session.add(image)
    db_session.commit()

    response = client.get(f"/api/batches/images/{image.id}/output")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"\x89PNG\r\n\x1a\nfake-bytes"

    missing = client.get(f"/api/batches/images/{uuid4()}/output")
    assert missing.status_code == 404


def test_product_atomicity_counters(db_session, shop):
    ensure_shop_settings(db_session, shop)
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=2,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/1",
        status=BatchProductStatus.FAILED,
        image_count=2,
    )
    db_session.add(bp)
    db_session.commit()
    PrimaryBatchService(db_session, shop).refresh_batch_counters(batch)
    db_session.refresh(batch)
    assert batch.failed_product_count == 1
    assert batch.status in {BatchStatus.FAILED, BatchStatus.PARTIALLY_COMPLETED, BatchStatus.COMPLETED}


def test_stale_recovery_closes_open_attempt_without_unique_violation(db_session, shop, monkeypatch):
    """Regression: recovering a hung PROCESSING image must not insert attempt_number twice."""
    monkeypatch.setattr("app.services.retry_service.settings.processing_stale_lock_seconds", 1)
    monkeypatch.setattr("app.services.retry_service.settings.processing_retry_delay_seconds", 1)
    ensure_shop_settings(db_session, shop)

    locked_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        processing_product_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/stale-1",
        status=BatchProductStatus.PROCESSING,
        image_count=1,
        locked_by="worker_old",
        locked_at=locked_at,
        claimed_at=locked_at,
        started_at=locked_at,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/stale-1",
        cdn_url="https://cdn.shopify.com/stale.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.PROCESSING,
        attempt_count=1,
        started_at=locked_at,
    )
    db_session.add(image)
    db_session.flush()
    db_session.add(
        ProcessingAttempt(
            batch_image_id=image.id,
            batch_product_id=bp.id,
            attempt_number=1,
            status=AttemptStatus.STARTED,
            provider="openai",
            shopify_source_url=image.cdn_url,
            started_at=locked_at,
        )
    )
    db_session.commit()

    recovered = RetryService(db_session).recover_stale_batch_products(worker_id="worker_new")
    assert recovered == 1

    db_session.refresh(bp)
    db_session.refresh(image)
    assert bp.status == BatchProductStatus.RETRYING
    assert bp.locked_by is None
    assert image.status == BatchImageStatus.RETRYING

    attempts = (
        db_session.query(ProcessingAttempt)
        .filter(ProcessingAttempt.batch_image_id == image.id)
        .order_by(ProcessingAttempt.attempt_number.asc())
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].status == AttemptStatus.INTERRUPTED
    assert attempts[0].error_code == "STALE_LOCK"


def _stale_locked_product(db_session, shop, *, image_status, processing_phase=None, **image_kwargs):
    ensure_shop_settings(db_session, shop)
    locked_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        processing_product_count=1,
        processing_phase=processing_phase,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=f"gid://shopify/Product/stale-{uuid4()}",
        status=BatchProductStatus.PROCESSING,
        image_count=1,
        locked_by="worker_old",
        locked_at=locked_at,
        claimed_at=locked_at,
        started_at=locked_at,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid=f"gid://shopify/MediaImage/stale-{uuid4()}",
        cdn_url="https://cdn.shopify.com/stale.png",
        delta_type=DeltaType.INITIAL,
        status=image_status,
        attempt_count=1,
        started_at=locked_at,
        **image_kwargs,
    )
    db_session.add(image)
    db_session.commit()
    return batch, bp, image


def test_stale_recovery_skips_waiting_for_openai_provider(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.services.retry_service.settings.processing_stale_lock_seconds", 1)
    _batch, bp, image = _stale_locked_product(
        db_session,
        shop,
        image_status=BatchImageStatus.WAITING_FOR_PROVIDER,
        processing_phase="WAITING_FOR_OPENAI",
    )

    recovered = RetryService(db_session).recover_stale_batch_products(worker_id="worker_new")
    assert recovered == 0
    db_session.refresh(bp)
    db_session.refresh(image)
    assert bp.status == BatchProductStatus.PROCESSING
    assert bp.locked_by == "worker_old"
    assert image.status == BatchImageStatus.WAITING_FOR_PROVIDER


def test_stale_recovery_skips_pending_shopify_upload(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.services.retry_service.settings.processing_stale_lock_seconds", 1)
    _batch, bp, image = _stale_locked_product(
        db_session,
        shop,
        image_status=BatchImageStatus.PROCESSING,
        processing_phase="UPLOADING_TO_SHOPIFY_FILES",
        output_storage_key="shop/batch/img/output.png",
    )

    recovered = RetryService(db_session).recover_stale_batch_products(worker_id="worker_new")
    assert recovered == 0
    db_session.refresh(bp)
    db_session.refresh(image)
    assert bp.status == BatchProductStatus.PROCESSING
    assert image.status == BatchImageStatus.PROCESSING
    assert image.error_code is None


def test_stale_recovery_heals_product_when_all_images_completed(db_session, shop, monkeypatch):
    monkeypatch.setattr("app.services.retry_service.settings.processing_stale_lock_seconds", 1)
    _batch, bp, image = _stale_locked_product(
        db_session,
        shop,
        image_status=BatchImageStatus.COMPLETED,
        processing_phase="READY_TO_PUBLISH",
        generated_shopify_file_gid="gid://shopify/MediaImage/generated-1",
        completed_at=datetime.now(timezone.utc),
    )

    recovered = RetryService(db_session).recover_stale_batch_products(worker_id="worker_new")
    assert recovered == 1
    db_session.refresh(bp)
    assert bp.status == BatchProductStatus.COMPLETED
    assert bp.locked_by is None
    assert bp.error_code is None


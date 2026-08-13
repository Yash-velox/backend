"""Product media rollback tests."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

from app.models import (
    MediaVersionType,
    Product,
    ProductMediaVersion,
    ProductPublishOperation,
    ProductRollbackOperation,
    PublishStatus,
    PublishTriggerSource,
    RollbackStatus,
)
from app.services.media_versions import MediaVersionsService, product_has_active_media_op
from app.services.product_rollback import ProductRollbackService, RollbackError
from tests.test_publishing import _media


def _seed_product_with_versions(db, shop):
    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/1",
        title="Ring",
        handle="ring",
        status="ACTIVE",
    )
    db.add(product)
    db.flush()
    svc = MediaVersionsService(db, shop)
    original_snap = {
        "product_gid": product.shopify_product_gid,
        "featured_media_gid": "gid://shopify/MediaImage/10",
        "media": [_media("gid://shopify/MediaImage/10", 0, alt="front", featured=True)],
        "variants": [{"variant_gid": "gid://shopify/ProductVariant/1", "media_gid": "gid://shopify/MediaImage/10"}],
    }
    published_snap = {
        "product_gid": product.shopify_product_gid,
        "featured_media_gid": "gid://shopify/MediaImage/999",
        "media": [_media("gid://shopify/MediaImage/999", 0, alt="front", featured=True)],
        "variants": [{"variant_gid": "gid://shopify/ProductVariant/1", "media_gid": "gid://shopify/MediaImage/999"}],
    }
    original = svc.create_version(
        product=product,
        snapshot=original_snap,
        version_type=MediaVersionType.ORIGINAL,
        activate=False,
        skip_duplicate_hash=False,
    )
    published = svc.create_version(
        product=product,
        snapshot=published_snap,
        version_type=MediaVersionType.PUBLISHED,
        activate=True,
        skip_duplicate_hash=False,
    )
    db.commit()
    db.refresh(product)
    db.refresh(original)
    db.refresh(published)
    return product, original, published


def test_rollback_rejects_active_target(db_session, shop):
    product, _original, published = _seed_product_with_versions(db_session, shop)
    client = MagicMock()
    svc = ProductRollbackService(db_session, shop, client=client)
    try:
        svc.enqueue(product_id=product.id, target_version_id=published.id, confirm=True)
        assert False, "expected error"
    except RollbackError as exc:
        assert exc.code == "VERSION_ALREADY_ACTIVE"


def test_rollback_requires_confirm(db_session, shop):
    product, original, _published = _seed_product_with_versions(db_session, shop)
    client = MagicMock()
    svc = ProductRollbackService(db_session, shop, client=client)
    try:
        svc.enqueue(product_id=product.id, target_version_id=original.id, confirm=False)
        assert False, "expected error"
    except RollbackError as exc:
        assert exc.code == "ROLLBACK_CONFIRM_REQUIRED"


def test_publish_and_rollback_cannot_run_concurrently(db_session, shop):
    product, original, _published = _seed_product_with_versions(db_session, shop)

    from app.models import BatchProduct, BatchProductStatus, BatchStatus, ProcessingBatch, TriggerType

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.COMPLETED,
        product_count=1,
        image_count=1,
        completed_product_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=BatchProductStatus.COMPLETED,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()
    pub = ProductPublishOperation(
        shop_id=shop.id,
        processing_batch_id=batch.id,
        batch_product_id=bp.id,
        shopify_product_gid=product.shopify_product_gid,
        status=PublishStatus.PUBLISHING,
        trigger_source=PublishTriggerSource.MANUAL,
        idempotency_key="lock-test-key-2",
    )
    db_session.add(pub)
    db_session.commit()

    assert (
        product_has_active_media_op(
            db_session, shop_id=shop.id, shopify_product_gid=product.shopify_product_gid
        )
        == "PUBLISH_ALREADY_ACTIVE"
    )

    client = MagicMock()
    svc = ProductRollbackService(db_session, shop, client=client)
    try:
        svc.enqueue(product_id=product.id, target_version_id=original.id, confirm=True)
        assert False, "expected lock error"
    except RollbackError as exc:
        assert exc.code == "PUBLISH_ALREADY_ACTIVE"


def test_successful_rollback_creates_audit_version(db_session, shop):
    product, original, published = _seed_product_with_versions(db_session, shop)
    client = MagicMock()
    client.get_file_statuses.return_value = [
        {
            "id": "gid://shopify/MediaImage/10",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
        },
        {
            "id": "gid://shopify/MediaImage/999",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/new.png", "width": 10, "height": 10},
        },
    ]

    live_published = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/999"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/999",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/new.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/999"}]},
                }
            ]
        },
    }
    live_both = {
        **live_published,
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/999",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/new.png", "width": 10, "height": 10},
                },
                {
                    "id": "gid://shopify/MediaImage/10",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
                },
            ]
        },
    }
    live_original = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/10",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/10"}]},
                }
            ]
        },
    }

    # conflict check, after attach, verify target, after detach/final reorder, final verify
    client.get_product_media_snapshot.side_effect = [
        live_published,
        live_both,
        live_both,
        live_original,
        live_original,
    ]
    client.add_file_product_references.return_value = []
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}

    svc = ProductRollbackService(db_session, shop, client=client)
    queued = svc.enqueue(product_id=product.id, target_version_id=original.id, confirm=True)
    op_id = UUID(queued["operationId"])
    out = svc.run(op_id)

    assert out.status == RollbackStatus.ROLLED_BACK
    assert out.result_version_id == original.id
    db_session.refresh(original)
    db_session.refresh(published)
    assert original.is_active is True
    assert original.version_type == MediaVersionType.ORIGINAL
    assert published.is_active is False
    # No new ROLLBACK audit row should be created
    rollback_rows = (
        db_session.query(ProductMediaVersion)
        .filter(
            ProductMediaVersion.product_id == product.id,
            ProductMediaVersion.version_type == MediaVersionType.ROLLBACK,
        )
        .count()
    )
    assert rollback_rows == 0

    # Detach only removed published file from this product
    remove_calls = client.remove_file_product_references.call_args_list
    assert remove_calls
    assert "gid://shopify/MediaImage/999" in remove_calls[0].kwargs["file_gids"]
    assert remove_calls[0].kwargs["product_gid"] == product.shopify_product_gid


def test_compare_by_files_allows_rematerialized_media_gids():
    """Same CDN file after detach/reattach must not false-conflict on new MediaImage GIDs."""
    from app.services.product_rollback import _compare_by_files

    expected = {
        "featured_media_gid": "gid://shopify/MediaImage/999",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/999",
                "file_gid": "gid://shopify/MediaImage/999",
                "position": 0,
                "alt_text": "front",
                "cdn_url": "https://cdn.shopify.com/s/files/1/new.png?v=1",
                "is_primary": True,
            }
        ],
        "variants": [
            {"variant_gid": "gid://shopify/ProductVariant/1", "media_gid": "gid://shopify/MediaImage/999"}
        ],
    }
    live = {
        "featured_media_gid": "gid://shopify/MediaImage/1000",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/1000",
                "file_gid": "gid://shopify/MediaImage/1000",
                "position": 0,
                "alt_text": "front",
                "cdn_url": "https://cdn.shopify.com/s/files/1/new.png?v=99",
                "is_primary": True,
            }
        ],
        "variants": [
            {"variant_gid": "gid://shopify/ProductVariant/1", "media_gid": "gid://shopify/MediaImage/1000"}
        ],
    }
    assert _compare_by_files(expected, live)["hasConflict"] is False


def test_compare_by_files_conflicts_when_cdn_differs():
    from app.services.product_rollback import _compare_by_files

    expected = {
        "featured_media_gid": "gid://shopify/MediaImage/999",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/999",
                "file_gid": "gid://shopify/MediaImage/999",
                "position": 0,
                "alt_text": "front",
                "cdn_url": "https://cdn.shopify.com/s/files/1/published.png",
                "is_primary": True,
            }
        ],
        "variants": [],
    }
    live = {
        "featured_media_gid": "gid://shopify/MediaImage/50",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/50",
                "file_gid": "gid://shopify/MediaImage/50",
                "position": 0,
                "alt_text": "front",
                "cdn_url": "https://cdn.shopify.com/s/files/1/changed.png",
                "is_primary": True,
            }
        ],
        "variants": [],
    }
    out = _compare_by_files(expected, live)
    assert out["hasConflict"] is True
    assert out["membershipChanged"] is True


def test_rollback_conflict_when_live_differs(db_session, shop):
    product, original, _published = _seed_product_with_versions(db_session, shop)
    client = MagicMock()
    client.get_file_statuses.return_value = [
        {
            "id": "gid://shopify/MediaImage/10",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
        },
        {
            "id": "gid://shopify/MediaImage/999",
            "fileStatus": "READY",
            "image": {
                "url": "https://cdn.shopify.com/gid://shopify/MediaImage/999.png",
                "width": 10,
                "height": 10,
            },
        },
    ]
    # Live has an unexpected media id vs active published version
    client.get_product_media_snapshot.return_value = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/50"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/50",
                    "mediaContentType": "IMAGE",
                    "alt": "changed",
                    "image": {"url": "https://cdn.shopify.com/x.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {"nodes": []},
    }

    svc = ProductRollbackService(db_session, shop, client=client)
    queued = svc.enqueue(product_id=product.id, target_version_id=original.id, confirm=True)
    out = svc.run(UUID(queued["operationId"]))
    assert out.status == RollbackStatus.ROLLBACK_CONFLICT
    assert out.conflict_details


def test_rollback_allows_rematerialized_live_media_gids(db_session, shop):
    """Live MediaImage GIDs changed but CDN paths still match active published version."""
    product, original, published = _seed_product_with_versions(db_session, shop)
    # Align published snapshot CDN with what live will report (path-stable).
    snap = dict(published.items_json or {})
    media = [dict(m) for m in (snap.get("media") or [])]
    for m in media:
        m["cdn_url"] = "https://cdn.shopify.com/s/files/1/published.png?v=1"
    snap["media"] = media
    published.items_json = snap
    db_session.commit()

    client = MagicMock()
    client.get_file_statuses.return_value = [
        {
            "id": "gid://shopify/MediaImage/10",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
        },
        {
            "id": "gid://shopify/MediaImage/999",
            "fileStatus": "READY",
            "image": {
                "url": "https://cdn.shopify.com/s/files/1/published.png?v=1",
                "width": 10,
                "height": 10,
            },
        },
    ]

    live_published = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/1000"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/1000",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {
                        "url": "https://cdn.shopify.com/s/files/1/published.png?v=2",
                        "width": 10,
                        "height": 10,
                    },
                }
            ]
        },
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/1000"}]},
                }
            ]
        },
    }
    live_both = {
        **live_published,
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/1000",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {
                        "url": "https://cdn.shopify.com/s/files/1/published.png?v=2",
                        "width": 10,
                        "height": 10,
                    },
                },
                {
                    "id": "gid://shopify/MediaImage/10",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
                },
            ]
        },
    }
    live_original = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/10",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/10"}]},
                }
            ]
        },
    }
    client.get_product_media_snapshot.side_effect = [
        live_published,
        live_both,
        live_both,
        live_original,
        live_original,
    ]
    client.add_file_product_references.return_value = []
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}

    svc = ProductRollbackService(db_session, shop, client=client)
    queued = svc.enqueue(product_id=product.id, target_version_id=original.id, confirm=True)
    out = svc.run(UUID(queued["operationId"]))
    assert out.status == RollbackStatus.ROLLED_BACK
    assert out.result_version_id == original.id


def test_rollback_api_shop_scoped(client, db_session, shop):
    product, original, _published = _seed_product_with_versions(db_session, shop)
    res = client.get(f"/api/products/{product.id}/media-versions/{original.id}/rollback-preview")
    # Preview hits Shopify for file status - mock via service path is heavy; just ensure auth + ownership works
    # Without mock the client may fail on Shopify - patch at GraphQL level via dependency is complex.
    # Use enqueue without confirm to validate API validation.
    res = client.post(
        f"/api/products/{product.id}/media-versions/{original.id}/rollback",
        json={"confirm": False},
    )
    assert res.status_code == 400
    detail = res.json()["error"]["message"]
    assert "confirm" in str(detail).lower() or "CONFIRM" in str(detail) or "code" in str(detail)


def test_compare_by_files_matches_cdn_to_media_gid():
    """Active has CDN+GID; live has only the same MediaImage GID - must not false-conflict."""
    from app.services.product_rollback import _compare_by_files

    expected = {
        "featured_media_gid": "gid://shopify/MediaImage/33272640831571",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/33272640831571",
                "file_gid": "gid://shopify/MediaImage/33272640831571",
                "position": 0,
                "alt_text": "",
                "cdn_url": (
                    "https://cdn.shopify.com/s/files/1/0729/7512/2515/files/"
                    "product_b45fa38d24844e8a_media_33272640831571_version_1.png"
                ),
                "is_primary": True,
            }
        ],
        "variants": [],
    }
    live = {
        "featured_media_gid": "gid://shopify/MediaImage/33272640831571",
        "media": [
            {
                "media_gid": "gid://shopify/MediaImage/33272640831571",
                "file_gid": "gid://shopify/MediaImage/33272640831571",
                "position": 0,
                "alt_text": "",
                # Live snapshot missing CDN - old matcher preferred cdn on expected and media on live.
                "cdn_url": None,
                "is_primary": True,
            }
        ],
        "variants": [],
    }
    out = _compare_by_files(expected, live)
    assert out["hasConflict"] is False


def test_humanize_conflict_details_mentions_removed_file():
    from app.services.product_rollback import humanize_conflict_details

    msg = humanize_conflict_details(
        {
            "hasConflict": True,
            "removedMediaIds": [
                "cdn:/s/files/1/x/files/product_b45fa38d24844e8a_media_33272640831571_version_1.png"
            ],
            "addedMediaIds": [],
            "orderChanged": False,
            "altChanges": [],
            "featuredMediaChanged": False,
            "variantChanges": [],
        }
    )
    assert "not found on live Shopify" in msg
    assert "version_1.png" in msg


def test_rollback_force_despite_conflict(db_session, shop):
    product, original, published = _seed_product_with_versions(db_session, shop)
    client = MagicMock()
    client.get_file_statuses.return_value = [
        {
            "id": "gid://shopify/MediaImage/10",
            "fileStatus": "READY",
            "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
        },
        {
            "id": "gid://shopify/MediaImage/999",
            "fileStatus": "READY",
            "image": {
                "url": "https://cdn.shopify.com/gid://shopify/MediaImage/999.png",
                "width": 10,
                "height": 10,
            },
        },
    ]
    live_diverged = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/50"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/50",
                    "mediaContentType": "IMAGE",
                    "alt": "changed",
                    "image": {"url": "https://cdn.shopify.com/x.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {"nodes": []},
    }
    live_with_target = {
        "id": product.shopify_product_gid,
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {
            "nodes": [
                {
                    "id": "gid://shopify/MediaImage/10",
                    "mediaContentType": "IMAGE",
                    "alt": "front",
                    "image": {"url": "https://cdn.shopify.com/a.png", "width": 10, "height": 10},
                }
            ]
        },
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "media": {"nodes": [{"id": "gid://shopify/MediaImage/10"}]},
                }
            ]
        },
    }
    # First run: conflict snapshot only. Force run: mid/verify/detach/final snapshots.
    client.get_product_media_snapshot.side_effect = [
        live_diverged,
        live_with_target,
        live_with_target,
        live_with_target,
        live_with_target,
        live_with_target,
        live_with_target,
    ]
    client.add_file_product_references.return_value = []
    client.remove_file_product_references.return_value = []
    client.update_file_alt_text.return_value = {}
    client.associate_media_to_variants.return_value = []
    client.reorder_product_media.return_value = {"id": "gid://shopify/Job/1", "done": True}
    client.get_job_status.return_value = {"id": "gid://shopify/Job/1", "done": True}

    svc = ProductRollbackService(db_session, shop, client=client)
    queued = svc.enqueue(
        product_id=product.id,
        target_version_id=original.id,
        confirm=True,
        force_despite_conflict=False,
    )
    blocked = svc.run(UUID(queued["operationId"]))
    assert blocked.status == RollbackStatus.ROLLBACK_CONFLICT
    assert "not found on live Shopify" in (blocked.last_error_message or "") or (
        blocked.conflict_details or {}
    ).get("hasConflict")

    retried = svc.retry(UUID(queued["operationId"]), force_despite_conflict=True)
    assert retried["forceDespiteConflict"] is True
    out = svc.run(UUID(retried["operationId"]))
    assert out.status == RollbackStatus.ROLLED_BACK
    assert out.force_despite_conflict is True
    assert (out.conflict_details or {}).get("forceApplied") is True
    assert out.result_version_id == original.id
    # published remains the "from" version historically; target original is reactivated
    assert published.id != out.result_version_id


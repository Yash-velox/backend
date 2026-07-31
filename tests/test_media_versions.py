"""Product media version history tests."""

from __future__ import annotations

from app.models import MediaVersionType, Product, ProductMediaVersion, PublishStatus
from app.services.media_versions import MediaVersionsService
from app.services.product_publisher import ProductPublisher
from app.services.publish_trigger import PublishTriggerService
from tests.test_publishing import _media, _seed_completed_batch

from unittest.mock import MagicMock, patch
from uuid import UUID


def _seed_catalog_product(db, shop, *, gid="gid://shopify/Product/1", title="Ring"):
    product = Product(
        shop_id=shop.id,
        shopify_product_gid=gid,
        title=title,
        handle="ring",
        status="ACTIVE",
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def test_record_publish_creates_original_and_published(db_session, shop, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.processing_output_directory", str(tmp_path / "processed"))
    catalog = _seed_catalog_product(db_session, shop)
    batch, batch_product, _ = _seed_completed_batch(db_session, shop, tmp_path)
    batch_product.product_id = catalog.id
    db_session.commit()

    svc = PublishTriggerService(db_session, shop)
    result = svc.enqueue_product(batch_product.id)
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
    snap_original = {
        "id": "gid://shopify/Product/1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "featuredMedia": {"id": "gid://shopify/MediaImage/10"},
        "media": {"nodes": [original_media]},
        "variants": {"nodes": []},
    }
    snap_both = {**snap_original, "media": {"nodes": [original_media, new_media]}}
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
    versions = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == catalog.id)
        .order_by(ProductMediaVersion.version_number.asc())
        .all()
    )
    assert len(versions) == 2
    assert versions[0].version_type == MediaVersionType.ORIGINAL
    assert versions[0].is_active is False
    assert versions[1].version_type == MediaVersionType.PUBLISHED
    assert versions[1].is_active is True
    assert len((versions[1].items_json or {}).get("media") or []) == 1


def test_one_active_version_invariant(db_session, shop):
    catalog = _seed_catalog_product(db_session, shop)
    svc = MediaVersionsService(db_session, shop)
    snap1 = {
        "product_gid": catalog.shopify_product_gid,
        "featured_media_gid": "gid://shopify/MediaImage/1",
        "media": [_media("gid://shopify/MediaImage/1", 0, featured=True)],
        "variants": [],
    }
    snap2 = {
        "product_gid": catalog.shopify_product_gid,
        "featured_media_gid": "gid://shopify/MediaImage/2",
        "media": [_media("gid://shopify/MediaImage/2", 0, featured=True)],
        "variants": [],
    }
    v1 = svc.create_version(
        product=catalog,
        snapshot=snap1,
        version_type=MediaVersionType.ORIGINAL,
        activate=True,
        skip_duplicate_hash=False,
    )
    v2 = svc.create_version(
        product=catalog,
        snapshot=snap2,
        version_type=MediaVersionType.PUBLISHED,
        activate=True,
        skip_duplicate_hash=False,
    )
    db_session.commit()
    db_session.refresh(v1)
    db_session.refresh(v2)
    assert v1.is_active is False
    assert v2.is_active is True
    active_count = (
        db_session.query(ProductMediaVersion)
        .filter(ProductMediaVersion.product_id == catalog.id, ProductMediaVersion.is_active.is_(True))
        .count()
    )
    assert active_count == 1


def test_search_products_with_versions(client, db_session, shop):
    catalog = _seed_catalog_product(db_session, shop, title="Gold Ring")
    other = _seed_catalog_product(db_session, shop, gid="gid://shopify/Product/2", title="No Versions")
    svc = MediaVersionsService(db_session, shop)
    svc.create_version(
        product=catalog,
        snapshot={
            "product_gid": catalog.shopify_product_gid,
            "featured_media_gid": "gid://shopify/MediaImage/1",
            "media": [_media("gid://shopify/MediaImage/1", 0, featured=True)],
            "variants": [],
        },
        version_type=MediaVersionType.PUBLISHED,
        activate=True,
        skip_duplicate_hash=False,
    )
    db_session.commit()

    res = client.get("/api/products/media-versions?search=Gold")
    assert res.status_code == 200
    items = res.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["productId"] == str(catalog.id)
    assert str(other.id) not in {i["productId"] for i in items}


def test_shop_isolation_versions(client, db_session, shop, SessionLocal):
    from app.core.crypto import encrypt_token
    from app.models import Shop

    other = Shop(shop_domain="other.myshopify.com", encrypted_access_token=encrypt_token("x"))
    db_session.add(other)
    db_session.commit()
    catalog = Product(
        shop_id=other.id,
        shopify_product_gid="gid://shopify/Product/99",
        title="Other",
    )
    db_session.add(catalog)
    db_session.commit()
    version = ProductMediaVersion(
        shop_id=other.id,
        product_id=catalog.id,
        shopify_product_gid=catalog.shopify_product_gid,
        version_number=1,
        version_type=MediaVersionType.ORIGINAL,
        is_active=True,
        rollback_eligible=True,
        snapshot_hash="abc",
        items_json={"media": []},
    )
    db_session.add(version)
    db_session.commit()

    res = client.get(f"/api/products/{catalog.id}/media-versions")
    assert res.status_code == 404

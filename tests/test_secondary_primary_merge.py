"""Secondary → Primary merge/fill conversion tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import settings
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
    SecondaryQueueItem,
    SecondaryQueueStatus,
    TriggerType,
)
from app.services.primary_batch import PrimaryBatchService
from app.services.prompt_configuration import PromptConfigurationService
from app.services.prompt_product_types import PromptProductTypeService


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


def _media(gid_suffix: str, name: str, *, w: int = 10, h: int = 10) -> dict:
    return {
        "media_gid": f"gid://shopify/MediaImage/{gid_suffix}",
        "cdn_url": f"https://cdn.shopify.com/{name}",
        "filename": name,
        "width": w,
        "height": h,
    }


def _make_product(db_session, shop, *, gid_num: int, title: str = "Item", product_type: str = "Bags") -> Product:
    product = Product(
        shop_id=shop.id,
        shopify_product_gid=f"gid://shopify/Product/{gid_num}",
        title=title,
        status="ACTIVE",
        product_type=product_type,
    )
    db_session.add(product)
    db_session.flush()
    return product


def _claimed_secondary(
    db_session,
    shop,
    product: Product,
    media: list[dict],
    *,
    revision: str = "v1",
) -> SecondaryQueueItem:
    item = SecondaryQueueItem(
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=SecondaryQueueStatus.CLAIMED,
        eligible_product_snapshot_json={
            "shopify_product_gid": product.shopify_product_gid,
            "title": product.title,
            "product_type": product.product_type,
            "revision": revision,
        },
        eligible_media_snapshot_json=media,
        claimed_by="worker_test",
        claimed_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()
    return item


def _seed_empty_baseline(db_session, shop, product: Product) -> None:
    db_session.add(
        ProcessingBaseline(
            shop_id=shop.id,
            product_id=product.id,
            media_snapshot_json=[],
            product_snapshot_json={"shopify_product_gid": product.shopify_product_gid},
            evaluated_at=datetime.now(timezone.utc),
        )
    )
    db_session.flush()


def _queued_products(db_session, shop, product_gid: str) -> list[BatchProduct]:
    return (
        db_session.query(BatchProduct)
        .join(ProcessingBatch, BatchProduct.batch_id == ProcessingBatch.id)
        .filter(
            BatchProduct.shop_id == shop.id,
            BatchProduct.shopify_product_gid == product_gid,
            BatchProduct.status == BatchProductStatus.QUEUED,
            ProcessingBatch.trigger_type == TriggerType.AUTOMATIC,
        )
        .all()
    )


def test_queued_product_refresh_same_batch(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")
    product = _make_product(db_session, shop, gid_num=9101, title="A")
    _seed_empty_baseline(db_session, shop, product)

    item1 = _claimed_secondary(db_session, shop, product, [_media("a1", "a.png")], revision="v1")
    db_session.commit()
    batch = PrimaryBatchService(db_session, shop).convert_secondary_items([item1])
    assert batch is not None
    bp = _queued_products(db_session, shop, product.shopify_product_gid)
    assert len(bp) == 1
    original_id = bp[0].id
    assert bp[0].image_count == 1

    item2 = _claimed_secondary(
        db_session,
        shop,
        product,
        [_media("a1", "a.png"), _media("b1", "b.png")],
        revision="v2",
    )
    db_session.commit()
    batch2 = PrimaryBatchService(db_session, shop).convert_secondary_items([item2])
    assert batch2 is not None
    assert batch2.id == batch.id

    queued = _queued_products(db_session, shop, product.shopify_product_gid)
    assert len(queued) == 1
    assert queued[0].id == original_id
    assert queued[0].product_snapshot_json.get("revision") == "v2"
    images = (
        db_session.query(BatchImage)
        .filter(BatchImage.batch_product_id == original_id)
        .order_by(BatchImage.shopify_media_gid.asc())
        .all()
    )
    assert [i.shopify_media_gid for i in images] == [
        "gid://shopify/MediaImage/a1",
        "gid://shopify/MediaImage/b1",
    ]
    db_session.refresh(item2)
    assert item2.status == SecondaryQueueStatus.CONVERTED
    db_session.refresh(batch)
    assert batch.product_count == 1
    assert batch.image_count == 2
    assert (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.shop_id == shop.id, ProcessingBatch.trigger_type == TriggerType.AUTOMATIC)
        .count()
        == 1
    )


def test_multiple_updates_latest_wins(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")
    product = _make_product(db_session, shop, gid_num=9102)
    _seed_empty_baseline(db_session, shop, product)

    svc = PrimaryBatchService(db_session, shop)
    for rev, media in [
        ("v1", [_media("1", "1.png")]),
        ("v2", [_media("1", "1.png"), _media("2", "2.png")]),
        ("v3", [_media("1", "1.png"), _media("2", "2.png"), _media("3", "3.png")]),
    ]:
        item = _claimed_secondary(db_session, shop, product, media, revision=rev)
        db_session.commit()
        svc.convert_secondary_items([item])

    queued = _queued_products(db_session, shop, product.shopify_product_gid)
    assert len(queued) == 1
    assert queued[0].product_snapshot_json.get("revision") == "v3"
    gids = {
        i.shopify_media_gid
        for i in db_session.query(BatchImage).filter(BatchImage.batch_product_id == queued[0].id)
    }
    assert gids == {
        "gid://shopify/MediaImage/1",
        "gid://shopify/MediaImage/2",
        "gid://shopify/MediaImage/3",
    }
    assert (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.trigger_type == TriggerType.AUTOMATIC, ProcessingBatch.shop_id == shop.id)
        .count()
        == 1
    )


def test_processing_creates_new_queued_generation_without_mutating(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")
    product = _make_product(db_session, shop, gid_num=9103)
    _seed_empty_baseline(db_session, shop, product)

    batch100 = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(batch100)
    db_session.flush()
    bp_proc = BatchProduct(
        batch_id=batch100.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
        product_snapshot_json={"revision": "v1"},
        baseline_snapshot_json={"media": [_media("a", "a.png")]},
        claimed_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(bp_proc)
    db_session.flush()
    img_a = BatchImage(
        batch_product_id=bp_proc.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/a",
        cdn_url="https://cdn.shopify.com/a.png",
        original_filename="a.png",
        width=10,
        height=10,
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.PROCESSING,
        source_fingerprint="fp-a",
    )
    db_session.add(img_a)
    db_session.commit()

    item = _claimed_secondary(
        db_session,
        shop,
        product,
        [_media("a", "a.png"), _media("b", "b.png")],
        revision="v2",
    )
    db_session.commit()
    batch_new = PrimaryBatchService(db_session, shop).convert_secondary_items([item])
    assert batch_new is not None
    assert batch_new.id != batch100.id
    assert batch_new.status == BatchStatus.QUEUED

    db_session.refresh(bp_proc)
    db_session.refresh(img_a)
    assert bp_proc.status == BatchProductStatus.PROCESSING
    assert bp_proc.product_snapshot_json.get("revision") == "v1"
    assert img_a.status == BatchImageStatus.PROCESSING
    assert img_a.cdn_url == "https://cdn.shopify.com/a.png"

    queued = _queued_products(db_session, shop, product.shopify_product_gid)
    assert len(queued) == 1
    q_images = db_session.query(BatchImage).filter(BatchImage.batch_product_id == queued[0].id).all()
    assert len(q_images) == 1
    assert q_images[0].shopify_media_gid.endswith("/b")


def test_processing_plus_queued_refresh_to_v3(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")
    product = _make_product(db_session, shop, gid_num=9104)
    _seed_empty_baseline(db_session, shop, product)

    batch100 = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch100)
    db_session.flush()
    bp_proc = BatchProduct(
        batch_id=batch100.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
        product_snapshot_json={"revision": "v1"},
    )
    db_session.add(bp_proc)
    db_session.flush()
    db_session.add(
        BatchImage(
            batch_product_id=bp_proc.id,
            shop_id=shop.id,
            shopify_media_gid="gid://shopify/MediaImage/a",
            cdn_url="https://cdn.shopify.com/a.png",
            original_filename="a.png",
            width=10,
            height=10,
            delta_type=DeltaType.NEW,
            status=BatchImageStatus.PROCESSING,
            source_fingerprint="fp-a",
        )
    )

    batch101 = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch101)
    db_session.flush()
    bp_q = BatchProduct(
        batch_id=batch101.id,
        shop_id=shop.id,
        product_id=product.id,
        shopify_product_gid=product.shopify_product_gid,
        status=BatchProductStatus.QUEUED,
        image_count=1,
        product_snapshot_json={"revision": "v2"},
        baseline_snapshot_json={"media": [_media("a", "a.png"), _media("b", "b.png")]},
    )
    db_session.add(bp_q)
    db_session.flush()
    db_session.add(
        BatchImage(
            batch_product_id=bp_q.id,
            shop_id=shop.id,
            shopify_media_gid="gid://shopify/MediaImage/b",
            cdn_url="https://cdn.shopify.com/b.png",
            original_filename="b.png",
            width=10,
            height=10,
            delta_type=DeltaType.NEW,
            status=BatchImageStatus.QUEUED,
        )
    )
    db_session.commit()

    item = _claimed_secondary(
        db_session,
        shop,
        product,
        [_media("a", "a.png"), _media("b", "b.png"), _media("c", "c.png")],
        revision="v3",
    )
    db_session.commit()
    PrimaryBatchService(db_session, shop).convert_secondary_items([item])

    db_session.refresh(bp_proc)
    assert bp_proc.status == BatchProductStatus.PROCESSING
    assert bp_proc.product_snapshot_json.get("revision") == "v1"

    queued = _queued_products(db_session, shop, product.shopify_product_gid)
    assert len(queued) == 1
    assert queued[0].id == bp_q.id
    assert queued[0].product_snapshot_json.get("revision") == "v3"
    gids = {
        i.shopify_media_gid
        for i in db_session.query(BatchImage).filter(BatchImage.batch_product_id == bp_q.id)
    }
    assert gids == {"gid://shopify/MediaImage/b", "gid://shopify/MediaImage/c"}
    assert (
        db_session.query(BatchProduct)
        .filter(BatchProduct.shopify_product_gid == product.shopify_product_gid)
        .count()
        == 2
    )


def test_fill_partial_automatic_batch(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=5,
        image_count=5,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(batch)
    db_session.flush()
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9200 + i, title=f"Old{i}")
        _seed_empty_baseline(db_session, shop, p)
        bp = BatchProduct(
            batch_id=batch.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)
        db_session.flush()
        db_session.add(
            BatchImage(
                batch_product_id=bp.id,
                shop_id=shop.id,
                shopify_media_gid=f"gid://shopify/MediaImage/old{i}",
                cdn_url=f"https://cdn.shopify.com/old{i}.png",
                status=BatchImageStatus.QUEUED,
                delta_type=DeltaType.NEW,
            )
        )

    items = []
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9300 + i, title=f"New{i}")
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"n{i}", f"n{i}.png")]))
    db_session.commit()

    PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    db_session.refresh(batch)
    assert batch.product_count == 10
    assert (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.shop_id == shop.id, ProcessingBatch.trigger_type == TriggerType.AUTOMATIC)
        .count()
        == 1
    )
    for item in items:
        db_session.refresh(item)
        assert item.status == SecondaryQueueStatus.CONVERTED


def test_overflow_creates_second_batch(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=7,
        image_count=7,
    )
    db_session.add(batch)
    db_session.flush()
    for i in range(7):
        p = _make_product(db_session, shop, gid_num=9400 + i)
        _seed_empty_baseline(db_session, shop, p)
        bp = BatchProduct(
            batch_id=batch.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)

    items = []
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9500 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"o{i}", f"o{i}.png")]))
    db_session.commit()

    PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    db_session.refresh(batch)
    assert batch.product_count == 10
    auto_batches = (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.shop_id == shop.id, ProcessingBatch.trigger_type == TriggerType.AUTOMATIC)
        .order_by(ProcessingBatch.created_at.asc())
        .all()
    )
    assert len(auto_batches) == 2
    assert auto_batches[0].id == batch.id
    assert auto_batches[0].product_count == 10
    assert auto_batches[1].product_count == 2
    assert all(b.product_count <= 10 for b in auto_batches)


def test_same_product_refresh_does_not_consume_capacity(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=5,
        image_count=5,
    )
    db_session.add(batch)
    db_session.flush()
    products = []
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9600 + i, title=f"P{i}")
        _seed_empty_baseline(db_session, shop, p)
        products.append(p)
        bp = BatchProduct(
            batch_id=batch.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
            product_snapshot_json={"revision": "v1"},
        )
        db_session.add(bp)
        db_session.flush()
        db_session.add(
            BatchImage(
                batch_product_id=bp.id,
                shop_id=shop.id,
                shopify_media_gid=f"gid://shopify/MediaImage/p{i}",
                cdn_url=f"https://cdn.shopify.com/p{i}.png",
                status=BatchImageStatus.QUEUED,
                delta_type=DeltaType.NEW,
            )
        )

    # Product A (index 2) update + 5 new products
    items = [
        _claimed_secondary(
            db_session,
            shop,
            products[2],
            [_media("p2", "p2.png"), _media("p2b", "p2b.png")],
            revision="v2",
        )
    ]
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9700 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"x{i}", f"x{i}.png")]))
    db_session.commit()

    PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    db_session.refresh(batch)
    assert batch.product_count == 10
    assert (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.shop_id == shop.id, ProcessingBatch.trigger_type == TriggerType.AUTOMATIC)
        .count()
        == 1
    )
    queued_a = _queued_products(db_session, shop, products[2].shopify_product_gid)
    assert len(queued_a) == 1
    assert queued_a[0].product_snapshot_json.get("revision") == "v2"
    assert queued_a[0].image_count == 2


def test_manual_partial_batch_ignored(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    manual = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.QUEUED,
        product_count=5,
        image_count=5,
    )
    db_session.add(manual)
    db_session.flush()
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9800 + i)
        bp = BatchProduct(
            batch_id=manual.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)

    items = []
    for i in range(3):
        p = _make_product(db_session, shop, gid_num=9810 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"m{i}", f"m{i}.png")]))
    db_session.commit()

    auto = PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    assert auto is not None
    assert auto.trigger_type == TriggerType.AUTOMATIC
    assert auto.id != manual.id
    db_session.refresh(manual)
    assert manual.product_count == 5
    assert (
        db_session.query(BatchProduct)
        .filter(BatchProduct.batch_id == manual.id)
        .count()
        == 5
    )
    assert auto.product_count == 3


def test_processing_batch_ignored_for_capacity(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    processing = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
        product_count=5,
        image_count=5,
    )
    db_session.add(processing)
    db_session.flush()
    for i in range(5):
        p = _make_product(db_session, shop, gid_num=9900 + i)
        bp = BatchProduct(
            batch_id=processing.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.PROCESSING if i == 0 else BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)

    items = []
    for i in range(2):
        p = _make_product(db_session, shop, gid_num=9910 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"c{i}", f"c{i}.png")]))
    db_session.commit()

    auto = PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    assert auto is not None
    assert auto.status == BatchStatus.QUEUED
    assert auto.id != processing.id
    db_session.refresh(processing)
    assert processing.product_count == 5
    assert (
        db_session.query(BatchProduct)
        .filter(BatchProduct.batch_id == processing.id)
        .count()
        == 5
    )


def test_oldest_partial_batch_selected_first(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "auto_batch_product_limit", 10)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    older = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=7,
        image_count=7,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    newer = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
        product_count=4,
        image_count=4,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add_all([older, newer])
    db_session.flush()

    for i in range(7):
        p = _make_product(db_session, shop, gid_num=10000 + i)
        bp = BatchProduct(
            batch_id=older.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)
    for i in range(4):
        p = _make_product(db_session, shop, gid_num=10020 + i)
        bp = BatchProduct(
            batch_id=newer.id,
            shop_id=shop.id,
            product_id=p.id,
            shopify_product_gid=p.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=1,
        )
        db_session.add(bp)

    items = []
    for i in range(2):
        p = _make_product(db_session, shop, gid_num=10040 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"y{i}", f"y{i}.png")]))
    db_session.commit()

    PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.product_count == 9
    assert newer.product_count == 4


def test_capacity_never_exceeded_when_filling(db_session, shop, monkeypatch):
    """Sequential inserts must stop at auto_batch_product_limit (capacity invariant)."""
    monkeypatch.setattr(settings, "auto_batch_product_limit", 3)
    ensure_shop_settings(db_session, shop)
    _configure_product_type(db_session, shop, "Bags")

    items = []
    for i in range(7):
        p = _make_product(db_session, shop, gid_num=10100 + i)
        _seed_empty_baseline(db_session, shop, p)
        items.append(_claimed_secondary(db_session, shop, p, [_media(f"z{i}", f"z{i}.png")]))
    db_session.commit()

    PrimaryBatchService(db_session, shop).convert_secondary_items(items)
    batches = (
        db_session.query(ProcessingBatch)
        .filter(ProcessingBatch.shop_id == shop.id, ProcessingBatch.trigger_type == TriggerType.AUTOMATIC)
        .all()
    )
    assert len(batches) == 3
    assert sorted(b.product_count for b in batches) == [1, 3, 3]
    assert all(b.product_count <= 3 for b in batches)


def test_unique_queued_product_constraint(db_session, shop):
    """Partial unique index / app invariant: only one QUEUED generation per product."""
    ensure_shop_settings(db_session, shop)
    product = _make_product(db_session, shop, gid_num=10200)
    b1 = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
    )
    b2 = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
    )
    db_session.add_all([b1, b2])
    db_session.flush()
    db_session.add(
        BatchProduct(
            batch_id=b1.id,
            shop_id=shop.id,
            product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=0,
        )
    )
    db_session.flush()
    db_session.add(
        BatchProduct(
            batch_id=b2.id,
            shop_id=shop.id,
            product_id=product.id,
            shopify_product_gid=product.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=0,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    # PROCESSING + QUEUED remains allowed
    ensure_shop_settings(db_session, shop)
    product2 = _make_product(db_session, shop, gid_num=10201)
    bp_batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
    )
    bq_batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.QUEUED,
    )
    db_session.add_all([bp_batch, bq_batch])
    db_session.flush()
    db_session.add(
        BatchProduct(
            batch_id=bp_batch.id,
            shop_id=shop.id,
            product_id=product2.id,
            shopify_product_gid=product2.shopify_product_gid,
            status=BatchProductStatus.PROCESSING,
            image_count=0,
        )
    )
    db_session.add(
        BatchProduct(
            batch_id=bq_batch.id,
            shop_id=shop.id,
            product_id=product2.id,
            shopify_product_gid=product2.shopify_product_gid,
            status=BatchProductStatus.QUEUED,
            image_count=0,
        )
    )
    db_session.flush()  # must succeed

"""Prompt Management — product types, steps, variables, and process-time resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.crypto import encrypt_token
from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    DeltaType,
    ProcessingBatch,
    Product,
    PromptProductTypeSource,
    Shop,
    TriggerType,
)
from app.services.image_processor import ImageProcessor
from app.services.prompt_configuration import PromptConfigurationService
from app.services.prompt_product_types import PromptProductTypeService, normalize_product_type_name
from app.services.prompt_resolver import PromptResolver, PromptResolverError
from app.services.prompt_variables import (
    PromptVariableError,
    render_prompt,
    validate_prompt_variables,
)


def _add_product(db_session, shop: Shop, *, title: str, product_type: str | None, gid: str | None = None) -> Product:
    product = Product(
        shop_id=shop.id,
        shopify_product_gid=gid or f"gid://shopify/Product/{uuid4().hex[:8]}",
        title=title,
        product_type=product_type,
        vendor="Aone",
        handle=title.lower().replace(" ", "-"),
        description_html=f"<p>{title}</p>",
        is_deleted=False,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_normalize_product_type_name():
    assert normalize_product_type_name(" Rings ") == "rings"
    assert normalize_product_type_name("RINGS") == "rings"
    assert normalize_product_type_name("  ") is None
    assert normalize_product_type_name(None) is None


def test_sync_shopify_product_types_dedupes_and_ignores_blank(db_session, shop):
    _add_product(db_session, shop, title="A", product_type="Rings")
    _add_product(db_session, shop, title="B", product_type=" rings ")
    _add_product(db_session, shop, title="C", product_type="RINGS")
    _add_product(db_session, shop, title="D", product_type=None)
    _add_product(db_session, shop, title="E", product_type="  ")
    _add_product(db_session, shop, title="F", product_type="Charms")

    svc = PromptProductTypeService(db_session, shop)
    created = svc.sync_shopify_product_types()
    db_session.commit()
    assert created == 2

    items, total = svc.list()
    assert total == 2
    names = {i["name"] for i in items}
    assert "Charms" in names
    sources = {i["source"] for i in items}
    assert sources == {"SHOPIFY"}
    assert all(i["status"] == "NOT_CONFIGURED" for i in items)

    # Idempotent
    assert svc.sync_shopify_product_types() == 0


def test_manual_product_type_add_and_duplicate(db_session, shop, client):
    res = client.post("/api/prompts/product-types", json={"name": " Custom Jewelry "})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["name"] == "Custom Jewelry"
    assert body["data"]["source"] == "MANUAL"
    assert body["data"]["status"] == "NOT_CONFIGURED"

    dup = client.post("/api/prompts/product-types", json={"name": "custom jewelry"})
    assert dup.status_code == 409


def test_shopify_type_cannot_be_deleted_manual_can(db_session, shop, client):
    _add_product(db_session, shop, title="Ring", product_type="Rings")
    PromptProductTypeService(db_session, shop).sync_shopify_product_types()
    db_session.commit()

    listed = client.get("/api/prompts/product-types").json()["data"]["items"]
    shopify_id = next(i["id"] for i in listed if i["source"] == "SHOPIFY")

    forbidden = client.delete(f"/api/prompts/product-types/{shopify_id}")
    assert forbidden.status_code == 403

    created = client.post("/api/prompts/product-types", json={"name": "Temporary"}).json()["data"]
    deleted = client.delete(f"/api/prompts/product-types/{created['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/prompts/product-types/{created['id']}").status_code == 404


def test_configuration_and_steps_lifecycle(client, db_session, shop):
    created = client.post("/api/prompts/product-types", json={"name": "Bracelets"}).json()["data"]
    pt_id = created["id"]

    step1 = client.post(
        f"/api/prompts/product-types/{pt_id}/steps",
        json={"name": "Cleanup", "promptText": "Clean {{product_type}} image", "isEnabled": True},
    ).json()["data"]
    step2 = client.post(
        f"/api/prompts/product-types/{pt_id}/steps",
        json={"name": "Enhance", "promptText": "Enhance {{product_title}}", "isEnabled": True},
    ).json()["data"]

    detail = client.get(f"/api/prompts/product-types/{pt_id}").json()["data"]
    assert detail["stepCount"] == 2
    assert detail["status"] == "ENABLED"
    assert detail["steps"][0]["stepOrder"] == 1
    assert detail["steps"][1]["stepOrder"] == 2

    # Unsupported variable rejected
    bad = client.post(
        f"/api/prompts/product-types/{pt_id}/steps",
        json={"name": "Bad", "promptText": "Price {{product.price * 2}}", "isEnabled": True},
    )
    assert bad.status_code == 422

    # Edit
    updated = client.put(
        f"/api/prompts/steps/{step1['id']}",
        json={"name": "Background Cleanup", "promptText": "Remove bg for {{product_type}}"},
    ).json()["data"]
    assert updated["name"] == "Background Cleanup"
    assert "product_type" in updated["variables"]

    # Disable step
    client.patch(f"/api/prompts/steps/{step2['id']}/status", json={"isEnabled": False})
    detail = client.get(f"/api/prompts/product-types/{pt_id}").json()["data"]
    assert detail["enabledStepCount"] == 1
    assert detail["status"] == "ENABLED"

    # Reorder
    reordered = client.put(
        f"/api/prompts/product-types/{pt_id}/steps/reorder",
        json={"stepIds": [step2["id"], step1["id"]]},
    ).json()["data"]["items"]
    assert [s["id"] for s in reordered] == [step2["id"], step1["id"]]
    assert [s["stepOrder"] for s in reordered] == [1, 2]

    # Delete step normalizes order
    client.delete(f"/api/prompts/steps/{step2['id']}")
    detail = client.get(f"/api/prompts/product-types/{pt_id}").json()["data"]
    assert detail["stepCount"] == 1
    assert detail["steps"][0]["stepOrder"] == 1
    assert detail["steps"][0]["id"] == step1["id"]

    # Disable configuration
    client.patch(f"/api/prompts/product-types/{pt_id}/configuration", json={"isEnabled": False})
    detail = client.get(f"/api/prompts/product-types/{pt_id}").json()["data"]
    assert detail["status"] == "DISABLED"


def test_not_ready_when_all_steps_disabled(client):
    created = client.post("/api/prompts/product-types", json={"name": "Chains"}).json()["data"]
    pt_id = created["id"]
    step = client.post(
        f"/api/prompts/product-types/{pt_id}/steps",
        json={"name": "Only", "promptText": "Do {{product_type}}", "isEnabled": True},
    ).json()["data"]
    client.patch(f"/api/prompts/steps/{step['id']}/status", json={"isEnabled": False})
    listed = client.get("/api/prompts/product-types?status=NOT_READY").json()["data"]["items"]
    assert any(i["id"] == pt_id for i in listed)


def test_variable_validation_and_render():
    assert validate_prompt_variables("Hello {{product_title}} and {{product_type}}") == [
        "product_title",
        "product_type",
    ]
    with pytest.raises(PromptVariableError):
        validate_prompt_variables("Bad {{__class__}}")
    with pytest.raises(PromptVariableError):
        validate_prompt_variables("Bad {{unknown_var}}")

    rendered = render_prompt(
        "Title={{product_title}}; Type={{product_type}}; Missing={{product_vendor}}",
        {"product_title": "Halo Ring", "product_type": "Rings", "product_vendor": None},
    )
    assert rendered == "Title=Halo Ring; Type=Rings; Missing="


def test_resolver_errors(db_session, shop):
    product = _add_product(db_session, shop, title="No Type", product_type=None)
    resolver = PromptResolver(db_session, shop)
    with pytest.raises(PromptResolverError) as missing:
        resolver.resolve_for_product(product)
    assert missing.value.code == "PRODUCT_TYPE_MISSING"

    typed = _add_product(db_session, shop, title="Bracelet", product_type="Bracelets")
    with pytest.raises(PromptResolverError) as unconfigured:
        resolver.resolve_for_product(typed)
    assert unconfigured.value.code == "PROMPT_NOT_CONFIGURED"

    # Configure then disable
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.commit()
    ppt = types.find_by_normalized_name("bracelets")
    assert ppt is not None
    cfg = PromptConfigurationService(db_session, shop)
    cfg.add_step(ppt.id, name="Step", prompt_text="Enhance {{product_type}}", is_enabled=True)
    cfg.set_enabled(ppt.id, False)
    with pytest.raises(PromptResolverError) as disabled:
        resolver.resolve_for_product(typed)
    assert disabled.value.code == "PROMPT_CONFIGURATION_DISABLED"

    cfg.set_enabled(ppt.id, True)
    steps = cfg.get_detail(ppt.id)[1].steps
    cfg.set_step_status(steps[0].id, False)
    with pytest.raises(PromptResolverError) as no_steps:
        resolver.resolve_for_product(typed)
    assert no_steps.value.code == "PROMPT_NO_ENABLED_STEPS"


def test_resolver_order_and_render(db_session, shop):
    product = _add_product(db_session, shop, title="Diamond Ring", product_type="Rings")
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.commit()
    ppt = types.find_by_normalized_name("rings")
    cfg = PromptConfigurationService(db_session, shop)
    s1 = cfg.add_step(ppt.id, name="First", prompt_text="One {{product_title}}", is_enabled=True)
    s2 = cfg.add_step(ppt.id, name="Second", prompt_text="Two {{product_type}}", is_enabled=True)
    s3 = cfg.add_step(ppt.id, name="Disabled", prompt_text="Skip {{shop_name}}", is_enabled=False)
    cfg.reorder_steps(ppt.id, [s2.id, s1.id, s3.id])

    resolved = PromptResolver(db_session, shop).resolve_for_product(product, image_position=2)
    assert len(resolved) == 2
    assert resolved[0].name == "Second"
    assert "Rings" in resolved[0].rendered_prompt
    assert resolved[1].name == "First"
    assert "Diamond Ring" in resolved[1].rendered_prompt


def test_shop_isolation(db_session, shop, client):
    other = Shop(shop_domain="other-shop.myshopify.com", encrypted_access_token=encrypt_token("x"))
    db_session.add(other)
    db_session.commit()

    PromptProductTypeService(db_session, shop).add_manual("Only Mine")
    other_svc = PromptProductTypeService(db_session, other)
    other_svc.add_manual("Other Type")

    items = client.get("/api/prompts/product-types").json()["data"]["items"]
    names = {i["name"] for i in items}
    assert "Only Mine" in names
    assert "Other Type" not in names


def test_processor_skips_openai_when_prompt_missing(db_session, shop, monkeypatch, tmp_path):
    product = _add_product(db_session, shop, title="Ear", product_type="Earrings")
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        pending_product_count=0,
        processing_product_count=1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        product_snapshot_json={"product_type": "Earrings", "title": "Ear"},
        prompt_snapshot_json=None,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/1",
        shopify_file_gid="gid://shopify/MediaImage/1",
        cdn_url="https://cdn.shopify.com/s/files/1/test.png",
        original_filename="ear.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.QUEUED,
    )
    db_session.add(image)
    db_session.commit()

    openai = MagicMock()
    processor = ImageProcessor(db_session, openai_client=openai)
    monkeypatch.setattr(
        "app.services.image_processor.download_shopify_cdn_to_temp",
        lambda url: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    ok = processor._process_single_batch_image(image, bp, worker_id="test")
    assert ok is False
    openai.edit_image.assert_not_called()
    db_session.refresh(image)
    assert image.status == BatchImageStatus.FAILED
    assert image.error_code == "PROMPT_NOT_CONFIGURED"


def test_processor_runs_sequential_steps_and_retry_resolves_new_config(
    db_session, shop, monkeypatch, tmp_path
):
    product = _add_product(db_session, shop, title="Charm", product_type="Charms")
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.commit()
    ppt = types.find_by_normalized_name("charms")
    cfg = PromptConfigurationService(db_session, shop)
    cfg.add_step(ppt.id, name="A", prompt_text="StepA {{product_type}}", is_enabled=True)
    cfg.add_step(ppt.id, name="B", prompt_text="StepB {{product_title}}", is_enabled=True)

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        pending_product_count=0,
        processing_product_count=1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        product_snapshot_json={"product_type": "Charms", "title": "Charm"},
        prompt_snapshot_json=None,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/2",
        shopify_file_gid="gid://shopify/MediaImage/2",
        cdn_url="https://cdn.shopify.com/s/files/1/charm.png",
        original_filename="charm.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.QUEUED,
    )
    db_session.add(image)
    db_session.commit()

    src = tmp_path / "src.png"
    # Minimal valid-looking PNG header path used only as bytes source after download mock
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    src.write_bytes(png_bytes)

    calls: list[str] = []

    def fake_edit(*, image_bytes, prompt, job_id, step, **_kwargs):
        calls.append(prompt)
        return b"out-" + str(step).encode() + image_bytes[:4]

    openai = MagicMock()
    openai.edit_image.side_effect = fake_edit

    def fake_download(url: str):
        path = tmp_path / f"dl-{uuid4().hex[:8]}.png"
        path.write_bytes(png_bytes)
        return path

    monkeypatch.setattr(
        "app.services.image_processor.download_shopify_cdn_to_temp",
        fake_download,
    )
    monkeypatch.setattr(
        "app.services.image_processor.settings.processing_output_directory",
        str(tmp_path / "out"),
    )

    processor = ImageProcessor(db_session, openai_client=openai)
    assert processor._process_single_batch_image(image, bp, worker_id="w1") is True
    assert len(calls) == 2
    assert "Charms" in calls[0]
    assert "Charm" in calls[1]
    db_session.refresh(bp)
    assert isinstance(bp.prompt_snapshot_json, list)
    assert len(bp.prompt_snapshot_json) == 2

    # After merchant adds another step, a fresh process (retry) resolves latest config
    cfg.add_step(ppt.id, name="C", prompt_text="StepC final", is_enabled=True)
    image.status = BatchImageStatus.RETRYING
    image.error_code = None
    image.error_message = None
    image.completed_at = None
    image.output_storage_key = None
    image.current_prompt_step = 0
    bp.status = BatchProductStatus.PROCESSING
    bp.prompt_snapshot_json = None
    db_session.commit()

    calls.clear()
    assert processor._process_single_batch_image(image, bp, worker_id="w2") is True
    assert len(calls) == 3
    assert calls[2] == "StepC final"


def test_list_search_and_status_filter(client):
    client.post("/api/prompts/product-types", json={"name": "Alpha Rings"})
    client.post("/api/prompts/product-types", json={"name": "Beta Charms"})
    pt = client.post("/api/prompts/product-types", json={"name": "Configured"}).json()["data"]
    client.post(
        f"/api/prompts/product-types/{pt['id']}/steps",
        json={"name": "S", "promptText": "Hi {{product_type}}", "isEnabled": True},
    )

    searched = client.get("/api/prompts/product-types?search=alpha").json()["data"]["items"]
    assert len(searched) == 1
    assert searched[0]["name"] == "Alpha Rings"

    configured = client.get("/api/prompts/product-types?status=ENABLED").json()["data"]["items"]
    assert any(i["name"] == "Configured" for i in configured)
    assert all(i["status"] == "ENABLED" for i in configured)

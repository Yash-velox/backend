"""OpenAI Platform Batch API unit tests (Primary Queue remains separate)."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest

from app.config import settings
from app.services.openai_batch_client import (
    IMAGE_EDITS_ENDPOINT,
    BatchLine,
    build_description_body,
    build_image_edit_body,
    extract_image_bytes_from_response_body,
    extract_text_from_responses_body,
    lines_to_jsonl,
    parse_jsonl,
)
from app.services.openai_batch_orchestrator import (
    OpenAIBatchOrchestratorError,
    execution_mode,
    make_custom_id,
    primary_queue_uses_openai_batch,
)
from app.models.enums import AiExecutionMode


def test_make_custom_id_is_deterministic():
    shop = uuid4()
    image = uuid4()
    a = make_custom_id(shop_id=shop, batch_image_id=image, step_order=2, attempt=1)
    b = make_custom_id(shop_id=shop, batch_image_id=image, step_order=2, attempt=1)
    assert a == b
    assert str(shop) in a
    assert str(image) in a
    assert "step_2" in a
    assert "attempt_1" in a


def test_jsonl_roundtrip_and_image_body():
    body = build_image_edit_body(
        model="gpt-image-1",
        prompt="enhance",
        image_url="https://cdn.shopify.com/x.png",
    )
    assert body["images"][0]["image_url"] == "https://cdn.shopify.com/x.png"
    assert "background" not in body
    line = BatchLine(custom_id="c1", method="POST", url=IMAGE_EDITS_ENDPOINT, body=body)
    raw = lines_to_jsonl([line])
    rows = parse_jsonl(raw)
    assert len(rows) == 1
    assert rows[0]["custom_id"] == "c1"
    assert rows[0]["url"] == IMAGE_EDITS_ENDPOINT


def test_image_edit_body_skips_transparent_for_gpt_image_2():
    body = build_image_edit_body(
        model="gpt-image-2",
        prompt="enhance",
        image_url="https://cdn.shopify.com/x.png",
        transparent_background=True,
    )
    assert "background" not in body


def test_image_edit_body_sets_transparent_when_supported():
    body = build_image_edit_body(
        model="gpt-image-1",
        prompt="enhance",
        image_url="https://cdn.shopify.com/x.png",
        transparent_background=True,
    )
    assert body.get("background") == "transparent"


def test_image_edit_body_sets_transparent_for_gpt_image_1_5():
    body = build_image_edit_body(
        model="gpt-image-1.5",
        prompt="enhance",
        image_url="https://cdn.shopify.com/x.png",
        transparent_background=True,
    )
    assert body.get("background") == "transparent"
    assert body["model"] == "gpt-image-1.5"


def test_image_edit_body_honors_transparent_false():
    body = build_image_edit_body(
        model="gpt-image-1",
        prompt="enhance",
        file_id="file-abc",
        transparent_background=False,
    )
    assert "background" not in body
    assert body["images"][0]["file_id"] == "file-abc"


def test_description_body_includes_prior_context():
    body = build_description_body(
        model="gpt-4.1",
        prompt="describe",
        file_id="file-123",
        prior_description="ring metal",
    )
    content = body["input"][0]["content"]
    assert any(part.get("type") == "input_image" and part.get("file_id") == "file-123" for part in content)
    assert "ring metal" in content[0]["text"]


def test_extract_image_and_text():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 16
    body = {"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}
    assert extract_image_bytes_from_response_body(body) == png
    text_body = {
        "output": [{"content": [{"type": "output_text", "text": "  hello jewelry  "}]}],
    }
    assert extract_text_from_responses_body(text_body) == "hello jewelry"


def test_execution_mode_defaults_and_no_silent_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ai_execution_mode", "OPENAI_BATCH")
    monkeypatch.setattr(settings, "openai_batch_enabled", True)
    monkeypatch.setattr(settings, "openai_allow_sync_fallback", False)
    assert execution_mode() == AiExecutionMode.OPENAI_BATCH
    assert primary_queue_uses_openai_batch() is True

    monkeypatch.setattr(settings, "openai_batch_enabled", False)
    with pytest.raises(OpenAIBatchOrchestratorError) as exc:
        primary_queue_uses_openai_batch()
    assert exc.value.code == "OPENAI_BATCH_DISABLED"

    monkeypatch.setattr(settings, "openai_allow_sync_fallback", True)
    assert primary_queue_uses_openai_batch() is False

    monkeypatch.setattr(settings, "ai_execution_mode", "SYNC")
    assert primary_queue_uses_openai_batch() is False


def test_import_maps_by_custom_id_not_order(db_session, shop, monkeypatch, tmp_path):
    """Successful/failed mix advances only successes; failures stay retryable."""
    from app.models import (
        BatchImage,
        BatchImageStatus,
        BatchProduct,
        BatchProductStatus,
        BatchStatus,
        DeltaType,
        ProcessingBatch,
        TriggerType,
    )
    from app.models.enums import OpenAIBatchRequestStatus, OpenAIBatchStatus, PromptStepType
    from app.models.openai_batch import OpenAIBatch, OpenAIBatchRequest
    from app.services import openai_batch_orchestrator as orch_mod

    batch = ProcessingBatch(
        id=uuid4(),
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=2,
        processing_phase="WAITING_FOR_OPENAI",
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        id=uuid4(),
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/1",
        status=BatchProductStatus.PROCESSING,
        image_count=2,
        prompt_snapshot_json=[
            {"step": 1, "name": "Enhance", "prompt": "go", "promptTemplate": "go", "stepType": "IMAGE"}
        ],
    )
    db_session.add(bp)
    db_session.flush()
    img_ok = BatchImage(
        id=uuid4(),
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/1",
        cdn_url="https://cdn.shopify.com/1.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.WAITING_FOR_PROVIDER,
        current_prompt_step=0,
    )
    img_bad = BatchImage(
        id=uuid4(),
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/2",
        cdn_url="https://cdn.shopify.com/2.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.WAITING_FOR_PROVIDER,
        current_prompt_step=0,
    )
    db_session.add_all([img_ok, img_bad])
    db_session.flush()
    ob = OpenAIBatch(
        id=uuid4(),
        shop_id=shop.id,
        primary_batch_id=batch.id,
        workflow_step_order=1,
        step_type=PromptStepType.IMAGE,
        endpoint="/v1/images/edits",
        model="gpt-image-1",
        openai_batch_id="batch_test",
        openai_output_file_id="file_out",
        status=OpenAIBatchStatus.COMPLETED,
        request_count=2,
    )
    db_session.add(ob)
    db_session.flush()
    cid_ok = make_custom_id(shop_id=shop.id, batch_image_id=img_ok.id, step_order=1, attempt=1)
    cid_bad = make_custom_id(shop_id=shop.id, batch_image_id=img_bad.id, step_order=1, attempt=1)
    db_session.add_all(
        [
            OpenAIBatchRequest(
                openai_batch_id=ob.id,
                custom_id=cid_ok,
                batch_image_id=img_ok.id,
                batch_product_id=bp.id,
                source_media_gid=img_ok.shopify_media_gid,
                workflow_step_order=1,
                attempt_number=1,
                status=OpenAIBatchRequestStatus.SUBMITTED,
            ),
            OpenAIBatchRequest(
                openai_batch_id=ob.id,
                custom_id=cid_bad,
                batch_image_id=img_bad.id,
                batch_product_id=bp.id,
                source_media_gid=img_bad.shopify_media_gid,
                workflow_step_order=1,
                attempt_number=1,
                status=OpenAIBatchRequestStatus.SUBMITTED,
            ),
        ]
    )
    db_session.commit()

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    b64 = base64.b64encode(png).decode("ascii")
    output_lines = [
        {
            "custom_id": cid_bad,
            "response": {
                "status_code": 500,
                "body": {"error": {"code": "server_error", "message": "boom"}},
            },
            "error": None,
        },
        {
            "custom_id": cid_ok,
            "response": {"status_code": 200, "body": {"data": [{"b64_json": b64}]}},
            "error": None,
        },
    ]

    class FakeClient:
        def download_file_text(self, file_id: str) -> str:
            assert file_id == "file_out"
            return "\n".join(json.dumps(row) for row in output_lines) + "\n"

        def upload_vision_image(self, image_bytes: bytes, *, filename: str = "intermediate.png") -> str:
            return "file-intermediate"

        def create_batch(self, **kwargs):
            class R:
                id = "batch_retry"
                status = "validating"
                expires_at = None

            return R()

        def upload_batch_jsonl(self, data: bytes, *, filename: str = "batch_input.jsonl") -> str:
            return "file-retry-input"

    monkeypatch.setattr(settings, "processing_output_directory", str(tmp_path))
    monkeypatch.setattr(settings, "processing_max_attempts", 3)

    orch = orch_mod.OpenAIBatchOrchestrator(db_session, client=FakeClient())
    monkeypatch.setattr(orch, "_remaining_image_steps_after", lambda image, step_order: 0)
    monkeypatch.setattr(orch, "_remaining_steps_after", lambda image, step_order: 0)
    monkeypatch.setattr(orch, "_create_retry_batch", lambda parent, failed: None)

    orch.import_batch_results(ob, FakeClient())
    db_session.commit()

    db_session.refresh(img_ok)
    db_session.refresh(img_bad)
    assert img_ok.current_prompt_step == 1
    assert img_ok.output_storage_key is not None
    assert img_ok.error_code is None
    assert img_bad.error_code == "server_error"
    reqs = {
        r.custom_id: r
        for r in db_session.query(OpenAIBatchRequest).filter(OpenAIBatchRequest.openai_batch_id == ob.id)
    }
    assert reqs[cid_ok].status == OpenAIBatchRequestStatus.COMPLETED
    assert reqs[cid_bad].status == OpenAIBatchRequestStatus.FAILED


def test_skip_ai_passthrough_does_not_call_openai(db_session, shop, monkeypatch, tmp_path):
    from app.models import (
        BatchImage,
        BatchImageStatus,
        BatchProduct,
        BatchProductStatus,
        BatchStatus,
        DeltaType,
        ProcessingAttempt,
        ProcessingBatch,
        Product,
        TriggerType,
    )
    from app.models.openai_batch import OpenAIBatch
    from app.services import openai_batch_orchestrator as orch_mod
    from app.services.prompt_configuration import PromptConfigurationService
    from app.services.prompt_product_types import PromptProductTypeService

    monkeypatch.setattr(settings, "skip_ai_provider_call", True)
    monkeypatch.setattr(settings, "ai_execution_mode", "OPENAI_BATCH")
    monkeypatch.setattr(settings, "openai_batch_enabled", True)
    monkeypatch.setattr(settings, "openai_allow_sync_fallback", False)
    monkeypatch.setattr(settings, "ai_provider", "OPEN_AI")
    monkeypatch.setattr(settings, "processing_output_directory", str(tmp_path))

    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/skip-ai",
        title="Skip Ring",
        product_type="Rings",
    )
    db_session.add(product)
    db_session.flush()
    types = PromptProductTypeService(db_session, shop)
    types.sync_shopify_product_types()
    db_session.flush()
    ppt = types.find_by_normalized_name("rings")
    if ppt is None:
        ppt = types.add_manual("Rings")
    PromptConfigurationService(db_session, shop).add_step(
        ppt.id, name="Enhance", prompt_text="enhance {{product_title}}", is_enabled=True
    )
    db_session.commit()

    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.MANUAL,
        status=BatchStatus.QUEUED,
        product_count=1,
        image_count=1,
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        product_snapshot_json={"product_type": "Rings", "title": "Skip Ring"},
        status=BatchProductStatus.QUEUED,
        image_count=1,
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/skip-1",
        cdn_url="https://cdn.shopify.com/skip.png",
        delta_type=DeltaType.INITIAL,
        status=BatchImageStatus.QUEUED,
        current_prompt_step=0,
    )
    db_session.add(image)
    db_session.commit()

    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def fake_download(url: str):
        path = tmp_path / "src.png"
        path.write_bytes(png)
        return path

    monkeypatch.setattr(
        "app.services.image_processor.download_shopify_cdn_to_temp",
        fake_download,
    )

    class BoomClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("OpenAI Batch client must not be constructed")

    monkeypatch.setattr(orch_mod, "OpenAIBatchClient", BoomClient)
    monkeypatch.setattr(
        orch_mod.OpenAIBatchOrchestrator,
        "finalize_ready_images",
        lambda self, *, worker_id: 0,
    )
    orch = orch_mod.OpenAIBatchOrchestrator(db_session)
    stats = orch.tick(worker_id="skip-ai-test")
    db_session.commit()

    db_session.refresh(image)
    db_session.refresh(batch)
    assert stats["submitted"] == 1
    assert stats["polled"] == 0
    assert image.output_storage_key is not None
    assert (tmp_path / image.output_storage_key).read_bytes() == png
    assert image.status == BatchImageStatus.PROCESSING
    assert image.current_prompt_step >= 1
    assert db_session.query(OpenAIBatch).count() == 0
    attempts = db_session.query(ProcessingAttempt).filter(ProcessingAttempt.batch_image_id == image.id).all()
    assert attempts
    assert attempts[0].provider == "skip_ai"
    assert batch.status == BatchStatus.PROCESSING


def test_image_stage_body_never_uses_openai_file_id():
    """Batch /v1/images/edits must use a public URL, not vision file_ids (HTTP_401)."""
    from app.models import BatchImage, BatchImageStatus, DeltaType
    from app.models.enums import PromptStepType
    from app.services.openai_batch_orchestrator import OpenAIBatchOrchestrator

    image = BatchImage(
        id=uuid4(),
        batch_product_id=uuid4(),
        shop_id=uuid4(),
        shopify_media_gid="gid://shopify/MediaImage/1",
        cdn_url="https://cdn.shopify.com/source.png",
        source_fingerprint="fp",
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.WAITING_FOR_PROVIDER,
        current_openai_file_id="file-vision-should-be-ignored",
        output_url="https://cdn.shopify.com/intermediate.png",
    )
    orch = OpenAIBatchOrchestrator.__new__(OpenAIBatchOrchestrator)
    body = OpenAIBatchOrchestrator._build_body_for_image(
        orch,
        image=image,
        step_type=PromptStepType.IMAGE,
        model="gpt-image-1.5",
        prompt="brighten",
    )
    assert body["images"][0]["image_url"] == "https://cdn.shopify.com/intermediate.png"
    assert "file_id" not in body["images"][0]
    assert body["_input_reference"] == "https://cdn.shopify.com/intermediate.png"


def test_import_image_success_hosts_cdn_for_next_image_step(
    db_session, shop, monkeypatch, tmp_path
):
    """After IMAGE step 1 with another IMAGE step remaining, host CDN and clear file_id."""
    from app.models import (
        BatchImage,
        BatchImageStatus,
        BatchProduct,
        BatchProductStatus,
        BatchStatus,
        DeltaType,
        ProcessingBatch,
        Product,
        TriggerType,
    )
    from app.models.enums import PromptStepType
    from app.models.openai_batch import OpenAIBatch, OpenAIBatchRequest
    from app.models.enums import OpenAIBatchRequestStatus, OpenAIBatchStatus
    from app.services import openai_batch_orchestrator as orch_mod

    monkeypatch.setattr(settings, "processing_output_directory", str(tmp_path))

    product = Product(
        shop_id=shop.id,
        shopify_product_gid="gid://shopify/Product/seq-image",
        title="Sequential",
        product_type="Earrings",
    )
    db_session.add(product)
    db_session.flush()
    batch = ProcessingBatch(
        shop_id=shop.id,
        trigger_type=TriggerType.AUTOMATIC,
        status=BatchStatus.PROCESSING,
        product_count=1,
        image_count=1,
        processing_phase="WAITING_FOR_OPENAI",
    )
    db_session.add(batch)
    db_session.flush()
    bp = BatchProduct(
        batch_id=batch.id,
        shop_id=shop.id,
        shopify_product_gid=product.shopify_product_gid,
        product_id=product.id,
        status=BatchProductStatus.PROCESSING,
        image_count=1,
        prompt_snapshot_json=[
            {"step": 1, "stepType": "IMAGE", "prompt": "step1"},
            {"step": 2, "stepType": "IMAGE", "prompt": "step2"},
        ],
    )
    db_session.add(bp)
    db_session.flush()
    image = BatchImage(
        batch_product_id=bp.id,
        shop_id=shop.id,
        shopify_media_gid="gid://shopify/MediaImage/seq-1",
        cdn_url="https://cdn.shopify.com/source.png",
        source_fingerprint="fp-seq",
        delta_type=DeltaType.NEW,
        status=BatchImageStatus.WAITING_FOR_PROVIDER,
        current_prompt_step=0,
        current_openai_file_id=None,
    )
    db_session.add(image)
    db_session.flush()
    ob = OpenAIBatch(
        shop_id=shop.id,
        primary_batch_id=batch.id,
        workflow_step_order=1,
        step_type=PromptStepType.IMAGE,
        endpoint="/v1/images/edits",
        model="gpt-image-1.5",
        openai_batch_id="batch_seq_1",
        openai_output_file_id="file_out_seq",
        status=OpenAIBatchStatus.COMPLETED,
        request_count=1,
    )
    db_session.add(ob)
    db_session.flush()
    cid = make_custom_id(shop_id=shop.id, batch_image_id=image.id, step_order=1, attempt=1)
    req = OpenAIBatchRequest(
        openai_batch_id=ob.id,
        custom_id=cid,
        batch_image_id=image.id,
        batch_product_id=bp.id,
        source_media_gid=image.shopify_media_gid,
        workflow_step_order=1,
        attempt_number=1,
        status=OpenAIBatchRequestStatus.SUBMITTED,
    )
    db_session.add(req)
    db_session.commit()

    png = b"\x89PNG\r\n\x1a\n" + b"1" * 32
    b64 = base64.b64encode(png).decode("ascii")

    class FakeClient:
        def download_file_text(self, file_id: str) -> str:
            return json.dumps(
                {"custom_id": cid, "response": {"status_code": 200, "body": {"data": [{"b64_json": b64}]}}, "error": None}
            ) + "\n"

        def upload_vision_image(self, image_bytes: bytes, *, filename: str = "intermediate.png") -> str:
            raise AssertionError("vision upload must not be used for IMAGE→IMAGE handoff")

    orch = orch_mod.OpenAIBatchOrchestrator(db_session, client=FakeClient())
    monkeypatch.setattr(
        orch,
        "_host_intermediate_image_url",
        lambda image, image_bytes: "https://cdn.shopify.com/hosted-intermediate.png",
    )
    monkeypatch.setattr(orch, "_remaining_image_steps_after", lambda image, step_order: 1)
    monkeypatch.setattr(orch, "_remaining_steps_after", lambda image, step_order: 1)
    monkeypatch.setattr(orch, "_create_retry_batch", lambda parent, failed: None)

    orch.import_batch_results(ob, FakeClient())
    db_session.commit()
    db_session.refresh(image)
    db_session.refresh(req)

    assert image.output_url == "https://cdn.shopify.com/hosted-intermediate.png"
    assert image.current_openai_file_id is None
    assert image.current_prompt_step == 1
    assert req.status == OpenAIBatchRequestStatus.COMPLETED
    assert req.output_reference == "https://cdn.shopify.com/hosted-intermediate.png"

    body = orch._build_body_for_image(
        image=image,
        step_type=PromptStepType.IMAGE,
        model="gpt-image-1.5",
        prompt="step2",
    )
    assert body["images"][0]["image_url"] == "https://cdn.shopify.com/hosted-intermediate.png"
    assert "file_id" not in body["images"][0]

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
    line = BatchLine(custom_id="c1", method="POST", url=IMAGE_EDITS_ENDPOINT, body=body)
    raw = lines_to_jsonl([line])
    rows = parse_jsonl(raw)
    assert len(rows) == 1
    assert rows[0]["custom_id"] == "c1"
    assert rows[0]["url"] == IMAGE_EDITS_ENDPOINT


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

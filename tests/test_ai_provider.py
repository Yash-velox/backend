"""Unit tests for AI_PROVIDER routing (Phase 1 OPEN_AI / Phase 2 external_llm gate)."""

from __future__ import annotations

import pytest

from app.services.ai_provider import (
    AiProvider,
    AiProviderError,
    normalize_ai_provider,
    require_openai_provider,
    skip_ai_output_bytes,
    skip_ai_provider_call,
)
from app.services.openai_batch_orchestrator import (
    OpenAIBatchOrchestratorError,
    primary_queue_uses_openai_batch,
)


def test_normalize_openai_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_provider.settings.ai_provider", "OPEN_AI")
    assert normalize_ai_provider() == AiProvider.OPEN_AI
    assert normalize_ai_provider("openai") == AiProvider.OPEN_AI
    assert normalize_ai_provider("OPEN_AI") == AiProvider.OPEN_AI


def test_normalize_external_llm_aliases() -> None:
    assert normalize_ai_provider("external_llm") == AiProvider.EXTERNAL_LLM
    assert normalize_ai_provider("EXTERNAL_LLM") == AiProvider.EXTERNAL_LLM


def test_require_openai_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_provider.settings.ai_provider", "OPEN_AI")
    require_openai_provider()


def test_require_openai_blocks_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_provider.settings.ai_provider", "external_llm")
    monkeypatch.setattr("app.services.ai_provider.settings.llm_service_url", "http://llm:9000")
    with pytest.raises(AiProviderError) as exc:
        require_openai_provider()
    assert exc.value.code == "EXTERNAL_LLM_NOT_IMPLEMENTED"


def test_primary_queue_batch_blocked_when_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_provider.settings.ai_provider", "external_llm")
    monkeypatch.setattr("app.services.ai_provider.settings.llm_service_url", "")
    with pytest.raises(OpenAIBatchOrchestratorError) as exc:
        primary_queue_uses_openai_batch()
    assert exc.value.code == "EXTERNAL_LLM_NOT_IMPLEMENTED"


def test_skip_ai_provider_call_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_provider.settings.skip_ai_provider_call", False)
    assert skip_ai_provider_call() is False
    monkeypatch.setattr("app.services.ai_provider.settings.skip_ai_provider_call", True)
    assert skip_ai_provider_call() is True


def test_skip_ai_output_bytes_passthrough_png() -> None:
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    assert skip_ai_output_bytes(png) == png
    jpeg = b"\xff\xd8\xff" + b"x" * 16
    stub = skip_ai_output_bytes(jpeg)
    assert stub.startswith(b"\x89PNG\r\n\x1a\n")
    assert stub != jpeg


def test_openai_client_passthrough_does_not_construct_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.poc.openai_client import OpenAIImageClient

    monkeypatch.setattr("app.poc.openai_client.skip_ai_provider_call", lambda: True)
    monkeypatch.setattr("app.poc.openai_client.settings.openai_api_key", "")
    monkeypatch.setattr(
        "app.poc.openai_client.OpenAI",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("OpenAI SDK must not be constructed")),
    )
    client = OpenAIImageClient()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 24
    assert client.edit_image(image_bytes=png, prompt="enhance", job_id="j1", step=1) == png

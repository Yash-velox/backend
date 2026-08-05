"""Unit tests for AI_PROVIDER routing (Phase 1 OPEN_AI / Phase 2 external_llm gate)."""

from __future__ import annotations

import pytest

from app.services.ai_provider import (
    AiProvider,
    AiProviderError,
    normalize_ai_provider,
    require_openai_provider,
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

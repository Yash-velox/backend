"""AI provider routing - Phase 1 OPEN_AI; Phase 2 external_llm microservice."""

from __future__ import annotations

from enum import Enum

from app.config import settings


class AiProvider(str, Enum):
    OPEN_AI = "OPEN_AI"
    EXTERNAL_LLM = "EXTERNAL_LLM"


class AiProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "AI_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


def normalize_ai_provider(raw: str | None = None) -> AiProvider:
    value = (raw if raw is not None else settings.ai_provider or "OPEN_AI").strip().upper()
    # Accept common aliases from env examples / senior naming.
    if value in {"OPENAI", "OPEN_AI"}:
        return AiProvider.OPEN_AI
    if value in {"EXTERNAL_LLM", "EXTERNAL", "OPENSOURCE", "OPEN_SOURCE"}:
        return AiProvider.EXTERNAL_LLM
    raise AiProviderError(
        f"Invalid AI_PROVIDER={raw!r}. Use OPEN_AI or external_llm.",
        code="INVALID_AI_PROVIDER",
    )


def require_openai_provider() -> None:
    """Primary Queue OpenAI paths must not run when Phase 2 external_llm is selected."""
    provider = normalize_ai_provider()
    if provider == AiProvider.OPEN_AI:
        return
    if provider == AiProvider.EXTERNAL_LLM:
        url = (settings.llm_service_url or "").strip()
        raise AiProviderError(
            "AI_PROVIDER=external_llm is configured but the LLM microservice client is not "
            "implemented yet. Set AI_PROVIDER=OPEN_AI for Phase 1, or implement Phase 2 "
            f"against LLM_SERVICE_URL ({url or 'unset'}).",
            code="EXTERNAL_LLM_NOT_IMPLEMENTED",
        )
    raise AiProviderError(f"Unsupported AI_PROVIDER={provider!r}", code="INVALID_AI_PROVIDER")

"""AI provider routing - Phase 1 OPEN_AI; Phase 2 external_llm microservice."""

from __future__ import annotations

from enum import Enum

from app.config import settings

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 1x1 RGBA PNG used when SKIP_AI_PROVIDER_CALL is on and the source is not already PNG.
SKIP_AI_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


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


def skip_ai_provider_call() -> bool:
    """When true, never call OpenAI. Used for load tests. Default false."""
    return bool(settings.skip_ai_provider_call)


def skip_ai_output_bytes(image_bytes: bytes) -> bytes:
    """Identity passthrough for PNG sources; placeholder PNG otherwise (Shopify upload needs PNG)."""
    if image_bytes.startswith(PNG_SIGNATURE):
        return image_bytes
    return SKIP_AI_PLACEHOLDER_PNG

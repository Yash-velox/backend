"""Shared OpenAI image-model compatibility helpers."""

from __future__ import annotations

# These models reject background=transparent (HTTP 400 invalid_value).
# gpt-image-1 and gpt-image-1.5 support it.
_MODELS_WITHOUT_TRANSPARENT_BG = frozenset({"gpt-image-2", "gpt-image-2-2026-04-21"})


def supports_transparent_background(model: str) -> bool:
    return model.strip().lower() not in _MODELS_WITHOUT_TRANSPARENT_BG

"""Shared OpenAI image-model compatibility helpers."""

from __future__ import annotations

# gpt-image-2 rejects background=transparent (HTTP 400 invalid_value).
_MODELS_WITHOUT_TRANSPARENT_BG = frozenset({"gpt-image-2"})


def supports_transparent_background(model: str) -> bool:
    return model.strip().lower() not in _MODELS_WITHOUT_TRANSPARENT_BG

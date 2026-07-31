"""Safe prompt variable registry, validation, and runtime replacement."""

from __future__ import annotations

import re
from typing import Mapping

SUPPORTED_PROMPT_VARIABLES: frozenset[str] = frozenset(
    {
        "product_title",
        "product_type",
        "product_vendor",
        "product_handle",
        "product_description",
        "image_filename",
        "image_position",
        "shop_name",
    }
)

# Only simple {{identifier}} placeholders — no expressions, dots, or filters.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Catch any {{...}} that is not a simple identifier (reject nested/expressions).
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")


class PromptVariableError(ValueError):
    def __init__(self, message: str, *, code: str = "PROMPT_VARIABLE_UNSUPPORTED") -> None:
        super().__init__(message)
        self.code = code


def extract_variables(prompt_text: str) -> list[str]:
    """Return unique variable names in appearance order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(prompt_text or ""):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def validate_prompt_variables(prompt_text: str) -> list[str]:
    """Validate placeholders; return supported variables used.

    Raises PromptVariableError for unsupported or malformed placeholders.
    """
    text = prompt_text or ""
    unsupported: list[str] = []
    for match in _ANY_PLACEHOLDER_RE.finditer(text):
        raw = match.group(1).strip()
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", raw):
            unsupported.append(raw)
            continue
        if raw not in SUPPORTED_PROMPT_VARIABLES:
            unsupported.append(raw)

    if unsupported:
        unique = sorted(set(unsupported))
        raise PromptVariableError(
            f"Unsupported or invalid prompt variable(s): {', '.join(unique)}. "
            f"Allowed: {', '.join(sorted(SUPPORTED_PROMPT_VARIABLES))}."
        )
    return extract_variables(text)


def render_prompt(prompt_text: str, values: Mapping[str, str | None]) -> str:
    """Replace supported {{variables}} with string values. Missing → empty string."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in SUPPORTED_PROMPT_VARIABLES:
            return match.group(0)
        value = values.get(name)
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(_replace, prompt_text or "")


def list_supported_variables() -> list[dict[str, str]]:
    return [
        {"name": name, "token": f"{{{{{name}}}}}"}
        for name in sorted(SUPPORTED_PROMPT_VARIABLES)
    ]

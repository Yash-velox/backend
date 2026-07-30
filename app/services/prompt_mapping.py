"""Product-type to prompt sequence mapping."""

from __future__ import annotations

from typing import Any


class PromptMappingService:
    """Resolve ordered prompt steps for a Shopify product type."""

    def resolve_for_product_type(self, product_type: str | None) -> list[dict[str, Any]]:
        normalized = (product_type or "").strip().lower()
        prompts = self._prompts_for_type(normalized)
        steps: list[dict[str, Any]] = []
        for index, prompt in enumerate(prompts, start=1):
            steps.append(
                {
                    "step": index,
                    "prompt": prompt,
                    "preserveTransparency": False,
                }
            )
        steps.append(
            {
                "step": len(steps) + 1,
                "prompt": (
                    "Ensure the product subject has a clean transparent background suitable for ecommerce. "
                    "Return a transparent PNG preserving product edges and accurate colors."
                ),
                "preserveTransparency": True,
            }
        )
        return steps

    def _prompts_for_type(self, product_type: str) -> list[str]:
        if product_type in {"clothing", "apparel", "fashion"}:
            return [
                "Enhance this apparel product photo for ecommerce: accurate fabric color, "
                "natural drape, sharp details, clean neutral background.",
            ]
        if product_type in {"jewelry", "accessories"}:
            return [
                "Enhance this jewelry/accessory product photo: crisp metal and gem details, "
                "accurate reflections, professional studio lighting.",
            ]
        if product_type in {"food", "grocery", "beverage"}:
            return [
                "Enhance this food product photo: appetizing colors, sharp texture details, "
                "clean background, commercial packaging clarity.",
            ]
        return [
            "Enhance this product image for ecommerce: clean background, accurate colors, "
            "sharp details, professional catalog quality.",
        ]

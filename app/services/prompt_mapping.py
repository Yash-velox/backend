"""Legacy facade — product-type prompts are resolved via PromptResolver at process time.

Kept for any transitional imports; does not provide a default jewelry prompt.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Product, Shop
from app.services.prompt_resolver import PromptResolver, PromptResolverError


class PromptMappingService:
    """Compatibility wrapper around PromptResolver (no default fallback)."""

    def __init__(self, db: Session | None = None, shop: Shop | None = None) -> None:
        self.db = db
        self.shop = shop

    def resolve_for_product_type(self, product_type: str | None) -> list[dict[str, Any]]:
        """Deprecated path used only when db/shop are available.

        Without a DB-backed configuration this raises — there is no global default prompt.
        """
        if self.db is None or self.shop is None:
            raise PromptResolverError(
                "Prompt mapping requires a shop-scoped database session. "
                "Configure prompts for the product type before processing.",
                code="PROMPT_NOT_CONFIGURED",
                retryable=False,
            )
        product = Product(product_type=product_type, shop_id=self.shop.id)
        steps = PromptResolver(self.db, self.shop).resolve_for_product(
            product,
            product_type_override=product_type,
        )
        return PromptResolver(self.db, self.shop).to_snapshot(steps)

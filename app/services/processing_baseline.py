"""Shared ProcessingBaseline updates after Shopify media mutations we caused."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import ProcessingBaseline, Product
from app.services.snapshot import media_snapshots_from_shopify

logger = logging.getLogger("app.services.processing_baseline")


def advance_processing_baseline_to_live_media(
    db: Session,
    *,
    shop_id: UUID,
    catalog_product: Product | None,
    shopify_product_gid: str,
    product_snapshot: dict[str, Any] | None,
    final_snapshot: dict[str, Any],
    reason: str,
) -> None:
    """Point ProcessingBaseline at live Shopify media so our own mutations do not re-queue."""
    if catalog_product is None:
        logger.warning(
            "Skip ProcessingBaseline advance after %s; catalog product missing | product=%s",
            reason,
            shopify_product_gid,
        )
        return

    media_rows = [
        {**row, "is_visible": True}
        for row in (final_snapshot.get("media") or [])
        if isinstance(row, dict)
    ]
    media_snapshot = media_snapshots_from_shopify(media_rows, visible_only=False)
    now = datetime.now(timezone.utc)

    baseline = (
        db.query(ProcessingBaseline)
        .filter(
            ProcessingBaseline.shop_id == shop_id,
            ProcessingBaseline.product_id == catalog_product.id,
        )
        .one_or_none()
    )
    if baseline is None:
        baseline = ProcessingBaseline(shop_id=shop_id, product_id=catalog_product.id)
        db.add(baseline)

    baseline.product_snapshot_json = product_snapshot or {}
    baseline.media_snapshot_json = media_snapshot
    baseline.successfully_processed_at = now
    baseline.evaluated_at = now
    db.flush()
    logger.info(
        "ProcessingBaseline advanced after %s | product=%s media=%s",
        reason,
        shopify_product_gid,
        len(media_snapshot),
    )

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.core.shop_resolver import resolve_shop_access_token
from app.models import ProcessingQueueItem, QueueItemStatus, Shop, SourceType
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError

logger = logging.getLogger("app.services.shopify_image_source")

ACTIVE_STATUSES = {
    QueueItemStatus.PENDING,
    QueueItemStatus.QUEUED,
    QueueItemStatus.PROCESSING,
    QueueItemStatus.RETRY_PENDING,
}

_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/\d+$")
_NUMERIC_RE = re.compile(r"^\d+$")


@dataclass
class ShopifyMediaImage:
    product_id: str
    media_id: str
    image_id: str | None
    cdn_url: str
    filename: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    alt: str | None = None


@dataclass
class EnqueueResult:
    products_requested: int
    products_found: int
    images_found: int
    images_queued: int
    duplicates_skipped: int
    errors: list[dict[str, str]]
    items: list[ProcessingQueueItem]


def normalize_product_gid(raw: str) -> str:
    value = raw.strip()
    if _GID_PRODUCT_RE.match(value):
        return value
    if _NUMERIC_RE.match(value):
        return f"gid://shopify/Product/{value}"
    if value.startswith("gid://shopify/Product/"):
        return value
    raise ValueError(f"Invalid product ID: {raw}")


def fingerprint_config(processing_config: dict | None, prompt_data: Any) -> str:
    payload = {"config": processing_config or {}, "prompts": prompt_data or []}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _filename_from_url(url: str) -> str | None:
    try:
        path = url.split("?", 1)[0]
        name = path.rsplit("/", 1)[-1]
        return name or None
    except Exception:
        return None


def extract_media_images(product_node: dict[str, Any]) -> list[ShopifyMediaImage]:
    product_id = product_node.get("id") or ""
    media_nodes = ((product_node.get("media") or {}).get("nodes")) or []
    images: list[ShopifyMediaImage] = []
    for media in media_nodes:
        if not media:
            continue
        if media.get("mediaContentType") not in (None, "IMAGE") and "image" not in media:
            continue
        image = media.get("image") or {}
        url = image.get("url") or ((media.get("originalSource") or {}).get("url"))
        if not url:
            continue
        media_id = media.get("id") or ""
        images.append(
            ShopifyMediaImage(
                product_id=product_id,
                media_id=media_id,
                image_id=media.get("id"),
                cdn_url=url,
                filename=_filename_from_url(url),
                mime_type="image/jpeg" if ".jpg" in url.lower() or ".jpeg" in url.lower() else "image/png",
                width=image.get("width"),
                height=image.get("height"),
                alt=media.get("alt"),
            )
        )
    return images


class ShopifyImageSourceService:
    def __init__(self, db: Session, shop: Shop) -> None:
        self.db = db
        self.shop = shop

    def enqueue_products(
        self,
        product_ids: list[str],
        *,
        prompt_data: Any = None,
        processing_config: dict | None = None,
        priority: int = 100,
        graphql_client: ShopifyGraphQLClient | None = None,
    ) -> EnqueueResult:
        errors: list[dict[str, str]] = []
        gids: list[str] = []
        for raw in product_ids:
            try:
                gids.append(normalize_product_gid(raw))
            except ValueError as exc:
                errors.append({"productId": raw, "message": str(exc)})

        unique_gids = list(dict.fromkeys(gids))
        client = graphql_client
        if client is None:
            token = resolve_shop_access_token(self.shop, db=self.db)
            client = ShopifyGraphQLClient(shop_domain=self.shop.shop_domain, access_token=token)

        products_found = 0
        images_found = 0
        images_queued = 0
        duplicates_skipped = 0
        created_items: list[ProcessingQueueItem] = []
        fp = fingerprint_config(processing_config, prompt_data)

        try:
            nodes = client.fetch_products_media(unique_gids)
        except ShopifyGraphQLError as exc:
            logger.error(
                "Shopify media fetch failed | shop=%s error=%s",
                self.shop.shop_domain,
                exc,
            )
            return EnqueueResult(
                products_requested=len(product_ids),
                products_found=0,
                images_found=0,
                images_queued=0,
                duplicates_skipped=0,
                errors=[{"productId": "*", "message": str(exc)}],
                items=[],
            )

        found_ids = {n.get("id") for n in nodes if n and n.get("id")}
        for gid in unique_gids:
            if gid not in found_ids:
                errors.append({"productId": gid, "message": "Product not found for this shop"})

        for node in nodes:
            products_found += 1
            media_images = extract_media_images(node)
            images_found += len(media_images)
            for media in media_images:
                if self._has_active_duplicate(media.product_id, media.media_id, fp):
                    duplicates_skipped += 1
                    logger.info(
                        "Duplicate skip | shop=%s product=%s media=%s fingerprint=%s",
                        self.shop.id,
                        media.product_id,
                        media.media_id,
                        fp,
                    )
                    continue
                item = ProcessingQueueItem(
                    shop_id=self.shop.id,
                    source_type=SourceType.SHOPIFY_PRODUCT_MEDIA,
                    shopify_product_id=media.product_id,
                    shopify_media_id=media.media_id,
                    shopify_image_id=media.image_id,
                    shopify_cdn_url=media.cdn_url,
                    original_filename=media.filename,
                    source_mime_type=media.mime_type,
                    source_width=media.width,
                    source_height=media.height,
                    status=QueueItemStatus.PENDING,
                    priority=priority,
                    processing_config=processing_config,
                    processing_config_fingerprint=fp,
                    prompt_data=prompt_data,
                    attempt_count=0,
                    max_attempts=settings.processing_max_attempts,
                )
                self.db.add(item)
                created_items.append(item)
                images_queued += 1

        self.db.commit()
        for item in created_items:
            self.db.refresh(item)

        logger.info(
            "Enqueue complete | shop=%s requested=%s found=%s images=%s queued=%s duplicates=%s",
            self.shop.id,
            len(product_ids),
            products_found,
            images_found,
            images_queued,
            duplicates_skipped,
        )
        return EnqueueResult(
            products_requested=len(product_ids),
            products_found=products_found,
            images_found=images_found,
            images_queued=images_queued,
            duplicates_skipped=duplicates_skipped,
            errors=errors,
            items=created_items,
        )

    def _has_active_duplicate(self, product_id: str, media_id: str, fingerprint: str) -> bool:
        existing = (
            self.db.query(ProcessingQueueItem)
            .filter(
                ProcessingQueueItem.shop_id == self.shop.id,
                ProcessingQueueItem.shopify_product_id == product_id,
                ProcessingQueueItem.shopify_media_id == media_id,
                ProcessingQueueItem.processing_config_fingerprint == fingerprint,
                ProcessingQueueItem.status.in_(list(ACTIVE_STATUSES)),
            )
            .first()
        )
        return existing is not None

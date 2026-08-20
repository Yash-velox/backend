#!/usr/bin/env python3
"""
One-off ops script: add an existing media image URL to the first N products,
then trigger the existing `products/update` webhook processing path.

Key point: conversion processing is driven by the backend's webhook intake →
worker logic, which refreshes product + media via Shopify GraphQL. Therefore
this script must actually attach the image to products in Shopify.

Usage (from repo root):
  cd /home/yash-velox/Aone-Content
  python Backend/app/scripts/add_media_url_to_first_25_products_and_trigger_webhook.py \
    --media-url "https://..." \
    --shop-domain "your-shop.myshopify.com" \
    --count 25 \
    --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "Backend"
sys.path.insert(0, str(BACKEND))


_PRODUCT_GID_RE = re.compile(r"^gid://shopify/Product/(\d+)$")
_MEDIA_IMAGE_GID_RE = re.compile(r"^gid://shopify/MediaImage/(\d+)$")
_SHOPIFY_ADMIN_FILE_URL_RE = re.compile(r"content/files/(\d+)")


def _normalize_shop_domain(raw: str) -> str:
    domain = raw.strip()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0].strip().lower()
    return domain


def _product_numeric_id_from_gid(product_gid: str) -> int | None:
    m = _PRODUCT_GID_RE.match(product_gid.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_media_image_gid(media_url_or_gid: str) -> str | None:
    """
    Support operators passing either:
    - `gid://shopify/MediaImage/<id>` directly
    - a Shopify Admin URL like `https://admin.shopify.com/.../content/files/<id>`
    """

    raw = media_url_or_gid.strip()
    m = _MEDIA_IMAGE_GID_RE.match(raw)
    if m:
        return raw
    m2 = _SHOPIFY_ADMIN_FILE_URL_RE.search(raw)
    if m2:
        return f"gid://shopify/MediaImage/{m2.group(1)}"
    return None


def _media_filename_from_url(media_url: str) -> str:
    parsed = urllib.parse.urlparse(media_url)
    base = os.path.basename(parsed.path)
    base = base.split("?", 1)[0].split("#", 1)[0].strip()
    if not base:
        base = "image"
    # Drop common extensions so we can safely append ".png" later if needed.
    # For Shopify Files API, filename can be any string; extension helps content sniffing.
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        if ext and len(ext) <= 6:
            return f"{stem}.{ext}"
    return base


def _product_contains_original_source_media(product_node: dict[str, object], *, media_url: str) -> bool:
    """
    Best-effort check to avoid creating duplicate media.

    We compare against MediaImage.originalSource.url and MediaImage.image.url.
    """

    media = product_node.get("media") or {}
    if not isinstance(media, dict):
        return False
    nodes = media.get("nodes") or []
    if not isinstance(nodes, list):
        return False

    for node in nodes:
        if not isinstance(node, dict):
            continue
        original_source = node.get("originalSource") or {}
        if isinstance(original_source, dict):
            src = original_source.get("url")
            if isinstance(src, str) and src.strip() == media_url.strip():
                return True
        image = node.get("image") or {}
        if isinstance(image, dict):
            src2 = image.get("url")
            if isinstance(src2, str) and src2.strip() == media_url.strip():
                return True
    return False


def _product_contains_media_image_gid(product_node: dict[str, object], *, media_image_gid: str) -> bool:
    media = product_node.get("media") or {}
    if not isinstance(media, dict):
        return False
    nodes = media.get("nodes") or []
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id.strip() == media_image_gid.strip():
            return True
    return False


def _media_present_in_product_fetch(
    product_node: dict[str, object] | None,
    *,
    media_url: str | None,
    media_image_gid: str | None,
) -> bool:
    if not product_node:
        return False
    if media_image_gid:
        return _product_contains_media_image_gid(product_node, media_image_gid=media_image_gid)
    if media_url:
        return _product_contains_original_source_media(product_node, media_url=media_url)
    return False


def _poll_until_media_attached(
    *,
    client,
    product_gid: str,
    media_url: str | None,
    media_image_gid: str | None,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    delay = 1.0
    while time.monotonic() < deadline:
        node = client.fetch_product_by_gid(product_gid)
        if _media_present_in_product_fetch(
            node,
            media_url=media_url,
            media_image_gid=media_image_gid,
        ):
            return
        time.sleep(delay)
        delay = min(delay * 1.6, 6.0)
    raise RuntimeError(
        f"Timed out waiting for attached media to appear | product={product_gid} media_url={media_url} media_gid={media_image_gid}"
    )


def main(argv: list[str] | None = None) -> int:
    os.chdir(BACKEND)

    parser = argparse.ArgumentParser(description="Attach a media URL to products and trigger webhook processing")
    parser.add_argument("--media-url", required=True, help="Publicly reachable image URL for Shopify Files ingestion")
    parser.add_argument("--shop-domain", required=False, default=None, help="Shop domain (e.g. my-store.myshopify.com)")
    parser.add_argument("--count", required=False, type=int, default=25, help="Number of products to take from Shopify")
    parser.add_argument(
        "--topic",
        required=False,
        type=str,
        default="products/update",
        help="Webhook topic to process (default: products/update)",
    )
    parser.add_argument(
        "--file-ready-timeout-seconds",
        required=False,
        type=float,
        default=120.0,
        help="Wait for Shopify file status READY before attaching (default: 120s)",
    )
    parser.add_argument(
        "--propagation-timeout-seconds",
        required=False,
        type=float,
        default=60.0,
        help="Wait for product media to reflect new attachment before triggering webhook (default: 60s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="When omitted, script runs in dry-run mode (no Shopify mutations, no webhook processing).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Alias for running without --apply.")
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run) or not bool(args.apply)
    media_url = args.media_url.strip()
    if not media_url:
        print("Missing --media-url", file=sys.stderr)
        return 2

    from app.core.shop_resolver import create_shopify_graphql_client
    from app.db.session import SessionLocal
    from app.models import Shop, ShopStatus
    from app.services.webhook_intake import WebhookIntakeService

    db = SessionLocal()
    try:
        if args.shop_domain:
            shop_domain = _normalize_shop_domain(args.shop_domain)
        else:
            # Heuristic for operators: if there's exactly one active shop, use it.
            q = db.query(Shop).filter(Shop.status == ShopStatus.ACTIVE)
            shops = q.all()
            if not shops:
                print("No active shops found in DB. Provide --shop-domain.", file=sys.stderr)
                return 2
            if len(shops) > 1:
                print("Multiple active shops found. Provide --shop-domain explicitly.", file=sys.stderr)
                return 2
            shop_domain = shops[0].shop_domain

        shop = db.query(Shop).filter(Shop.shop_domain == shop_domain).one_or_none()
        if shop is None:
            print(f"Shop not found in DB: {shop_domain}", file=sys.stderr)
            return 2

        client = create_shopify_graphql_client(db, shop)

        media_image_gid = _extract_media_image_gid(media_url)
        if media_image_gid:
            print(f"Detected existing Shopify MediaImage gid | media_gid={media_image_gid}")

        print(f"Fetching first {args.count} products | shop={shop_domain}")
        products_page = client.fetch_products_page(first=max(1, min(int(args.count), 50)))
        products: list[dict[str, object]] = products_page.get("products") or []

        if not products:
            print("No products returned from Shopify for the first page.", file=sys.stderr)
            return 1

        already_present: list[dict[str, object]] = []
        needs_update: list[dict[str, object]] = []
        for p in products:
            if not isinstance(p, dict):
                continue
            if media_image_gid:
                if _product_contains_media_image_gid(p, media_image_gid=media_image_gid):
                    already_present.append(p)
                else:
                    needs_update.append(p)
            elif _product_contains_original_source_media(p, media_url=media_url):
                already_present.append(p)
            else:
                needs_update.append(p)

        print(f"Products total={len(products)} already_have_media={len(already_present)} needs_update={len(needs_update)}")
        if not needs_update:
            print("Nothing to do. Exiting.")
            return 0

        if dry_run:
            print("Dry-run: skipping Shopify mutations + webhook processing.")
            for p in needs_update:
                product_gid = str(p.get("id") or "")
                print(f"  would attach + trigger webhook | product_gid={product_gid}")
            return 0

        # Either:
        # - attach an existing MediaImage gid directly (preferred for Admin content/files URLs)
        # - or create ONE Media file from an external URL and reuse the result across products
        if media_image_gid:
            file_gid = media_image_gid
            print(f"Attaching existing MediaImage | file_gid={file_gid}")

            deadline = time.monotonic() + float(args.file_ready_timeout_seconds)
            delay = 1.0
            while time.monotonic() < deadline:
                statuses = client.get_file_statuses([file_gid])
                if statuses:
                    node = statuses[0] or {}
                    status = str((node.get("fileStatus") or "")).upper()
                    if status == "READY":
                        break
                    if status == "FAILED":
                        raise RuntimeError(f"Shopify existing file is FAILED | file_gid={file_gid}")
                time.sleep(delay)
                delay = min(delay * 1.6, 8.0)
            else:
                raise RuntimeError(f"Timed out waiting for Shopify existing file READY | file_gid={file_gid}")
        else:
            filename = _media_filename_from_url(media_url)
            print(f"Creating Shopify media file from URL (once) | filename={filename}")
            created_files = client.create_shopify_files(
                [
                    {
                        "contentType": "IMAGE",
                        "originalSource": media_url,
                        "filename": filename,
                        "alt": "",
                    }
                ]
            )
            if not created_files:
                raise RuntimeError("Shopify fileCreate returned no files")
            created = created_files[0]
            file_gid = str(created.get("id") or "")
            if not file_gid:
                raise RuntimeError("Shopify fileCreate result missing file id")

            # Wait for the file/media to become usable for product association + conversion.
            deadline = time.monotonic() + float(args.file_ready_timeout_seconds)
            delay = 1.0
            while time.monotonic() < deadline:
                statuses = client.get_file_statuses([file_gid])
                if statuses:
                    node = statuses[0] or {}
                    status = str((node.get("fileStatus") or "")).upper()
                    if status == "READY":
                        break
                    if status == "FAILED":
                        raise RuntimeError(f"Shopify file processing FAILED | file_gid={file_gid}")
                time.sleep(delay)
                delay = min(delay * 1.6, 8.0)

            else:
                raise RuntimeError(f"Timed out waiting for Shopify file READY | file_gid={file_gid}")

        # Attach + trigger conversion for each product missing it.
        webhook_svc = WebhookIntakeService(db)
        webhook_ts = int(time.time())
        for idx, p in enumerate(needs_update, start=1):
            product_gid = str(p.get("id") or "")
            numeric_id = _product_numeric_id_from_gid(product_gid) or _product_numeric_id_from_gid(str(p.get("id") or ""))
            if not product_gid or numeric_id is None:
                print(f"Skipping product with invalid gid | p={p}")
                continue

            product_status = str(p.get("status") or "active")
            product_title = str(p.get("title") or "")
            product_handle = str(p.get("handle") or "")

            webhook_id = f"manual-image-add-{webhook_ts}-{numeric_id}"
            payload = {
                "admin_graphql_api_id": product_gid,
                "id": numeric_id,
                "title": product_title,
                "handle": product_handle,
                "status": product_status.lower(),
                # Force detect_status_only() to return False even if only status changed.
                "images": [{"src": media_url}],
            }
            raw_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

            print(f"[{idx}/{len(needs_update)}] Attaching file + triggering conversion | product_gid={product_gid}")
            client.add_file_product_references(file_gids=[file_gid], product_gid=product_gid)
            _poll_until_media_attached(
                client=client,
                product_gid=product_gid,
                media_url=media_url if not media_image_gid else None,
                media_image_gid=media_image_gid,
                timeout_seconds=float(args.propagation_timeout_seconds),
            )
            webhook_svc.record_and_process_products_update(
                shop_domain=shop_domain,
                webhook_id=webhook_id,
                topic=args.topic,
                payload=payload,
                raw_hash=raw_hash,
            )

            # Small pause to reduce bursty webhook refresh load.
            time.sleep(0.3)

        print("Completed image attachment + webhook triggering.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())


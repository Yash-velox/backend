"""Shopify staged upload + fileCreate + READY polling for publish PNGs."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.services.shopify_graphql import ShopifyGraphQLClient, ShopifyGraphQLError

logger = logging.getLogger("app.services.shopify_file_upload")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PublishUploadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_png_file(path: Path) -> tuple[int, bytes]:
    if not path.is_file():
        raise PublishUploadError("PUBLISH_OUTPUT_MISSING", f"Output file missing: {path.name}")
    data = path.read_bytes()
    if not data:
        raise PublishUploadError("PUBLISH_OUTPUT_INVALID", f"Output file empty: {path.name}")
    if not data.startswith(PNG_SIGNATURE):
        raise PublishUploadError("PUBLISH_OUTPUT_NOT_PNG", f"Output is not a PNG: {path.name}")
    return len(data), data


def sanitize_png_filename(original: str | None, fallback: str = "image") -> str:
    base = (original or fallback).rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in base).strip("._") or fallback
    return f"{cleaned}.png"


class ShopifyFileUploadService:
    def __init__(self, client: ShopifyGraphQLClient) -> None:
        self.client = client

    def upload_png(
        self,
        *,
        path: Path,
        filename: str,
        existing_file_gid: str | None = None,
    ) -> dict[str, Any]:
        """Upload a PNG (or reuse existing READY file). Returns {file_gid, file_status, cdn_url}."""
        if existing_file_gid:
            reused = self._reuse_or_none(existing_file_gid)
            if reused is not None:
                return reused

        size, data = validate_png_file(path)
        targets = self.client.create_staged_image_uploads(
            [
                {
                    "filename": filename,
                    "mimeType": "image/png",
                    "httpMethod": "POST",
                    "resource": "IMAGE",
                    "fileSize": str(size),
                }
            ]
        )
        if not targets:
            raise PublishUploadError("SHOPIFY_STAGED_UPLOAD_FAILED", "No staged upload target returned")

        target = targets[0]
        resource_url = target.get("resourceUrl")
        upload_url = target.get("url")
        parameters = target.get("parameters") or []
        if not resource_url or not upload_url:
            raise PublishUploadError("SHOPIFY_STAGED_UPLOAD_FAILED", "Staged upload target incomplete")

        self._post_multipart(upload_url, parameters, filename, data)

        files = self.client.create_shopify_files(
            [
                {
                    "contentType": "IMAGE",
                    "originalSource": resource_url,
                    "filename": filename,
                    "alt": "",
                }
            ]
        )
        if not files:
            raise PublishUploadError("SHOPIFY_FILE_CREATE_FAILED", "fileCreate returned no files")
        created = files[0]
        file_gid = created.get("id")
        if not file_gid:
            raise PublishUploadError("SHOPIFY_FILE_CREATE_FAILED", "fileCreate missing file id")

        ready = self.poll_until_ready(file_gid)
        return ready

    def _reuse_or_none(self, file_gid: str) -> dict[str, Any] | None:
        statuses = self.client.get_file_statuses([file_gid])
        if not statuses:
            return None
        node = statuses[0]
        status = (node.get("fileStatus") or "").upper()
        if status == "READY":
            image = node.get("image") or {}
            return {
                "file_gid": node.get("id") or file_gid,
                "file_status": "READY",
                "cdn_url": image.get("url"),
                "width": image.get("width"),
                "height": image.get("height"),
            }
        if status in {"UPLOADED", "PROCESSING"}:
            return self.poll_until_ready(file_gid)
        if status == "FAILED":
            logger.warning("Existing Shopify file FAILED; will recreate | gid=%s", file_gid)
            return None
        return None

    def poll_until_ready(self, file_gid: str) -> dict[str, Any]:
        deadline = time.monotonic() + settings.shopify_file_ready_timeout_seconds
        delay = max(0.5, settings.shopify_file_status_poll_seconds)
        while time.monotonic() < deadline:
            statuses = self.client.get_file_statuses([file_gid])
            if not statuses:
                time.sleep(delay)
                delay = min(delay * 1.5, 10.0)
                continue
            node = statuses[0]
            status = (node.get("fileStatus") or "").upper()
            if status == "READY":
                image = node.get("image") or {}
                return {
                    "file_gid": node.get("id") or file_gid,
                    "file_status": "READY",
                    "cdn_url": image.get("url"),
                    "width": image.get("width"),
                    "height": image.get("height"),
                }
            if status == "FAILED":
                raise PublishUploadError(
                    "SHOPIFY_FILE_PROCESSING_FAILED",
                    f"Shopify file processing failed for {file_gid}",
                )
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)
        raise PublishUploadError(
            "SHOPIFY_FILE_READY_TIMEOUT",
            f"Timed out waiting for Shopify file READY: {file_gid}",
            retryable=True,
        )

    def _post_multipart(
        self,
        upload_url: str,
        parameters: list[dict[str, Any]],
        filename: str,
        data: bytes,
    ) -> None:
        form: dict[str, Any] = {}
        for param in parameters:
            name = param.get("name")
            value = param.get("value")
            if name:
                form[str(name)] = str(value) if value is not None else ""
        files = {"file": (filename, data, "image/png")}
        try:
            response = httpx.post(upload_url, data=form, files=files, timeout=120.0)
        except httpx.HTTPError as exc:
            raise PublishUploadError(
                "SHOPIFY_BINARY_UPLOAD_FAILED",
                f"Binary upload network error: {exc}",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise PublishUploadError(
                "SHOPIFY_BINARY_UPLOAD_FAILED",
                f"Binary upload HTTP {response.status_code}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )


def poll_reorder_job(client: ShopifyGraphQLClient, job_gid: str | None) -> None:
    if not job_gid:
        return
    deadline = time.monotonic() + settings.shopify_reorder_timeout_seconds
    delay = 1.0
    while time.monotonic() < deadline:
        status = client.get_job_status(job_gid)
        if status.get("done"):
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 8.0)
    raise ShopifyGraphQLError(f"Reorder job timed out: {job_gid}", retryable=True)

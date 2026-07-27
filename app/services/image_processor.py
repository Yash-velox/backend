from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AttemptStatus, ProcessingAttempt, ProcessingQueueItem, QueueItemStatus
from app.poc.openai_client import OpenAIImageClient, OpenAIImageError
from app.services.batch_service import BatchService, assert_transition
from app.services.output_storage import OutputStorage, checksum_sha256, get_output_storage
from app.services.retry_service import RetryService

logger = logging.getLogger("app.services.image_processor")

ALLOWED_CDN_HOST_SUFFIXES = (
    "cdn.shopify.com",
    "shopify.com",
    "shopifycdn.com",
)


class ProcessingError(Exception):
    def __init__(self, message: str, *, code: str = "PROCESSING_ERROR", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _is_allowed_cdn_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_CDN_HOST_SUFFIXES)


def _classify_http_error(status_code: int) -> tuple[str, bool]:
    if status_code in {429, 500, 502, 503, 504}:
        return f"HTTP_{status_code}", True
    if status_code == 404:
        return "SOURCE_NOT_FOUND", False
    return f"HTTP_{status_code}", False


def download_shopify_cdn_to_temp(url: str) -> Path:
    if not _is_allowed_cdn_url(url):
        raise ProcessingError("Shopify CDN URL is not allowed", code="INVALID_CDN_URL", retryable=False)

    timeout = settings.shopify_image_download_timeout_seconds
    max_bytes = settings.shopify_image_max_download_mb * 1024 * 1024
    logger.info("CDN download start | url_host=%s", urlparse(url).hostname)
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
            if response.status_code >= 400:
                code, retryable = _classify_http_error(response.status_code)
                raise ProcessingError(
                    f"Failed to download Shopify image (HTTP {response.status_code})",
                    code=code,
                    retryable=retryable,
                )
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            tmp = tempfile.NamedTemporaryFile(prefix="shopify_src_", suffix=".img", delete=False)
            tmp_path = Path(tmp.name)
            total = 0
            try:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ProcessingError(
                            "Shopify image exceeds maximum download size",
                            code="SOURCE_TOO_LARGE",
                            retryable=False,
                        )
                    tmp.write(chunk)
                tmp.close()
            except Exception:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise
    except ProcessingError:
        raise
    except httpx.TimeoutException as exc:
        raise ProcessingError("Shopify CDN download timed out", code="CDN_TIMEOUT", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise ProcessingError(f"Shopify CDN network error: {exc}", code="CDN_NETWORK", retryable=True) from exc

    data = tmp_path.read_bytes()
    if len(data) < 24:
        tmp_path.unlink(missing_ok=True)
        raise ProcessingError("Downloaded file is not a valid image", code="CORRUPT_IMAGE", retryable=False)

    # Basic magic-byte checks
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = data[:2] == b"\xff\xd8"
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (is_png or is_jpeg or is_webp):
        if content_type and not content_type.startswith("image/"):
            tmp_path.unlink(missing_ok=True)
            raise ProcessingError("Unsupported source content type", code="UNSUPPORTED_TYPE", retryable=False)

    logger.info("CDN download complete | bytes=%s", len(data))
    return tmp_path


def _default_prompts(prompt_data: object | None) -> list[str]:
    if isinstance(prompt_data, list) and prompt_data:
        prompts: list[str] = []
        for entry in prompt_data:
            if isinstance(entry, str) and entry.strip():
                prompts.append(entry.strip())
            elif isinstance(entry, dict):
                text = str(entry.get("prompt") or "").strip()
                if text:
                    prompts.append(text)
        if prompts:
            return prompts
    return [
        "Enhance this product image for ecommerce: clean background, accurate colors, sharp details. "
        "Return a transparent PNG when the subject is isolated."
    ]


class ImageProcessor:
    def __init__(
        self,
        db: Session,
        *,
        storage: OutputStorage | None = None,
        openai_client: OpenAIImageClient | None = None,
    ) -> None:
        self.db = db
        self.storage = storage or get_output_storage()
        self._openai = openai_client
        self.batch_service = BatchService(db)
        self.retry_service = RetryService(db)

    def _client(self) -> OpenAIImageClient:
        if self._openai is None:
            self._openai = OpenAIImageClient()
        return self._openai

    def process_queue_item(self, item_id: UUID, *, worker_id: str) -> None:
        item = self.db.get(ProcessingQueueItem, item_id)
        if not item:
            logger.error("Queue item missing | item_id=%s", item_id)
            return

        now = datetime.now(timezone.utc)
        try:
            assert_transition(item.status, QueueItemStatus.PROCESSING)
        except ValueError:
            logger.warning("Skip item — invalid start state | item_id=%s status=%s", item.id, item.status)
            return

        item.status = QueueItemStatus.PROCESSING
        item.attempt_count += 1
        item.processing_started_at = now
        item.locked_by = worker_id
        item.locked_at = now
        item.error_code = None
        item.error_message = None

        attempt = ProcessingAttempt(
            queue_item_id=item.id,
            batch_id=item.batch_id,
            attempt_number=item.attempt_count,
            status=AttemptStatus.STARTED,
            provider="openai",
            shopify_source_url=item.shopify_cdn_url,
            started_at=now,
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(item)
        self.db.refresh(attempt)

        if item.batch_id:
            self.batch_service.refresh_batch_summary(item.batch_id)

        temp_path: Path | None = None
        try:
            temp_path = download_shopify_cdn_to_temp(item.shopify_cdn_url)
            input_bytes = temp_path.read_bytes()
            prompts = _default_prompts(item.prompt_data)
            client = self._client()
            current = input_bytes
            for index, prompt in enumerate(prompts, start=1):
                current = client.edit_image(
                    image_bytes=current,
                    prompt=prompt,
                    job_id=str(item.id),
                    step=index,
                )
                if not current:
                    raise ProcessingError("AI provider returned empty output", code="EMPTY_OUTPUT", retryable=True)

            key = f"{item.shop_id}/{item.id}/output.png"
            output_ref = self.storage.save_bytes(key=key, data=current, content_type="image/png")
            checksum = checksum_sha256(current)

            item.status = QueueItemStatus.COMPLETED
            item.output_storage_key = key
            item.output_url = output_ref
            item.output_mime_type = "image/png"
            item.output_checksum = checksum
            item.processing_completed_at = datetime.now(timezone.utc)
            item.locked_by = None
            item.locked_at = None
            item.error_code = None
            item.error_message = None

            attempt.status = AttemptStatus.COMPLETED
            attempt.output_storage_key = key
            attempt.completed_at = datetime.now(timezone.utc)
            self.db.commit()

            logger.info(
                "Item completed | item_id=%s batch_id=%s attempt=%s output_key=%s",
                item.id,
                item.batch_id,
                item.attempt_count,
                key,
            )
        except Exception as exc:
            retryable = False
            code = "PROCESSING_ERROR"
            message = "Image processing failed"
            if isinstance(exc, ProcessingError):
                retryable = exc.retryable
                code = exc.code
                message = str(exc)
            elif isinstance(exc, OpenAIImageError):
                text = str(exc).lower()
                retryable = any(token in text for token in ("timeout", "429", "rate", "500", "502", "503", "504"))
                code = "AI_PROVIDER_ERROR"
                message = "AI image processing failed. Please retry later."
                logger.exception(
                    "AI provider failure | item_id=%s attempt=%s",
                    item.id,
                    item.attempt_count,
                )
            else:
                logger.exception("Unexpected processing failure | item_id=%s", item.id)
                retryable = True
                message = "Unexpected processing error"

            attempt.status = AttemptStatus.FAILED
            attempt.error_code = code
            attempt.error_message = message
            attempt.completed_at = datetime.now(timezone.utc)
            # Ensure item is PROCESSING for retry transitions
            if item.status != QueueItemStatus.PROCESSING:
                item.status = QueueItemStatus.PROCESSING
            self.retry_service.schedule_retry(item, error_code=code, error_message=message, retryable=retryable)
            self.db.commit()
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to delete temp CDN file | path=%s", temp_path)

        if item.batch_id:
            self.batch_service.refresh_batch_summary(item.batch_id)

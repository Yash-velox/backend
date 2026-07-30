from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    AttemptStatus,
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    ProcessingAttempt,
    ProcessingBaseline,
    Product,
)
from app.poc.openai_client import OpenAIImageClient, OpenAIImageError
from app.services.output_storage import OutputStorage, checksum_sha256, get_output_storage
from app.services.primary_batch import PrimaryBatchService
from app.services.retry_service import RetryService
from app.services.state_machine import BATCH_IMAGE_TRANSITIONS, BATCH_PRODUCT_TRANSITIONS, assert_transition

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
    content_type = ""
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

    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = data[:2] == b"\xff\xd8"
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (is_png or is_jpeg or is_webp):
        if content_type and not content_type.startswith("image/"):
            tmp_path.unlink(missing_ok=True)
            raise ProcessingError("Unsupported source content type", code="UNSUPPORTED_TYPE", retryable=False)

    logger.info("CDN download complete | bytes=%s", len(data))
    return tmp_path


def _prompts_from_snapshot(prompt_snapshot: object | None) -> list[str]:
    if isinstance(prompt_snapshot, list) and prompt_snapshot:
        prompts: list[str] = []
        for entry in prompt_snapshot:
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
        self.retry_service = RetryService(db)

    def _client(self) -> OpenAIImageClient:
        if self._openai is None:
            self._openai = OpenAIImageClient()
        return self._openai

    def _primary_batch_service(self, shop_id: UUID) -> PrimaryBatchService:
        from app.models import Shop

        shop = self.db.get(Shop, shop_id)
        if shop is None:
            raise ProcessingError("Shop not found", code="SHOP_NOT_FOUND", retryable=False)
        return PrimaryBatchService(self.db, shop)

    def process_batch_product(self, batch_product_id: UUID, *, worker_id: str) -> None:
        batch_product = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(BatchProduct.id == batch_product_id)
            .one_or_none()
        )
        if not batch_product:
            logger.error("Batch product missing | id=%s", batch_product_id)
            return

        if batch_product.status != BatchProductStatus.PROCESSING:
            logger.warning(
                "Skip batch product — not processing | id=%s status=%s",
                batch_product.id,
                batch_product.status,
            )
            return

        images = sorted(batch_product.images, key=lambda i: i.created_at)
        product_failed = False
        for image in images:
            if image.status in (BatchImageStatus.COMPLETED, BatchImageStatus.FAILED):
                if image.status == BatchImageStatus.FAILED:
                    product_failed = True
                continue
            success = self._process_single_batch_image(image, batch_product, worker_id=worker_id)
            if not success:
                product_failed = True
                break

        batch_service = self._primary_batch_service(batch_product.shop_id)
        if product_failed:
            any_retrying = any(i.status == BatchImageStatus.RETRYING for i in batch_product.images)
            if not any_retrying:
                assert_transition(
                    "batch_product",
                    BATCH_PRODUCT_TRANSITIONS,
                    batch_product.status,
                    BatchProductStatus.FAILED,
                )
                batch_product.status = BatchProductStatus.FAILED
                batch_product.completed_at = datetime.now(timezone.utc)
                batch_product.locked_by = None
                batch_product.locked_at = None
        else:
            all_complete = all(i.status == BatchImageStatus.COMPLETED for i in batch_product.images)
            if all_complete:
                assert_transition(
                    "batch_product",
                    BATCH_PRODUCT_TRANSITIONS,
                    batch_product.status,
                    BatchProductStatus.COMPLETED,
                )
                batch_product.status = BatchProductStatus.COMPLETED
                batch_product.completed_at = datetime.now(timezone.utc)
                batch_product.locked_by = None
                batch_product.locked_at = None
                batch_product.error_code = None
                batch_product.error_message = None
                self._advance_baseline_on_success(batch_product)

        batch = batch_service.get_batch(batch_product.batch_id)
        if batch:
            batch_service.refresh_batch_counters(batch)
        self.db.commit()

    def process_batch_image(self, batch_image_id: UUID, *, worker_id: str) -> None:
        image = self.db.get(BatchImage, batch_image_id)
        if not image:
            logger.error("Batch image missing | id=%s", batch_image_id)
            return
        batch_product = self.db.get(BatchProduct, image.batch_product_id)
        if not batch_product:
            logger.error("Batch product missing for image | image=%s", batch_image_id)
            return
        self._process_single_batch_image(image, batch_product, worker_id=worker_id)
        batch_service = self._primary_batch_service(batch_product.shop_id)
        batch = batch_service.get_batch(batch_product.batch_id)
        if batch:
            batch_service.refresh_batch_counters(batch)
        self.db.commit()

    def _process_single_batch_image(
        self,
        image: BatchImage,
        batch_product: BatchProduct,
        *,
        worker_id: str,
    ) -> bool:
        now = datetime.now(timezone.utc)
        if image.status not in (
            BatchImageStatus.QUEUED,
            BatchImageStatus.RETRYING,
        ):
            return image.status == BatchImageStatus.COMPLETED

        target = BatchImageStatus.DOWNLOADING
        assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, target)
        image.status = target
        image.attempt_count += 1
        image.started_at = image.started_at or now
        image.error_code = None
        image.error_message = None

        attempt = ProcessingAttempt(
            batch_image_id=image.id,
            batch_product_id=batch_product.id,
            attempt_number=image.attempt_count,
            status=AttemptStatus.STARTED,
            provider="openai",
            shopify_source_url=image.cdn_url,
            started_at=now,
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(image)
        self.db.refresh(attempt)

        temp_path: Path | None = None
        started_perf = time.perf_counter()
        try:
            temp_path = download_shopify_cdn_to_temp(image.cdn_url)
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING)
            image.status = BatchImageStatus.PROCESSING
            self.db.commit()

            input_bytes = temp_path.read_bytes()
            prompts = _prompts_from_snapshot(batch_product.prompt_snapshot_json)
            client = self._client()
            current = input_bytes
            for index, prompt in enumerate(prompts, start=1):
                image.current_prompt_step = index
                current = client.edit_image(
                    image_bytes=current,
                    prompt=prompt,
                    job_id=str(image.id),
                    step=index,
                )
                if not current:
                    raise ProcessingError("AI provider returned empty output", code="EMPTY_OUTPUT", retryable=True)

            key = f"{batch_product.shop_id}/{batch_product.batch_id}/{image.id}/output.png"
            output_ref = self.storage.save_bytes(key=key, data=current, content_type="image/png")
            checksum = checksum_sha256(current)

            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.COMPLETED)
            image.status = BatchImageStatus.COMPLETED
            image.output_storage_key = key
            image.output_url = output_ref
            image.output_mime_type = "image/png"
            image.output_checksum = checksum
            image.completed_at = datetime.now(timezone.utc)

            attempt.status = AttemptStatus.COMPLETED
            attempt.output_storage_key = key
            attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
            attempt.completed_at = datetime.now(timezone.utc)
            self.db.commit()
            logger.info(
                "Batch image completed | image=%s product=%s attempt=%s",
                image.id,
                batch_product.id,
                image.attempt_count,
            )
            return True
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
                    "AI provider failure | image=%s attempt=%s",
                    image.id,
                    image.attempt_count,
                )
            else:
                logger.exception("Unexpected processing failure | image=%s", image.id)
                retryable = True
                message = "Unexpected processing error"

            attempt.status = AttemptStatus.FAILED
            attempt.error_code = code
            attempt.error_message = message
            attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
            attempt.completed_at = datetime.now(timezone.utc)

            if image.status != BatchImageStatus.PROCESSING:
                image.status = BatchImageStatus.PROCESSING

            self.retry_service.schedule_image_retry(
                image,
                batch_product,
                error_code=code,
                error_message=message,
                retryable=retryable,
            )
            self.db.commit()
            return False
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to delete temp CDN file | path=%s", temp_path)

    def _advance_baseline_on_success(self, batch_product: BatchProduct) -> None:
        if not batch_product.product_id:
            return
        product = self.db.get(Product, batch_product.product_id)
        if not product:
            return

        baseline = (
            self.db.query(ProcessingBaseline)
            .filter(
                ProcessingBaseline.shop_id == batch_product.shop_id,
                ProcessingBaseline.product_id == product.id,
            )
            .one_or_none()
        )
        now = datetime.now(timezone.utc)
        product_snapshot = batch_product.product_snapshot_json or {}
        media_snapshot = []
        for image in batch_product.images:
            if image.status != BatchImageStatus.COMPLETED:
                continue
            media_snapshot.append(
                {
                    "media_gid": image.shopify_media_gid,
                    "file_gid": image.shopify_file_gid,
                    "cdn_url": image.cdn_url,
                    "filename": image.original_filename,
                    "width": image.width,
                    "height": image.height,
                    "mime_type": image.mime_type,
                    "fingerprint": image.source_fingerprint,
                }
            )

        if baseline is None:
            baseline = ProcessingBaseline(
                shop_id=batch_product.shop_id,
                product_id=product.id,
            )
            self.db.add(baseline)

        baseline.product_snapshot_json = product_snapshot
        baseline.media_snapshot_json = media_snapshot
        baseline.successfully_processed_at = now
        baseline.evaluated_at = now

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
from app.core.shop_resolver import create_shopify_graphql_client
from app.models import (
    AttemptStatus,
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    ProcessingAttempt,
    ProcessingBaseline,
    Product,
    Shop,
)
from app.poc.openai_client import OpenAIImageClient, OpenAIImageError
from app.services.image_versions import ImageVersionsService, build_generated_filename
from app.services.output_storage import OutputStorage, checksum_sha256, get_output_storage
from app.services.primary_batch import PrimaryBatchService
from app.services.prompt_resolver import PromptResolver, PromptResolverError
from app.services.retry_service import RetryService
from app.services.shopify_file_upload import (
    PublishUploadError,
    ShopifyFileUploadService,
    validate_generated_png_for_shopify,
)
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
            if any_retrying:
                # Permanent failure on one image can coexist with earlier RETRYING
                # rows from stale recovery — never leave the product locked in
                # PROCESSING or the worker will never reclaim it.
                if batch_product.status == BatchProductStatus.PROCESSING:
                    assert_transition(
                        "batch_product",
                        BATCH_PRODUCT_TRANSITIONS,
                        batch_product.status,
                        BatchProductStatus.RETRYING,
                    )
                    batch_product.status = BatchProductStatus.RETRYING
                if batch_product.next_retry_at is None:
                    batch_product.next_retry_at = datetime.now(timezone.utc)
                batch_product.locked_by = None
                batch_product.locked_at = None
            else:
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
                batch_product.prompt_override_json = None
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
                batch_product.prompt_override_json = None
                self._advance_baseline_on_success(batch_product)
            else:
                # Exiting without a terminal state — always drop the lock so
                # another poll / recover can continue remaining work.
                batch_product.locked_by = None
                batch_product.locked_at = None

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

    def finalize_local_output(self, batch_image_id: UUID, *, worker_id: str) -> bool:
        """Upload an already-generated local output to Shopify Files (OpenAI Batch path)."""
        image = self.db.get(BatchImage, batch_image_id)
        if not image or not image.output_storage_key:
            return False
        if image.generated_shopify_file_gid:
            return True
        batch_product = self.db.get(BatchProduct, image.batch_product_id)
        if not batch_product:
            return False
        shop = self.db.get(Shop, image.shop_id)
        if not shop:
            return False
        if not self._local_output_usable(image):
            raise ProcessingError(
                "Local AI output missing for Shopify upload",
                code="PUBLISH_OUTPUT_MISSING",
                retryable=True,
            )
        attempt_number = max(image.attempt_count, 1)
        existing_numbers = {
            row[0]
            for row in self.db.query(ProcessingAttempt.attempt_number)
            .filter(ProcessingAttempt.batch_image_id == image.id)
            .all()
        }
        while attempt_number in existing_numbers:
            attempt_number += 1
        attempt = ProcessingAttempt(
            batch_image_id=image.id,
            batch_product_id=batch_product.id,
            attempt_number=attempt_number,
            status=AttemptStatus.STARTED,
            provider="shopify_upload",
            shopify_source_url=image.cdn_url,
            output_storage_key=image.output_storage_key,
        )
        self.db.add(attempt)
        self.db.flush()
        if image.status in {BatchImageStatus.PROCESSING, BatchImageStatus.WAITING_FOR_PROVIDER}:
            if image.status == BatchImageStatus.WAITING_FOR_PROVIDER:
                assert_transition(
                    "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
                )
                image.status = BatchImageStatus.PROCESSING
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.UPLOADING)
            image.status = BatchImageStatus.UPLOADING
            self.db.commit()
        self._upload_generated_output(
            shop=shop,
            image=image,
            batch_product=batch_product,
            attempt=attempt,
        )
        attempt.status = AttemptStatus.COMPLETED
        attempt.completed_at = datetime.now(timezone.utc)
        self.db.commit()

        # Product rollup
        self.db.refresh(batch_product)
        images = sorted(batch_product.images, key=lambda i: i.created_at)
        if all(i.status == BatchImageStatus.COMPLETED for i in images):
            if batch_product.status == BatchProductStatus.PROCESSING:
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
                batch_product.prompt_override_json = None
                self._advance_baseline_on_success(batch_product)
            batch = self._primary_batch_service(batch_product.shop_id).get_batch(batch_product.batch_id)
            if batch:
                self._primary_batch_service(batch_product.shop_id).refresh_batch_counters(batch)
                if batch.status == BatchStatus.PROCESSING and batch.pending_product_count == 0 and batch.processing_product_count == 0:
                    batch.processing_phase = "READY_TO_PUBLISH"
            self.db.commit()
        return True

    def _local_output_usable(self, image: BatchImage) -> bool:
        key = image.output_storage_key
        if not key:
            return False
        try:
            return self.storage.exists(key)
        except Exception:
            return False

    def _upload_generated_output(
        self,
        *,
        shop: Shop,
        image: BatchImage,
        batch_product: BatchProduct,
        attempt: ProcessingAttempt,
    ) -> None:
        """Validate local output, upload to Shopify Files, create image_version, delete local file."""
        if not batch_product.product_id:
            raise ProcessingError(
                "Catalog product id required before Shopify Files upload",
                code="PRODUCT_NOT_LINKED",
                retryable=False,
            )
        if not image.output_storage_key:
            raise ProcessingError(
                "Missing local output for Shopify upload",
                code="PUBLISH_OUTPUT_MISSING",
                retryable=False,
            )

        path = self.storage.resolve_path(image.output_storage_key)
        meta = validate_generated_png_for_shopify(path)

        assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.UPLOADING)
        image.status = BatchImageStatus.UPLOADING
        self.db.commit()

        version_hint = ImageVersionsService(self.db, shop).next_version_number(
            product_id=batch_product.product_id,
            source_media_gid=image.shopify_media_gid,
        )
        stored_filename = build_generated_filename(
            product_id=batch_product.product_id,
            source_media_gid=image.shopify_media_gid,
            version_number=version_hint,
        )
        idempotency_key = f"upload:{image.id}:{image.output_checksum or meta['checksum']}"

        existing_gid = image.generated_shopify_file_gid
        prior_version = ImageVersionsService(self.db, shop).find_by_idempotency_key(idempotency_key)
        if prior_version and prior_version.shopify_file_gid:
            existing_gid = prior_version.shopify_file_gid

        try:
            client = create_shopify_graphql_client(self.db, shop)
        except RuntimeError as exc:
            raise ProcessingError(str(exc), code="SHOPIFY_TOKEN_MISSING", retryable=True) from exc

        uploader = ShopifyFileUploadService(client)
        try:
            result = uploader.upload_png(
                path=path,
                filename=stored_filename,
                existing_file_gid=existing_gid,
            )
        except PublishUploadError as exc:
            ImageVersionsService(self.db, shop).record_upload_failed(
                product_id=batch_product.product_id,
                source_media_gid=image.shopify_media_gid,
                batch_id=batch_product.batch_id,
                batch_image_id=image.id,
                error_code=exc.code,
                error_message=str(exc),
            )
            raise ProcessingError(str(exc), code=exc.code, retryable=exc.retryable) from exc

        version = ImageVersionsService(self.db, shop).create_generated_after_upload(
            product_id=batch_product.product_id,
            source_media_gid=image.shopify_media_gid,
            shopify_file_gid=result["file_gid"],
            shopify_cdn_url=result.get("cdn_url"),
            width=result.get("width") or meta.get("width"),
            height=result.get("height") or meta.get("height"),
            file_size_bytes=meta.get("size_bytes"),
            checksum=meta.get("checksum") or image.output_checksum,
            mime_type="image/png",
            original_filename=image.original_filename,
            stored_filename=stored_filename,
            upload_idempotency_key=idempotency_key,
            batch_id=batch_product.batch_id,
            batch_image_id=image.id,
            attempt_id=attempt.id,
            metadata_json={"validation_warnings": meta.get("warnings") or []},
        )

        image.generated_shopify_file_gid = result["file_gid"]
        image.generated_shopify_cdn_url = result.get("cdn_url")
        image.generated_image_version_id = version.id
        image.output_checksum = meta.get("checksum") or image.output_checksum
        image.output_mime_type = "image/png"

        assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.COMPLETED)
        image.status = BatchImageStatus.COMPLETED
        image.completed_at = datetime.now(timezone.utc)
        image.error_code = None
        image.error_message = None

        attempt.status = AttemptStatus.COMPLETED
        attempt.output_storage_key = image.output_storage_key
        attempt.completed_at = datetime.now(timezone.utc)
        self.db.flush()

        key = image.output_storage_key
        if key:
            self.storage.delete(key)
            image.output_storage_key = None
            image.output_url = None

    def retry_upload_only(self, batch_image_id: UUID, *, worker_id: str = "api") -> BatchImage:
        """Retry Shopify Files upload without re-running AI when local output remains."""
        image = self.db.get(BatchImage, batch_image_id)
        if not image:
            raise ProcessingError("Batch image not found", code="BATCH_IMAGE_NOT_FOUND", retryable=False)
        batch_product = self.db.get(BatchProduct, image.batch_product_id)
        if not batch_product:
            raise ProcessingError("Batch product not found", code="BATCH_PRODUCT_NOT_FOUND", retryable=False)
        shop = self.db.get(Shop, image.shop_id)
        if not shop:
            raise ProcessingError("Shop not found", code="SHOP_NOT_FOUND", retryable=False)
        if not self._local_output_usable(image):
            raise ProcessingError(
                "Local generated output is no longer available; full reprocess required",
                code="UPLOAD_RETRY_OUTPUT_MISSING",
                retryable=False,
            )
        if image.status not in {
            BatchImageStatus.FAILED,
            BatchImageStatus.RETRYING,
            BatchImageStatus.UPLOADING,
            BatchImageStatus.QUEUED,
        }:
            raise ProcessingError(
                f"Image status {image.status.value} cannot retry upload",
                code="UPLOAD_RETRY_INVALID_STATUS",
                retryable=False,
            )

        now = datetime.now(timezone.utc)
        image.attempt_count += 1
        image.error_code = None
        image.error_message = None
        attempt = ProcessingAttempt(
            batch_image_id=image.id,
            batch_product_id=batch_product.id,
            attempt_number=image.attempt_count,
            status=AttemptStatus.STARTED,
            provider="shopify_upload",
            shopify_source_url=image.cdn_url,
            output_storage_key=image.output_storage_key,
            started_at=now,
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(attempt)

        started_perf = time.perf_counter()
        try:
            if image.status == BatchImageStatus.FAILED:
                assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.RETRYING)
                image.status = BatchImageStatus.RETRYING
            if image.status in {BatchImageStatus.QUEUED, BatchImageStatus.RETRYING}:
                assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.UPLOADING)
            self._upload_generated_output(
                shop=shop,
                image=image,
                batch_product=batch_product,
                attempt=attempt,
            )
            attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
            self.db.commit()
            return image
        except Exception as exc:
            retryable = True
            code = "SHOPIFY_UPLOAD_FAILED"
            message = "Shopify Files upload failed"
            if isinstance(exc, ProcessingError):
                retryable = exc.retryable
                code = exc.code
                message = str(exc)
            elif isinstance(exc, PublishUploadError):
                retryable = exc.retryable
                code = exc.code
                message = str(exc)
            attempt.status = AttemptStatus.FAILED
            attempt.error_code = code
            attempt.error_message = message
            attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
            attempt.completed_at = datetime.now(timezone.utc)
            self.retry_service.schedule_image_retry(
                image,
                batch_product,
                error_code=code,
                error_message=message,
                retryable=retryable,
            )
            self.db.commit()
            raise

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
            BatchImageStatus.UPLOADING,
        ):
            return image.status == BatchImageStatus.COMPLETED

        # Prefer upload-only whenever local AI output still exists on retry/upload.
        upload_only = image.status in {BatchImageStatus.RETRYING, BatchImageStatus.UPLOADING} and self._local_output_usable(
            image
        )

        image.attempt_count += 1
        image.started_at = image.started_at or now
        image.error_code = None
        image.error_message = None

        attempt = ProcessingAttempt(
            batch_image_id=image.id,
            batch_product_id=batch_product.id,
            attempt_number=image.attempt_count,
            status=AttemptStatus.STARTED,
            provider="shopify_upload" if upload_only else "openai",
            shopify_source_url=image.cdn_url,
            output_storage_key=image.output_storage_key if upload_only else None,
            started_at=now,
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(image)
        self.db.refresh(attempt)

        temp_path: Path | None = None
        started_perf = time.perf_counter()
        try:
            shop = self.db.get(Shop, batch_product.shop_id)
            if shop is None:
                raise ProcessingError("Shop not found", code="SHOP_NOT_FOUND", retryable=False)

            if upload_only:
                if image.status == BatchImageStatus.RETRYING:
                    assert_transition(
                        "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.UPLOADING
                    )
                self._upload_generated_output(
                    shop=shop,
                    image=image,
                    batch_product=batch_product,
                    attempt=attempt,
                )
                attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
                self.db.commit()
                logger.info(
                    "Batch image upload-only completed | image=%s product=%s attempt=%s",
                    image.id,
                    batch_product.id,
                    image.attempt_count,
                )
                return True

            target = BatchImageStatus.DOWNLOADING
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, target)
            image.status = target
            self.db.commit()

            product = None
            if batch_product.product_id:
                product = self.db.get(Product, batch_product.product_id)
            product_type_override = None
            if product is None and isinstance(batch_product.product_snapshot_json, dict):
                product_type_override = batch_product.product_snapshot_json.get("product_type")
            elif product is not None and not (product.product_type or "").strip():
                if isinstance(batch_product.product_snapshot_json, dict):
                    product_type_override = batch_product.product_snapshot_json.get("product_type")

            images_sorted = sorted(batch_product.images, key=lambda i: i.created_at)
            try:
                image_position = images_sorted.index(image) + 1
            except ValueError:
                image_position = 1

            resolver = PromptResolver(self.db, shop)
            override = image.prompt_override_json or batch_product.prompt_override_json
            try:
                if isinstance(override, list) and override:
                    product_type_display = (
                        (product.product_type if product else None)
                        or (
                            batch_product.product_snapshot_json.get("product_type")
                            if isinstance(batch_product.product_snapshot_json, dict)
                            else None
                        )
                        or "product"
                    )
                    resolved = resolver.resolve_from_override(
                        override,
                        product=product,
                        product_type_display=str(product_type_display),
                        image=image,
                        image_position=image_position,
                    )
                    if image.prompt_override_json is not None:
                        image.prompt_override_json = None
                else:
                    resolved = resolver.resolve_for_product(
                        product,
                        product_type_override=product_type_override,
                        image=image,
                        image_position=image_position,
                    )
            except PromptResolverError as exc:
                raise ProcessingError(str(exc), code=exc.code, retryable=exc.retryable) from exc

            batch_product.prompt_snapshot_json = resolver.to_snapshot(resolved)
            self.db.commit()

            temp_path = download_shopify_cdn_to_temp(image.cdn_url)
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING)
            image.status = BatchImageStatus.PROCESSING
            self.db.commit()

            input_bytes = temp_path.read_bytes()
            client = self._client()
            current = input_bytes
            for index, step in enumerate(resolved, start=1):
                image.current_prompt_step = index
                current = client.edit_image(
                    image_bytes=current,
                    prompt=step.rendered_prompt,
                    job_id=str(image.id),
                    step=index,
                    transparent_background=False,
                )
                if not current:
                    raise ProcessingError("AI provider returned empty output", code="EMPTY_OUTPUT", retryable=True)

            key = f"{batch_product.shop_id}/{batch_product.batch_id}/{image.id}/output.png"
            output_ref = self.storage.save_bytes(key=key, data=current, content_type="image/png")
            checksum = checksum_sha256(current)
            image.output_storage_key = key
            image.output_url = output_ref
            image.output_mime_type = "image/png"
            image.output_checksum = checksum
            attempt.output_storage_key = key
            self.db.commit()

            self._upload_generated_output(
                shop=shop,
                image=image,
                batch_product=batch_product,
                attempt=attempt,
            )
            attempt.duration_ms = (time.perf_counter() - started_perf) * 1000
            self.db.commit()
            logger.info(
                "Batch image completed | image=%s product=%s attempt=%s file=%s",
                image.id,
                batch_product.id,
                image.attempt_count,
                image.generated_shopify_file_gid,
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
            elif isinstance(exc, PublishUploadError):
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

            # Keep local output for Shopify upload retries.
            if not (
                code.startswith("SHOPIFY_")
                or code
                in {
                    "GENERATED_IMAGE_TOO_LARGE",
                    "GENERATED_IMAGE_PIXEL_LIMIT",
                    "SHOPIFY_TOKEN_MISSING",
                }
            ):
                if not self._local_output_usable(image):
                    image.output_storage_key = None
                    image.output_url = None

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

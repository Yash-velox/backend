"""Orchestrate OpenAI Platform Batch API stages for Primary Queue jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    BatchImage,
    BatchImageStatus,
    BatchProduct,
    BatchProductStatus,
    BatchStatus,
    ProcessingAttempt,
    ProcessingBatch,
    Product,
    Shop,
)
from app.models.enums import (
    AiExecutionMode,
    AttemptStatus,
    OpenAIBatchRequestStatus,
    OpenAIBatchStatus,
    OpenAITempFileCleanupStatus,
    ProcessingPhase,
    PromptStepType,
)
from app.models.openai_batch import OpenAIBatch, OpenAIBatchRequest, OpenAITemporaryFile
from app.services.openai_batch_client import (
    IMAGE_EDITS_ENDPOINT,
    RESPONSES_ENDPOINT,
    BatchLine,
    OpenAIBatchClient,
    OpenAIBatchClientError,
    build_description_body,
    build_image_edit_body,
    extract_image_bytes_from_response_body,
    extract_text_from_responses_body,
    lines_to_jsonl,
    parse_jsonl,
)
from app.services.output_storage import checksum_sha256, get_output_storage
from app.services.primary_batch import PrimaryBatchService
from app.services.prompt_resolver import PromptResolver, PromptResolverError, ResolvedPromptStep
from app.services.retry_service import (
    RetryService,
    complete_image_from_shopify_file,
    find_generated_version_for_batch_image,
    next_processing_attempt_number,
    should_skip_openai_for_image,
)
from app.services.state_machine import (
    BATCH_IMAGE_TRANSITIONS,
    BATCH_PRODUCT_TRANSITIONS,
    BATCH_TRANSITIONS,
    assert_transition,
)

logger = logging.getLogger("app.services.openai_batch_orchestrator")

IN_FLIGHT_OPENAI_STATUSES = frozenset(
    {
        OpenAIBatchStatus.DRAFT,
        OpenAIBatchStatus.VALIDATING,
        OpenAIBatchStatus.IN_PROGRESS,
        OpenAIBatchStatus.FINALIZING,
        OpenAIBatchStatus.CANCELLING,
    }
)

TERMINAL_OPENAI_STATUSES = frozenset(
    {
        OpenAIBatchStatus.COMPLETED,
        OpenAIBatchStatus.FAILED,
        OpenAIBatchStatus.EXPIRED,
        OpenAIBatchStatus.CANCELLED,
    }
)


class OpenAIBatchOrchestratorError(RuntimeError):
    def __init__(self, message: str, *, code: str = "OPENAI_BATCH_ORCHESTRATION_ERROR") -> None:
        super().__init__(message)
        self.code = code


def execution_mode() -> AiExecutionMode:
    raw = (settings.ai_execution_mode or "OPENAI_BATCH").strip().upper()
    try:
        return AiExecutionMode(raw)
    except ValueError as exc:
        raise OpenAIBatchOrchestratorError(
            f"Invalid AI_EXECUTION_MODE={settings.ai_execution_mode!r}",
            code="INVALID_AI_EXECUTION_MODE",
        ) from exc


def primary_queue_uses_openai_batch() -> bool:
    from app.services.ai_provider import AiProviderError, require_openai_provider

    try:
        require_openai_provider()
    except AiProviderError as exc:
        raise OpenAIBatchOrchestratorError(str(exc), code=exc.code) from exc

    mode = execution_mode()
    if mode == AiExecutionMode.SYNC:
        return False
    if mode != AiExecutionMode.OPENAI_BATCH:
        return False
    if not settings.openai_batch_enabled:
        if settings.openai_allow_sync_fallback:
            return False
        raise OpenAIBatchOrchestratorError(
            "OPENAI_BATCH is configured but OPENAI_BATCH_ENABLED=false and sync fallback is disabled",
            code="OPENAI_BATCH_DISABLED",
        )
    return True


def make_custom_id(*, shop_id: UUID, batch_image_id: UUID, step_order: int, attempt: int) -> str:
    return f"shop_{shop_id}_batch-image_{batch_image_id}_step_{step_order}_attempt_{attempt}"


@dataclass
class StageTarget:
    step_order: int
    step_type: PromptStepType
    step_id: UUID | None
    rendered_prompt: str
    name: str


class OpenAIBatchOrchestrator:
    def __init__(self, db: Session, client: OpenAIBatchClient | None = None) -> None:
        self.db = db
        self._client = client

    def _client_or_create(self) -> OpenAIBatchClient:
        from app.services.ai_provider import skip_ai_provider_call

        if skip_ai_provider_call():
            raise OpenAIBatchOrchestratorError(
                "OpenAI client is disabled by SKIP_AI_PROVIDER_CALL",
                code="SKIP_AI_PROVIDER_CALL",
            )
        if self._client is None:
            self._client = OpenAIBatchClient()
        return self._client

    def tick(self, *, worker_id: str = "openai-batch-worker") -> dict[str, int]:
        """Restart-safe poll/submit/import/cleanup cycle."""
        from app.services.ai_provider import skip_ai_provider_call

        stats = {
            "polled": 0,
            "imported": 0,
            "submitted": 0,
            "reused": 0,
            "finalized": 0,
            "cleaned": 0,
            "errors": 0,
        }
        try:
            if not primary_queue_uses_openai_batch():
                return stats
        except OpenAIBatchOrchestratorError:
            logger.exception("OpenAI Batch mode misconfigured")
            stats["errors"] += 1
            return stats

        if skip_ai_provider_call():
            logger.warning(
                "SKIP_AI_PROVIDER_CALL=true | OpenAI submit/poll disabled; passthrough source images"
            )
            stats["reused"] = self._reuse_existing_generated_outputs()
            stats["submitted"] = self.submit_ready_stages(worker_id=worker_id)
            stats["finalized"] = self.finalize_ready_images(worker_id=worker_id)
            return stats

        stats["polled"] = self.poll_inflight_batches()
        stats["reused"] = self._reuse_existing_generated_outputs()
        stats["submitted"] = self.submit_ready_stages(worker_id=worker_id)
        stats["finalized"] = self.finalize_ready_images(worker_id=worker_id)
        stats["cleaned"] = self.cleanup_temporary_files()
        return stats

    # ------------------------------------------------------------------ poll
    def poll_inflight_batches(self) -> int:
        from app.services.ai_provider import skip_ai_provider_call

        if skip_ai_provider_call():
            return 0
        rows = (
            self.db.query(OpenAIBatch)
            .filter(OpenAIBatch.status.in_(list(IN_FLIGHT_OPENAI_STATUSES - {OpenAIBatchStatus.DRAFT})))
            .order_by(OpenAIBatch.created_at.asc())
            .all()
        )
        # Also submit DRAFT leftovers that crashed mid-submit
        drafts = (
            self.db.query(OpenAIBatch)
            .filter(OpenAIBatch.status == OpenAIBatchStatus.DRAFT, OpenAIBatch.openai_input_file_id.is_not(None))
            .all()
        )
        count = 0
        client = self._client_or_create()
        for row in list(rows) + list(drafts):
            try:
                if row.status == OpenAIBatchStatus.DRAFT and row.openai_batch_id is None and row.openai_input_file_id:
                    self._submit_existing_draft(row, client)
                    count += 1
                    continue
                if not row.openai_batch_id:
                    continue
                remote = client.retrieve_batch(row.openai_batch_id)
                self._apply_remote_status(row, remote)
                if row.status == OpenAIBatchStatus.COMPLETED:
                    self.import_batch_results(row, client)
                elif row.status in {OpenAIBatchStatus.FAILED, OpenAIBatchStatus.EXPIRED, OpenAIBatchStatus.CANCELLED}:
                    self._handle_terminal_failure(row, client)
                count += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception("Failed polling OpenAI batch | id=%s", row.id)
        return count

    def _submit_existing_draft(self, row: OpenAIBatch, client: OpenAIBatchClient) -> None:
        assert row.openai_input_file_id
        remote = client.create_batch(
            input_file_id=row.openai_input_file_id,
            endpoint=row.endpoint,
            metadata={
                "primary_batch_id": str(row.primary_batch_id),
                "step": str(row.workflow_step_order),
            },
        )
        row.openai_batch_id = remote.id
        row.status = OpenAIBatchStatus(getattr(remote, "status", "validating"))
        row.submitted_at = datetime.now(timezone.utc)
        expires = getattr(remote, "expires_at", None)
        if expires:
            row.expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
        self._set_primary_phase(row.primary_batch_id, ProcessingPhase.WAITING_FOR_OPENAI, active=row.id)
        self.db.flush()

    def _apply_remote_status(self, row: OpenAIBatch, remote: Any) -> None:
        status_raw = str(getattr(remote, "status", "") or "").lower()
        try:
            row.status = OpenAIBatchStatus(status_raw)
        except ValueError:
            logger.warning("Unknown OpenAI batch status %s for %s", status_raw, row.id)
        row.openai_output_file_id = getattr(remote, "output_file_id", None) or row.openai_output_file_id
        row.openai_error_file_id = getattr(remote, "error_file_id", None) or row.openai_error_file_id
        counts = getattr(remote, "request_counts", None)
        if counts is not None:
            row.completed_count = int(getattr(counts, "completed", 0) or 0)
            row.failed_count = int(getattr(counts, "failed", 0) or 0)
            row.request_count = int(getattr(counts, "total", row.request_count) or row.request_count)
        expires = getattr(remote, "expires_at", None)
        if expires:
            row.expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
        if row.status in TERMINAL_OPENAI_STATUSES and row.completed_at is None:
            row.completed_at = datetime.now(timezone.utc)
        primary = self.db.get(ProcessingBatch, row.primary_batch_id)
        if primary is not None:
            primary.openai_requests_total = row.request_count
            primary.openai_requests_completed = row.completed_count
            primary.openai_requests_failed = row.failed_count
            if row.status in {OpenAIBatchStatus.VALIDATING, OpenAIBatchStatus.IN_PROGRESS, OpenAIBatchStatus.FINALIZING}:
                primary.processing_phase = ProcessingPhase.WAITING_FOR_OPENAI.value
                self._heartbeat_product_locks(primary.id)
            elif row.status == OpenAIBatchStatus.COMPLETED:
                primary.processing_phase = ProcessingPhase.COLLECTING_OPENAI_RESULTS.value

    def _heartbeat_product_locks(self, primary_batch_id: UUID) -> None:
        """Keep product locks fresh while OpenAI Platform work is still in progress.

        Stale recovery uses locked_at age. OpenAI batches often run longer than
        processing_stale_lock_seconds; refreshing prevents false STALE_LOCK retries.
        """
        now = datetime.now(timezone.utc)
        products = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(
                BatchProduct.batch_id == primary_batch_id,
                BatchProduct.status == BatchProductStatus.PROCESSING,
                BatchProduct.locked_at.is_not(None),
            )
            .all()
        )
        for product in products:
            if any(img.status == BatchImageStatus.WAITING_FOR_PROVIDER for img in product.images):
                product.locked_at = now

    # ---------------------------------------------------------------- import
    def import_batch_results(self, row: OpenAIBatch, client: OpenAIBatchClient | None = None) -> None:
        client = client or self._client_or_create()
        primary = self.db.get(ProcessingBatch, row.primary_batch_id)
        if primary is not None:
            primary.processing_phase = ProcessingPhase.IMPORTING_STAGE_RESULTS.value
        by_custom = {
            r.custom_id: r
            for r in self.db.query(OpenAIBatchRequest).filter(OpenAIBatchRequest.openai_batch_id == row.id).all()
        }
        succeeded = 0
        failed = 0
        if row.openai_output_file_id:
            for item in parse_jsonl(client.download_file_text(row.openai_output_file_id)):
                custom_id = item.get("custom_id")
                req = by_custom.get(custom_id)
                if req is None:
                    continue
                error = item.get("error")
                response = item.get("response") or {}
                if error or int(response.get("status_code") or 0) >= 400:
                    self._mark_request_failed(req, error, response)
                    failed += 1
                    continue
                body = response.get("body") or {}
                try:
                    if row.step_type == PromptStepType.IMAGE:
                        self._import_image_success(req, body, client)
                    else:
                        self._import_description_success(req, body)
                    succeeded += 1
                except Exception as exc:
                    req.status = OpenAIBatchRequestStatus.FAILED
                    req.error_code = "IMPORT_FAILED"
                    req.error_message = str(exc)
                    req.completed_at = datetime.now(timezone.utc)
                    failed += 1
                    logger.exception("Import failed | custom_id=%s", custom_id)

        if row.openai_error_file_id:
            for item in parse_jsonl(client.download_file_text(row.openai_error_file_id)):
                custom_id = item.get("custom_id")
                req = by_custom.get(custom_id)
                if req is None or req.status == OpenAIBatchRequestStatus.COMPLETED:
                    continue
                self._mark_request_failed(req, item.get("error"), item.get("response") or {})
                failed += 1

        # Any still-submitted requests after import are treated as failed/missing
        for req in by_custom.values():
            if req.status in {OpenAIBatchRequestStatus.PENDING, OpenAIBatchRequestStatus.SUBMITTED}:
                req.status = OpenAIBatchRequestStatus.FAILED
                req.error_code = "MISSING_RESULT"
                req.error_message = "No result returned for this custom_id"
                req.completed_at = datetime.now(timezone.utc)
                failed += 1

        row.completed_count = succeeded
        row.failed_count = failed
        row.status = OpenAIBatchStatus.COMPLETED
        row.completed_at = row.completed_at or datetime.now(timezone.utc)
        self.db.flush()

        failed_reqs = [
            r
            for r in by_custom.values()
            if r.status in {OpenAIBatchRequestStatus.FAILED, OpenAIBatchRequestStatus.EXPIRED}
        ]
        if failed_reqs:
            if primary is not None:
                primary.processing_phase = ProcessingPhase.RETRYING_FAILED_REQUESTS.value
            self._create_retry_batch(row, failed_reqs)
        elif primary is not None:
            primary.processing_phase = ProcessingPhase.PREPARING_NEXT_STAGE.value
            primary.active_openai_batch_id = None

    def _mark_request_failed(self, req: OpenAIBatchRequest, error: Any, response: dict[str, Any]) -> None:
        req.status = OpenAIBatchRequestStatus.FAILED
        if isinstance(error, dict):
            req.error_code = str(error.get("code") or "OPENAI_REQUEST_FAILED")
            req.error_message = str(error.get("message") or error)
        else:
            body = response.get("body") or {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                req.error_code = str(err.get("code") or f"HTTP_{response.get('status_code')}")
                req.error_message = str(err.get("message") or err)
            else:
                req.error_code = f"HTTP_{response.get('status_code') or 'ERROR'}"
                req.error_message = str(error or body or "OpenAI request failed")
        if req.error_code == "batch_expired":
            req.status = OpenAIBatchRequestStatus.EXPIRED
        req.completed_at = datetime.now(timezone.utc)
        image = self.db.get(BatchImage, req.batch_image_id)
        if image is not None:
            image.error_code = req.error_code
            image.error_message = req.error_message

    def _import_image_success(self, req: OpenAIBatchRequest, body: dict[str, Any], client: OpenAIBatchClient) -> None:
        image_bytes = extract_image_bytes_from_response_body(body)
        image = self.db.get(BatchImage, req.batch_image_id)
        if image is None:
            raise OpenAIBatchOrchestratorError("Batch image missing", code="BATCH_IMAGE_NOT_FOUND")
        later_image = self._remaining_image_steps_after(image, req.workflow_step_order)
        later_any = self._remaining_steps_after(image, req.workflow_step_order)
        is_last_image = later_image == 0

        if is_last_image:
            storage = get_output_storage()
            product = self.db.get(BatchProduct, image.batch_product_id)
            key = f"{image.shop_id}/{product.batch_id if product else 'unknown'}/{image.id}/output.png"
            output_ref = storage.save_bytes(key=key, data=image_bytes, content_type="image/png")
            image.output_storage_key = key
            image.output_url = output_ref
            image.output_mime_type = "image/png"
            image.output_checksum = checksum_sha256(image_bytes)

        if later_image > 0:
            # Batch /v1/images/edits rejects vision-purpose file_ids with HTTP_401
            # "Unable to authorize file access". Host the intermediate on Shopify CDN
            # and pass image_url into the next IMAGE stage (same as step 1).
            cdn_url = self._host_intermediate_image_url(image, image_bytes)
            image.output_url = cdn_url
            image.output_mime_type = "image/png"
            prev = image.current_openai_file_id
            image.current_openai_file_id = None
            if prev:
                self._mark_temp_file_pending_delete(prev)
            req.output_reference = cdn_url
        elif later_any > 0:
            # Later stages are DESCRIPTION (or non-image): vision file_id is correct.
            file_id = client.upload_vision_image(image_bytes)
            expires = datetime.now(timezone.utc) + timedelta(hours=settings.openai_temp_file_retention_hours)
            self.db.add(
                OpenAITemporaryFile(
                    shop_id=image.shop_id,
                    openai_file_id=file_id,
                    asset_type="intermediate_image",
                    workflow_step_id=req.workflow_step_id,
                    workflow_step_order=req.workflow_step_order,
                    batch_image_id=image.id,
                    expires_at=expires,
                    cleanup_status=OpenAITempFileCleanupStatus.ACTIVE,
                )
            )
            prev = image.current_openai_file_id
            image.current_openai_file_id = file_id
            if prev and prev != file_id:
                self._mark_temp_file_pending_delete(prev)
            req.output_reference = file_id
        elif is_last_image:
            req.output_reference = image.output_storage_key

        image.current_prompt_step = req.workflow_step_order
        image.error_code = None
        image.error_message = None
        if image.status == BatchImageStatus.WAITING_FOR_PROVIDER:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
            )
            image.status = BatchImageStatus.PROCESSING
        req.status = OpenAIBatchRequestStatus.COMPLETED
        req.completed_at = datetime.now(timezone.utc)

    def _import_description_success(self, req: OpenAIBatchRequest, body: dict[str, Any]) -> None:
        text = extract_text_from_responses_body(body)
        image = self.db.get(BatchImage, req.batch_image_id)
        if image is None:
            raise OpenAIBatchOrchestratorError("Batch image missing", code="BATCH_IMAGE_NOT_FOUND")
        prior = (image.pending_description_context or "").strip()
        image.pending_description_context = f"{prior}\n{text}".strip() if prior else text
        image.current_prompt_step = req.workflow_step_order
        image.error_code = None
        image.error_message = None
        if image.status == BatchImageStatus.WAITING_FOR_PROVIDER:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
            )
            image.status = BatchImageStatus.PROCESSING
        req.status = OpenAIBatchRequestStatus.COMPLETED
        req.output_text = text
        req.completed_at = datetime.now(timezone.utc)

    def _handle_terminal_failure(self, row: OpenAIBatch, client: OpenAIBatchClient) -> None:
        if row.openai_error_file_id or row.openai_output_file_id:
            # Partial results may still exist on expired batches
            try:
                self.import_batch_results(row, client)
                return
            except Exception:
                logger.exception("Partial import after terminal OpenAI batch failed | id=%s", row.id)
        reqs = self.db.query(OpenAIBatchRequest).filter(OpenAIBatchRequest.openai_batch_id == row.id).all()
        for req in reqs:
            if req.status == OpenAIBatchRequestStatus.COMPLETED:
                continue
            req.status = (
                OpenAIBatchRequestStatus.EXPIRED
                if row.status == OpenAIBatchStatus.EXPIRED
                else OpenAIBatchRequestStatus.FAILED
            )
            req.error_code = row.status.value.upper()
            req.error_message = row.error_message or f"OpenAI batch ended as {row.status.value}"
            req.completed_at = datetime.now(timezone.utc)
        self._create_retry_batch(row, [r for r in reqs if r.status != OpenAIBatchRequestStatus.COMPLETED])

    def _create_retry_batch(self, parent: OpenAIBatch, failed_reqs: list[OpenAIBatchRequest]) -> OpenAIBatch | None:
        retryable = [
            r
            for r in failed_reqs
            if r.status in {OpenAIBatchRequestStatus.FAILED, OpenAIBatchRequestStatus.EXPIRED}
        ]
        if not retryable:
            return None
        # Cap retries using processing_max_attempts on the image
        eligible: list[OpenAIBatchRequest] = []
        for req in retryable:
            image = self.db.get(BatchImage, req.batch_image_id)
            if image is None:
                continue
            next_attempt = req.attempt_number + 1
            if next_attempt > settings.processing_max_attempts:
                self._fail_image_permanently(image, req.error_code or "MAX_ATTEMPTS", req.error_message or "Max attempts")
                continue
            eligible.append(req)
        if not eligible:
            return None

        child = OpenAIBatch(
            shop_id=parent.shop_id,
            primary_batch_id=parent.primary_batch_id,
            workflow_step_id=parent.workflow_step_id,
            workflow_step_order=parent.workflow_step_order,
            step_type=parent.step_type,
            endpoint=parent.endpoint,
            model=parent.model,
            status=OpenAIBatchStatus.DRAFT,
            is_retry=True,
            parent_openai_batch_id=parent.id,
            request_count=0,
        )
        self.db.add(child)
        self.db.flush()

        lines: list[BatchLine] = []
        for old in eligible:
            image = self.db.get(BatchImage, old.batch_image_id)
            product = self.db.get(BatchProduct, old.batch_product_id)
            if image is None or product is None:
                continue
            attempt = old.attempt_number + 1
            custom_id = make_custom_id(
                shop_id=image.shop_id,
                batch_image_id=image.id,
                step_order=old.workflow_step_order,
                attempt=attempt,
            )
            body = self._build_body_for_image(
                image=image,
                step_type=parent.step_type,
                model=parent.model,
                prompt=self._resolve_prompt_text(image, old.workflow_step_order),
            )
            self.db.add(
                OpenAIBatchRequest(
                    openai_batch_id=child.id,
                    custom_id=custom_id,
                    batch_image_id=image.id,
                    batch_product_id=product.id,
                    source_media_gid=image.shopify_media_gid,
                    workflow_step_id=old.workflow_step_id,
                    workflow_step_order=old.workflow_step_order,
                    attempt_number=attempt,
                    status=OpenAIBatchRequestStatus.PENDING,
                    input_reference=body.get("_input_reference"),
                )
            )
            body.pop("_input_reference", None)
            lines.append(BatchLine(custom_id=custom_id, method="POST", url=parent.endpoint, body=body))
            image.attempt_count = max(image.attempt_count, attempt)
            if image.status != BatchImageStatus.WAITING_FOR_PROVIDER:
                assert_transition(
                    "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.WAITING_FOR_PROVIDER
                )
                image.status = BatchImageStatus.WAITING_FOR_PROVIDER

        if not lines:
            self.db.delete(child)
            return None

        client = self._client_or_create()
        jsonl = lines_to_jsonl(lines)
        file_id = client.upload_batch_jsonl(jsonl)
        child.openai_input_file_id = file_id
        child.request_count = len(lines)
        remote = client.create_batch(
            input_file_id=file_id,
            endpoint=parent.endpoint,
            metadata={"primary_batch_id": str(parent.primary_batch_id), "retry_of": str(parent.id)},
        )
        child.openai_batch_id = remote.id
        child.status = OpenAIBatchStatus(getattr(remote, "status", "validating"))
        child.submitted_at = datetime.now(timezone.utc)
        for req in self.db.query(OpenAIBatchRequest).filter(OpenAIBatchRequest.openai_batch_id == child.id).all():
            req.status = OpenAIBatchRequestStatus.SUBMITTED
        self._set_primary_phase(parent.primary_batch_id, ProcessingPhase.RETRYING_FAILED_REQUESTS, active=child.id)
        self.db.flush()
        return child

    def _reuse_existing_generated_outputs(self) -> int:
        """Complete unpublished work that already has a Shopify Files version."""
        products = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(
                BatchProduct.status.in_(
                    [
                        BatchProductStatus.QUEUED,
                        BatchProductStatus.PROCESSING,
                        BatchProductStatus.RETRYING,
                        BatchProductStatus.FAILED,
                    ]
                )
            )
            .order_by(BatchProduct.created_at.asc())
            .limit(50)
            .all()
        )
        retry = RetryService(self.db)
        healed = 0
        batch_ids: set[UUID] = set()
        for product in products:
            if not retry.complete_unpublished_generated_work(product):
                continue
            healed += 1
            batch_ids.add(product.batch_id)
        if not healed:
            return 0
        self.db.commit()
        for batch_id in batch_ids:
            batch = self.db.get(ProcessingBatch, batch_id)
            if batch is None:
                continue
            shop = self.db.get(Shop, batch.shop_id)
            if shop is None:
                continue
            PrimaryBatchService(self.db, shop).refresh_batch_counters(batch)
            if batch.status in {BatchStatus.COMPLETED, BatchStatus.PARTIALLY_COMPLETED}:
                batch.processing_phase = ProcessingPhase.READY_TO_PUBLISH.value
            self.db.commit()
        return healed

    # --------------------------------------------------------------- submit
    def submit_ready_stages(self, *, worker_id: str) -> int:
        batches = (
            self.db.query(ProcessingBatch)
            .filter(ProcessingBatch.status.in_([BatchStatus.QUEUED, BatchStatus.PROCESSING]))
            .order_by(ProcessingBatch.created_at.asc())
            .all()
        )
        submitted = 0
        for batch in batches:
            try:
                # Skip if an OpenAI batch is still in flight for this primary batch
                inflight = (
                    self.db.query(OpenAIBatch)
                    .filter(
                        OpenAIBatch.primary_batch_id == batch.id,
                        OpenAIBatch.status.in_(list(IN_FLIGHT_OPENAI_STATUSES)),
                    )
                    .first()
                )
                if inflight is not None:
                    continue
                created = self._submit_next_stage(batch, worker_id=worker_id)
                if created:
                    submitted += 1
                    self.db.commit()
            except OpenAIBatchOrchestratorError as exc:
                self.db.rollback()
                batch = self.db.get(ProcessingBatch, batch.id)
                if batch is not None:
                    batch.error_summary = str(exc)
                    if exc.code in {"OPENAI_BATCH_DISABLED", "OPENAI_NOT_CONFIGURED", "INVALID_AI_EXECUTION_MODE"}:
                        # Block the job clearly; do not silent-fallback
                        pass
                    self.db.commit()
                logger.error("Stage submit blocked | batch=%s code=%s error=%s", batch.id if batch else None, exc.code, exc)
            except Exception:
                self.db.rollback()
                logger.exception("Stage submit failed | batch=%s", batch.id)
        return submitted

    def _submit_next_stage(self, batch: ProcessingBatch, *, worker_id: str) -> OpenAIBatch | bool | None:
        shop = self.db.get(Shop, batch.shop_id)
        if shop is None:
            return None
        products = (
            self.db.query(BatchProduct)
            .options(selectinload(BatchProduct.images))
            .filter(BatchProduct.batch_id == batch.id)
            .all()
        )
        ready: list[tuple[BatchProduct, BatchImage, StageTarget]] = []
        for product in products:
            if product.status in {BatchProductStatus.FAILED, BatchProductStatus.SKIPPED, BatchProductStatus.COMPLETED}:
                continue
            if RetryService(self.db).complete_unpublished_generated_work(product):
                continue
            for image in product.images:
                if image.status in {BatchImageStatus.COMPLETED, BatchImageStatus.FAILED, BatchImageStatus.UPLOADING}:
                    continue
                version = find_generated_version_for_batch_image(self.db, image)
                if version is not None and version.shopify_file_gid:
                    complete_image_from_shopify_file(
                        image,
                        file_gid=version.shopify_file_gid,
                        cdn_url=version.shopify_cdn_url,
                        version_id=version.id,
                    )
                    continue
                target = self._next_stage_for_image(shop, product, image)
                if should_skip_openai_for_image(
                    self.db,
                    image,
                    next_stage_exists=target is not None,
                ):
                    # Local/Shopify output already exists — finalize owns the rest.
                    # Do not mark COMPLETED here without an image_version row.
                    continue
                if target is None:
                    continue
                ready.append((product, image, target))

        if not ready:
            # Prompt/config failures mark products FAILED above; refresh so the Primary
            # batch does not remain stuck in QUEUED with no OpenAI work left.
            PrimaryBatchService(self.db, shop).refresh_batch_counters(batch)
            return None

        from app.services.ai_provider import skip_ai_provider_call

        if skip_ai_provider_call():
            self._passthrough_ready_without_openai(batch, shop, ready, worker_id=worker_id)
            return True

        # Prefer lowest step order; one step_type per OpenAI batch
        min_step = min(t.step_order for _, _, t in ready)
        cohort = [(p, i, t) for p, i, t in ready if t.step_order == min_step]
        step_type = cohort[0][2].step_type
        cohort = [(p, i, t) for p, i, t in cohort if t.step_type == step_type]
        if len(cohort) > settings.openai_batch_max_requests:
            cohort = cohort[: settings.openai_batch_max_requests]

        if batch.status == BatchStatus.QUEUED:
            assert_transition("batch", BATCH_TRANSITIONS, batch.status, BatchStatus.PROCESSING)
            batch.status = BatchStatus.PROCESSING
            batch.started_at = batch.started_at or datetime.now(timezone.utc)

        batch.processing_phase = ProcessingPhase.PREPARING_OPENAI_STAGE.value
        batch.current_workflow_step = min_step
        batch.total_workflow_steps = max(batch.total_workflow_steps, max(t.step_order for _, _, t in cohort))

        endpoint = IMAGE_EDITS_ENDPOINT if step_type == PromptStepType.IMAGE else RESPONSES_ENDPOINT
        model = settings.openai_image_model if step_type == PromptStepType.IMAGE else settings.openai_text_model
        openai_batch = OpenAIBatch(
            shop_id=batch.shop_id,
            primary_batch_id=batch.id,
            workflow_step_id=cohort[0][2].step_id,
            workflow_step_order=min_step,
            step_type=step_type,
            endpoint=endpoint,
            model=model,
            status=OpenAIBatchStatus.DRAFT,
            is_retry=False,
        )
        self.db.add(openai_batch)
        self.db.flush()

        lines: list[BatchLine] = []
        now = datetime.now(timezone.utc)
        for product, image, target in cohort:
            if product.status == BatchProductStatus.QUEUED:
                assert_transition(
                    "batch_product", BATCH_PRODUCT_TRANSITIONS, product.status, BatchProductStatus.PROCESSING
                )
                product.status = BatchProductStatus.PROCESSING
                product.locked_by = worker_id
                product.locked_at = now
                product.claimed_at = product.claimed_at or now
                product.started_at = product.started_at or now

            attempt = next_processing_attempt_number(
                self.db,
                image.id,
                start_from=max(image.attempt_count, 0) + 1,
            )
            if image.status in {BatchImageStatus.QUEUED, BatchImageStatus.RETRYING, BatchImageStatus.PROCESSING}:
                # Move through allowed transitions toward WAITING_FOR_PROVIDER
                if image.status == BatchImageStatus.QUEUED:
                    assert_transition(
                        "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.DOWNLOADING
                    )
                    image.status = BatchImageStatus.DOWNLOADING
                if image.status == BatchImageStatus.DOWNLOADING:
                    assert_transition(
                        "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
                    )
                    image.status = BatchImageStatus.PROCESSING
                if image.status == BatchImageStatus.RETRYING:
                    assert_transition(
                        "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.DOWNLOADING
                    )
                    image.status = BatchImageStatus.DOWNLOADING
                    assert_transition(
                        "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
                    )
                    image.status = BatchImageStatus.PROCESSING
                assert_transition(
                    "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.WAITING_FOR_PROVIDER
                )
                image.status = BatchImageStatus.WAITING_FOR_PROVIDER

            image.attempt_count = attempt
            image.started_at = image.started_at or now
            image.current_prompt_step = max(image.current_prompt_step, target.step_order - 1)
            self.db.add(
                ProcessingAttempt(
                    batch_image_id=image.id,
                    batch_product_id=product.id,
                    attempt_number=attempt,
                    status=AttemptStatus.STARTED,
                    provider="openai_batch",
                    shopify_source_url=image.cdn_url,
                )
            )

            prompt = target.rendered_prompt
            if image.pending_description_context and step_type == PromptStepType.IMAGE:
                prompt = f"{prompt}\n\nDescription context:\n{image.pending_description_context}"

            body = self._build_body_for_image(
                image=image,
                step_type=step_type,
                model=model,
                prompt=prompt,
            )
            custom_id = make_custom_id(
                shop_id=image.shop_id,
                batch_image_id=image.id,
                step_order=target.step_order,
                attempt=attempt,
            )
            input_ref = body.pop("_input_reference", None)
            self.db.add(
                OpenAIBatchRequest(
                    openai_batch_id=openai_batch.id,
                    custom_id=custom_id,
                    batch_image_id=image.id,
                    batch_product_id=product.id,
                    source_media_gid=image.shopify_media_gid,
                    workflow_step_id=target.step_id,
                    workflow_step_order=target.step_order,
                    attempt_number=attempt,
                    status=OpenAIBatchRequestStatus.PENDING,
                    input_reference=input_ref,
                )
            )
            lines.append(BatchLine(custom_id=custom_id, method="POST", url=endpoint, body=body))

            # Snapshot prompts once
            if product.prompt_snapshot_json is None:
                try:
                    resolved = PromptResolver(self.db, shop).resolve_for_product(
                        self.db.get(Product, product.product_id) if product.product_id else None,
                        image=image,
                    )
                    product.prompt_snapshot_json = PromptResolver(self.db, shop).to_snapshot(resolved)
                except PromptResolverError:
                    pass

        if not lines:
            self.db.delete(openai_batch)
            return None

        batch.processing_phase = ProcessingPhase.UPLOADING_BATCH_INPUT.value
        client = self._client_or_create()
        jsonl = lines_to_jsonl(lines)
        file_id = client.upload_batch_jsonl(jsonl, filename=f"primary_{batch.id}_step_{min_step}.jsonl")
        openai_batch.openai_input_file_id = file_id
        openai_batch.request_count = len(lines)

        batch.processing_phase = ProcessingPhase.OPENAI_BATCH_SUBMITTED.value
        remote = client.create_batch(
            input_file_id=file_id,
            endpoint=endpoint,
            metadata={"primary_batch_id": str(batch.id), "step": str(min_step)},
        )
        openai_batch.openai_batch_id = remote.id
        openai_batch.status = OpenAIBatchStatus(getattr(remote, "status", "validating"))
        openai_batch.submitted_at = datetime.now(timezone.utc)
        expires = getattr(remote, "expires_at", None)
        if expires:
            openai_batch.expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)

        for req in self.db.query(OpenAIBatchRequest).filter(OpenAIBatchRequest.openai_batch_id == openai_batch.id).all():
            req.status = OpenAIBatchRequestStatus.SUBMITTED

        batch.processing_phase = ProcessingPhase.WAITING_FOR_OPENAI.value
        batch.active_openai_batch_id = openai_batch.id
        batch.openai_requests_total = len(lines)
        batch.openai_requests_completed = 0
        batch.openai_requests_failed = 0
        PrimaryBatchService(self.db, shop).refresh_batch_counters(batch)
        self.db.flush()
        logger.info(
            "Submitted OpenAI Batch | primary=%s openai=%s step=%s type=%s requests=%s",
            batch.id,
            openai_batch.openai_batch_id,
            min_step,
            step_type.value,
            len(lines),
        )
        return openai_batch

    def _transition_image_to_processing(self, image: BatchImage) -> None:
        if image.status == BatchImageStatus.QUEUED:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.DOWNLOADING
            )
            image.status = BatchImageStatus.DOWNLOADING
        if image.status == BatchImageStatus.RETRYING:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.DOWNLOADING
            )
            image.status = BatchImageStatus.DOWNLOADING
        if image.status == BatchImageStatus.DOWNLOADING:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
            )
            image.status = BatchImageStatus.PROCESSING
        if image.status == BatchImageStatus.WAITING_FOR_PROVIDER:
            assert_transition(
                "batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.PROCESSING
            )
            image.status = BatchImageStatus.PROCESSING

    def _passthrough_ready_without_openai(
        self,
        batch: ProcessingBatch,
        shop: Shop,
        ready: list[tuple[BatchProduct, BatchImage, StageTarget]],
        *,
        worker_id: str,
    ) -> None:
        """Load-test path: download Shopify source, skip OpenAI, save local PNG for Files upload."""
        from pathlib import Path

        from app.services.ai_provider import skip_ai_output_bytes
        from app.services.image_processor import ProcessingError, download_shopify_cdn_to_temp

        now = datetime.now(timezone.utc)
        if batch.status == BatchStatus.QUEUED:
            assert_transition("batch", BATCH_TRANSITIONS, batch.status, BatchStatus.PROCESSING)
            batch.status = BatchStatus.PROCESSING
            batch.started_at = batch.started_at or now

        batch.processing_phase = ProcessingPhase.PREPARING_OPENAI_STAGE.value
        storage = get_output_storage()

        for product, image, target in ready:
            if product.status == BatchProductStatus.QUEUED:
                assert_transition(
                    "batch_product",
                    BATCH_PRODUCT_TRANSITIONS,
                    product.status,
                    BatchProductStatus.PROCESSING,
                )
                product.status = BatchProductStatus.PROCESSING
                product.locked_by = worker_id
                product.locked_at = now
                product.claimed_at = product.claimed_at or now
                product.started_at = product.started_at or now

            attempt_number = next_processing_attempt_number(
                self.db,
                image.id,
                start_from=max(image.attempt_count, 0) + 1,
            )
            self._transition_image_to_processing(image)
            image.attempt_count = attempt_number
            image.started_at = image.started_at or now

            attempt = ProcessingAttempt(
                batch_image_id=image.id,
                batch_product_id=product.id,
                attempt_number=attempt_number,
                status=AttemptStatus.STARTED,
                provider="skip_ai",
                shopify_source_url=image.cdn_url,
            )
            self.db.add(attempt)
            self.db.flush()

            temp_path: Path | None = None
            try:
                if not image.cdn_url:
                    raise ProcessingError("Missing Shopify CDN URL", code="INVALID_CDN_URL", retryable=False)
                temp_path = download_shopify_cdn_to_temp(image.cdn_url)
                output_bytes = skip_ai_output_bytes(temp_path.read_bytes())
                key = f"{image.shop_id}/{product.batch_id}/{image.id}/output.png"
                output_ref = storage.save_bytes(key=key, data=output_bytes, content_type="image/png")
                image.output_storage_key = key
                image.output_url = output_ref
                image.output_mime_type = "image/png"
                image.output_checksum = checksum_sha256(output_bytes)
                steps = self._resolved_steps_for_image(image)
                last_order = max((step.step_order for step in steps), default=target.step_order)
                image.current_prompt_step = last_order
                if any(
                    getattr(step, "step_type", PromptStepType.IMAGE) == PromptStepType.DESCRIPTION
                    for step in steps
                ):
                    image.pending_description_context = "skipped (SKIP_AI_PROVIDER_CALL)"
                image.error_code = None
                image.error_message = None
                attempt.status = AttemptStatus.COMPLETED
                attempt.output_storage_key = key
                attempt.completed_at = datetime.now(timezone.utc)
                logger.info(
                    "Skipped OpenAI call | SKIP_AI_PROVIDER_CALL=true | image=%s primary=%s output_bytes=%s",
                    image.id,
                    batch.id,
                    len(output_bytes),
                )
            except ProcessingError as exc:
                attempt.status = AttemptStatus.FAILED
                attempt.error_code = exc.code
                attempt.error_message = str(exc)
                attempt.completed_at = datetime.now(timezone.utc)
                RetryService(self.db).schedule_image_retry(
                    image,
                    product,
                    error_code=exc.code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                )
            except Exception:
                logger.exception("Skip-AI passthrough failed | image=%s", image.id)
                attempt.status = AttemptStatus.FAILED
                attempt.error_code = "PROCESSING_ERROR"
                attempt.error_message = "Skip-AI passthrough failed"
                attempt.completed_at = datetime.now(timezone.utc)
                RetryService(self.db).schedule_image_retry(
                    image,
                    product,
                    error_code="PROCESSING_ERROR",
                    error_message="Skip-AI passthrough failed",
                    retryable=True,
                )
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Failed to delete temp CDN file | path=%s", temp_path)

            if product.prompt_snapshot_json is None:
                try:
                    resolved = PromptResolver(self.db, shop).resolve_for_product(
                        self.db.get(Product, product.product_id) if product.product_id else None,
                        image=image,
                    )
                    product.prompt_snapshot_json = PromptResolver(self.db, shop).to_snapshot(resolved)
                except PromptResolverError:
                    pass

        PrimaryBatchService(self.db, shop).refresh_batch_counters(batch)
        self.db.flush()

    # ------------------------------------------------------------- finalize
    def finalize_ready_images(self, *, worker_id: str) -> int:
        """Upload final local outputs to Shopify Files using existing processor path."""
        from app.services.image_processor import ImageProcessor

        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        eligible = and_(
            BatchImage.status.in_(
                [
                    BatchImageStatus.PROCESSING,
                    BatchImageStatus.WAITING_FOR_PROVIDER,
                    BatchImageStatus.UPLOADING,
                    BatchImageStatus.RETRYING,
                ]
            ),
            or_(
                BatchImage.output_storage_key.is_not(None),
                # GID persisted after Shopify upload but version/complete not finished yet.
                and_(
                    BatchImage.generated_shopify_file_gid.is_not(None),
                    BatchImage.generated_image_version_id.is_(None),
                ),
            ),
        )
        stmt = (
            select(BatchImage)
            .where(eligible)
            .order_by(BatchImage.created_at.asc())
            .limit(20)
        )
        if dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        else:
            stmt = stmt.with_for_update()
        images = list(self.db.execute(stmt).scalars().all())
        count = 0
        processor = ImageProcessor(self.db)
        for image in images:
            product = self.db.get(BatchProduct, image.batch_product_id)
            if product is None:
                continue
            # Do not finalize while later AI stages remain
            shop = self.db.get(Shop, image.shop_id)
            if shop is not None:
                nxt = self._next_stage_for_image(shop, product, image)
                if nxt is not None:
                    continue
            batch = self.db.get(ProcessingBatch, product.batch_id)
            if batch is not None:
                batch.processing_phase = ProcessingPhase.UPLOADING_TO_SHOPIFY_FILES.value
            try:
                self.db.commit()
                if processor.finalize_local_output(image.id, worker_id=worker_id):
                    count += 1
            except Exception:
                self.db.rollback()
                logger.exception("Finalize/upload failed | image=%s", image.id)
        return count

    def cleanup_temporary_files(self) -> int:
        from app.services.ai_provider import skip_ai_provider_call

        if skip_ai_provider_call():
            return 0
        now = datetime.now(timezone.utc)
        rows = (
            self.db.query(OpenAITemporaryFile)
            .filter(
                OpenAITemporaryFile.cleanup_status.in_(
                    [OpenAITempFileCleanupStatus.PENDING_DELETE, OpenAITempFileCleanupStatus.ACTIVE]
                )
            )
            .order_by(OpenAITemporaryFile.created_at.asc())
            .limit(50)
            .all()
        )
        client = self._client_or_create()
        cleaned = 0
        for row in rows:
            if row.cleanup_status == OpenAITempFileCleanupStatus.ACTIVE:
                if row.expires_at and row.expires_at > now:
                    continue
                # Still referenced as current input?
                image = self.db.get(BatchImage, row.batch_image_id)
                if image is not None and image.current_openai_file_id == row.openai_file_id:
                    continue
                # Pending/retry OpenAI requests still using this file
                pending = (
                    self.db.query(OpenAIBatchRequest)
                    .filter(
                        OpenAIBatchRequest.batch_image_id == row.batch_image_id,
                        OpenAIBatchRequest.status.in_(
                            [OpenAIBatchRequestStatus.PENDING, OpenAIBatchRequestStatus.SUBMITTED]
                        ),
                        OpenAIBatchRequest.input_reference == row.openai_file_id,
                    )
                    .first()
                )
                if pending is not None:
                    continue
                row.cleanup_status = OpenAITempFileCleanupStatus.PENDING_DELETE
            try:
                client.delete_file(row.openai_file_id)
                row.cleanup_status = OpenAITempFileCleanupStatus.DELETED
                cleaned += 1
            except Exception as exc:
                row.cleanup_attempts += 1
                row.cleanup_status = OpenAITempFileCleanupStatus.DELETE_FAILED
                row.last_cleanup_error = str(exc)
            self.db.commit()
        return cleaned

    # --------------------------------------------------------------- helpers
    def _set_primary_phase(
        self, primary_batch_id: UUID, phase: ProcessingPhase, *, active: UUID | None = None
    ) -> None:
        batch = self.db.get(ProcessingBatch, primary_batch_id)
        if batch is None:
            return
        batch.processing_phase = phase.value
        if active is not None:
            batch.active_openai_batch_id = active

    def _mark_temp_file_pending_delete(self, openai_file_id: str) -> None:
        row = (
            self.db.query(OpenAITemporaryFile)
            .filter(OpenAITemporaryFile.openai_file_id == openai_file_id)
            .first()
        )
        if row is not None and row.cleanup_status == OpenAITempFileCleanupStatus.ACTIVE:
            row.cleanup_status = OpenAITempFileCleanupStatus.PENDING_DELETE

    def _fail_image_permanently(self, image: BatchImage, code: str, message: str) -> None:
        if image.status not in {BatchImageStatus.FAILED, BatchImageStatus.COMPLETED}:
            assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.FAILED)
            image.status = BatchImageStatus.FAILED
        image.error_code = code
        image.error_message = message
        image.completed_at = datetime.now(timezone.utc)
        product = self.db.get(BatchProduct, image.batch_product_id)
        if product is not None and product.status == BatchProductStatus.PROCESSING:
            assert_transition(
                "batch_product", BATCH_PRODUCT_TRANSITIONS, product.status, BatchProductStatus.FAILED
            )
            product.status = BatchProductStatus.FAILED
            product.error_code = code
            product.error_message = message
            product.completed_at = datetime.now(timezone.utc)
            product.locked_by = None
            product.locked_at = None

    def _host_intermediate_image_url(self, image: BatchImage, image_bytes: bytes) -> str:
        """Upload an intermediate PNG to Shopify Files and return a public CDN URL."""
        from app.core.shop_resolver import create_shopify_graphql_client
        from app.services.shopify_file_upload import PublishUploadError, ShopifyFileUploadService

        shop = self.db.get(Shop, image.shop_id)
        if shop is None:
            raise OpenAIBatchOrchestratorError(
                "Shop missing for intermediate image upload",
                code="SHOP_NOT_FOUND",
            )
        storage = get_output_storage()
        key = f"{image.shop_id}/intermediate/{image.id}/step_{image.current_prompt_step or 0}.png"
        storage.save_bytes(key=key, data=image_bytes, content_type="image/png")
        path = storage.resolve_path(key)
        try:
            client = create_shopify_graphql_client(self.db, shop)
            result = ShopifyFileUploadService(client).upload_png(
                path=path,
                filename=f"aone-intermediate-{image.id}.png",
                existing_file_gid=None,
            )
        except (PublishUploadError, RuntimeError) as exc:
            code = getattr(exc, "code", None) or "INTERMEDIATE_UPLOAD_FAILED"
            raise OpenAIBatchOrchestratorError(str(exc), code=str(code)) from exc
        finally:
            storage.delete(key)

        cdn_url = (result or {}).get("cdn_url") if isinstance(result, dict) else None
        if not cdn_url:
            raise OpenAIBatchOrchestratorError(
                "Intermediate Shopify upload returned no CDN URL",
                code="INTERMEDIATE_CDN_MISSING",
            )
        logger.info(
            "Hosted intermediate IMAGE step output on Shopify CDN | image=%s url=%s",
            image.id,
            cdn_url,
        )
        return str(cdn_url)

    @staticmethod
    def _public_image_url(image: BatchImage) -> str | None:
        """Prefer intermediate HTTPS URL from a prior IMAGE step; else source CDN."""
        for candidate in (image.output_url, image.generated_shopify_cdn_url, image.cdn_url):
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        return None

    def _build_body_for_image(
        self,
        *,
        image: BatchImage,
        step_type: PromptStepType,
        model: str,
        prompt: str,
    ) -> dict[str, Any]:
        if step_type == PromptStepType.IMAGE:
            # Never send OpenAI vision file_ids into Batch images/edits (HTTP_401).
            image_url = self._public_image_url(image)
            if not image_url:
                raise OpenAIBatchOrchestratorError(
                    "No public image URL available for IMAGE batch step",
                    code="IMAGE_URL_MISSING",
                )
            body = build_image_edit_body(
                model=model,
                prompt=prompt,
                image_url=image_url,
                file_id=None,
                transparent_background=settings.openai_transparent_background,
            )
            body["_input_reference"] = image_url
            return body

        file_id = image.current_openai_file_id
        image_url = None if file_id else self._public_image_url(image)
        body = build_description_body(
            model=model,
            prompt=prompt,
            image_url=image_url,
            file_id=file_id,
            prior_description=image.pending_description_context,
        )
        body["_input_reference"] = file_id or image_url
        return body

    def _resolve_prompt_text(self, image: BatchImage, step_order: int) -> str:
        product = self.db.get(BatchProduct, image.batch_product_id)
        if product is None:
            return ""
        snap = product.prompt_snapshot_json
        if isinstance(snap, list):
            for item in snap:
                if isinstance(item, dict) and int(item.get("step") or 0) == step_order:
                    return str(item.get("prompt") or item.get("promptTemplate") or "")
        shop = self.db.get(Shop, image.shop_id)
        if shop is None:
            return ""
        try:
            resolved = PromptResolver(self.db, shop).resolve_for_product(
                self.db.get(Product, product.product_id) if product.product_id else None,
                image=image,
            )
            for step in resolved:
                if step.step_order == step_order:
                    return step.rendered_prompt
        except PromptResolverError:
            return ""
        return ""

    def _workflow_step_count_for_image(self, image: BatchImage) -> int:
        product = self.db.get(BatchProduct, image.batch_product_id)
        if product is None:
            return 0
        snap = product.prompt_snapshot_json
        if isinstance(snap, list) and snap:
            return len(snap)
        shop = self.db.get(Shop, image.shop_id)
        if shop is None:
            return 0
        try:
            resolved = PromptResolver(self.db, shop).resolve_for_product(
                self.db.get(Product, product.product_id) if product.product_id else None,
                image=image,
            )
            return len(resolved)
        except PromptResolverError:
            return 0

    def _remaining_image_steps_after(self, image: BatchImage, step_order: int) -> int:
        steps = self._resolved_steps_for_image(image)
        return sum(
            1
            for step in steps
            if step.step_order > step_order
            and getattr(step, "step_type", PromptStepType.IMAGE) == PromptStepType.IMAGE
        )

    def _remaining_steps_after(self, image: BatchImage, step_order: int) -> int:
        steps = self._resolved_steps_for_image(image)
        return sum(1 for step in steps if step.step_order > step_order)

    def _resolved_steps_for_image(self, image: BatchImage) -> list[ResolvedPromptStep]:
        product = self.db.get(BatchProduct, image.batch_product_id)
        shop = self.db.get(Shop, image.shop_id)
        if product is None or shop is None:
            return []
        try:
            return PromptResolver(self.db, shop).resolve_for_product(
                self.db.get(Product, product.product_id) if product.product_id else None,
                image=image,
            )
        except PromptResolverError:
            return []

    def _next_stage_for_image(
        self, shop: Shop, product: BatchProduct, image: BatchImage
    ) -> StageTarget | None:
        try:
            override = image.prompt_override_json or product.prompt_override_json
            resolver = PromptResolver(self.db, shop)
            if isinstance(override, list) and override:
                resolved = resolver.resolve_from_override(
                    override,
                    product=self.db.get(Product, product.product_id) if product.product_id else None,
                    product_type_display="product",
                    image=image,
                )
            else:
                resolved = resolver.resolve_for_product(
                    self.db.get(Product, product.product_id) if product.product_id else None,
                    image=image,
                )
        except PromptResolverError as exc:
            image.error_code = exc.code
            image.error_message = str(exc)
            if image.status not in {BatchImageStatus.FAILED}:
                assert_transition("batch_image", BATCH_IMAGE_TRANSITIONS, image.status, BatchImageStatus.FAILED)
                image.status = BatchImageStatus.FAILED
            image.completed_at = image.completed_at or datetime.now(timezone.utc)
            if product.status in {BatchProductStatus.QUEUED, BatchProductStatus.PROCESSING}:
                assert_transition(
                    "batch_product",
                    BATCH_PRODUCT_TRANSITIONS,
                    product.status,
                    BatchProductStatus.FAILED,
                )
                product.status = BatchProductStatus.FAILED
                product.error_code = exc.code
                product.error_message = str(exc)
                product.completed_at = datetime.now(timezone.utc)
                product.locked_by = None
                product.locked_at = None
            return None

        next_order = image.current_prompt_step + 1
        for step in resolved:
            if step.step_order == next_order:
                step_type = getattr(step, "step_type", PromptStepType.IMAGE) or PromptStepType.IMAGE
                step_id = getattr(step, "step_id", None)
                return StageTarget(
                    step_order=step.step_order,
                    step_type=step_type,
                    step_id=step_id,
                    rendered_prompt=step.rendered_prompt,
                    name=step.name,
                )
        return None

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.config import settings
from app.poc.auth import require_shopify_jwt
from app.poc.cleanup import cleanup_expired_jobs
from app.poc.job_store import JobRecord, poc_job_store
from app.poc.processor import process_job, validate_retryable
from app.poc.schemas import ErrorEnvelope, JobProgress, JobStatus, PromptInput, PromptStep, SuccessEnvelope

logger = logging.getLogger("app.poc.router")

router = APIRouter(prefix="/api/poc/image-enhancement", tags=["poc-image-enhancement"])

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpg", "image/jpeg", "image/webp"}


def _feature_guard() -> None:
    if not settings.poc_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POC is disabled. Set POC_ENABLED=true.",
        )


def _job_to_progress(job: JobRecord) -> JobProgress:
    steps = []
    for step in job.steps:
        steps.append(
            PromptStep(
                step=step.step,
                prompt=step.prompt,
                status=step.status,
                outputUrl=(
                    f"/api/poc/image-enhancement/jobs/{job.job_id}/steps/{step.step}/image"
                    if step.output_file
                    else None
                ),
                errorMessage=step.error_message,
            )
        )
    return JobProgress(
        jobId=job.job_id,
        status=job.status,
        totalSteps=job.total_steps,
        completedSteps=job.completed_steps,
        steps=steps,
        failedStep=job.failed_step,
    )


@router.post("/jobs", response_model=SuccessEnvelope, dependencies=[Depends(require_shopify_jwt)])
async def create_job(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    prompts: str = Form(...),
):
    _feature_guard()
    cleanup_expired_jobs()

    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported image type")

    image_bytes = await image.read()
    max_bytes = max(settings.poc_max_image_size_mb, 1) * 1024 * 1024
    if len(image_bytes) > max_bytes:
        raise HTTPException(status_code=400, detail="Image exceeds max size")

    try:
        prompts_payload = json.loads(prompts)
        prompt_inputs = [PromptInput.model_validate(item) for item in prompts_payload]
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid prompts payload: {exc}") from exc

    if not prompt_inputs:
        raise HTTPException(status_code=400, detail="At least one prompt step is required")
    if len(prompt_inputs) > settings.poc_max_prompt_steps:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.poc_max_prompt_steps} prompt steps are allowed",
        )

    for index, item in enumerate(prompt_inputs, start=1):
        if item.step != index:
            raise HTTPException(status_code=400, detail="Prompt steps must be sequential")

    job = poc_job_store.create_job(
        original_bytes=image_bytes,
        mime_type=image.content_type or "image/png",
        prompts=prompt_inputs,
    )
    logger.info(
        "Job created | job=%s steps=%s image_bytes=%s mime=%s prompts=%r",
        job.job_id,
        len(prompt_inputs),
        len(image_bytes),
        image.content_type,
        [item.prompt for item in prompt_inputs],
    )
    background_tasks.add_task(process_job, job, 1)
    progress = _job_to_progress(job)
    return SuccessEnvelope(
        message="Image enhancement job started",
        data={
            "jobId": progress.job_id,
            "status": progress.status,
            "totalSteps": progress.total_steps,
            "completedSteps": progress.completed_steps,
        },
    )


@router.get("/jobs/{job_id}", response_model=SuccessEnvelope, dependencies=[Depends(require_shopify_jwt)])
def get_job(job_id: str):
    _feature_guard()
    cleanup_expired_jobs()
    job = poc_job_store.get_job(job_id)
    if not job:
        logger.warning("Job status — not found | job=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")
    progress = _job_to_progress(job)
    logger.info(
        "Job status | job=%s status=%s completed=%s/%s failed_step=%s",
        job_id,
        progress.status.value,
        progress.completed_steps,
        progress.total_steps,
        progress.failed_step,
    )
    return SuccessEnvelope(data=progress.model_dump(by_alias=True))


@router.get(
    "/jobs/{job_id}/steps/{step_number}/image",
    dependencies=[Depends(require_shopify_jwt)],
)
def get_step_image(job_id: str, step_number: int):
    _feature_guard()
    job = poc_job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    step = next((s for s in job.steps if s.step == step_number), None)
    if not step or not step.output_file:
        raise HTTPException(status_code=404, detail="Step output not found")
    path = Path(step.output_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Step output file missing")
    return FileResponse(path=str(path), media_type="image/png")


@router.get(
    "/jobs/{job_id}/final/download",
    dependencies=[Depends(require_shopify_jwt)],
)
def download_final_image(job_id: str):
    _feature_guard()
    job = poc_job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Job is not completed")

    final_file = job.final_output_file
    if not final_file:
        raise HTTPException(status_code=404, detail="Final output file not found")
    path = Path(final_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Final output file missing")
    return FileResponse(
        path=str(path),
        media_type="image/png",
        filename=f"{job_id}-final.png",
    )


@router.post(
    "/jobs/{job_id}/retry",
    response_model=SuccessEnvelope,
    dependencies=[Depends(require_shopify_jwt)],
)
def retry_job(job_id: str, payload: dict, background_tasks: BackgroundTasks):
    _feature_guard()
    job = poc_job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from_step = int(payload.get("fromStep", 1))
    try:
        validate_retryable(job, from_step)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    poc_job_store.reset_for_retry(job, from_step=from_step)
    background_tasks.add_task(process_job, job, from_step)
    return SuccessEnvelope(
        message=f"Retry started from step {from_step}",
        data=_job_to_progress(job).model_dump(by_alias=True),
    )

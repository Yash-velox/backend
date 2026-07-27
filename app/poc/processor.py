from __future__ import annotations

import logging
import time
from pathlib import Path

from app.poc.job_store import JobRecord, StepRecord, poc_job_store
from app.poc.openai_client import OpenAIImageClient, OpenAIImageError
from app.poc.schemas import JobStatus, StepStatus

logger = logging.getLogger("app.poc.processor")


def process_job(job: JobRecord, from_step: int = 1) -> None:
    logger.info(
        "Job processing start | job=%s from_step=%s total_steps=%s",
        job.job_id,
        from_step,
        job.total_steps,
    )
    started = time.perf_counter()
    try:
        client = OpenAIImageClient()
    except OpenAIImageError as exc:
        logger.error("Job aborted — OpenAI client unavailable | job=%s error=%s", job.job_id, exc)
        poc_job_store.mark_job_failed(job, from_step, str(exc))
        return

    input_bytes = _load_start_input(job, from_step)
    for step in job.steps:
        if step.step < from_step:
            continue
        _run_step(job, step, client, input_bytes)
        if step.status == StepStatus.FAILED:
            logger.error(
                "Job failed | job=%s failed_step=%s error=%s elapsed_s=%.2f",
                job.job_id,
                step.step,
                step.error_message,
                time.perf_counter() - started,
            )
            return
        if not step.output_file:
            poc_job_store.mark_job_failed(
                job,
                step.step,
                "Image generation finished without a step output.",
            )
            logger.error("Job failed — missing step output file | job=%s step=%s", job.job_id, step.step)
            return
        input_bytes = Path(step.output_file).read_bytes()

    poc_job_store.mark_job_completed_if_done(job)
    logger.info(
        "Job processing finished | job=%s status=%s elapsed_s=%.2f",
        job.job_id,
        job.status.value,
        time.perf_counter() - started,
    )


def _run_step(
    job: JobRecord,
    step: StepRecord,
    client: OpenAIImageClient,
    input_bytes: bytes,
) -> None:
    logger.info(
        "Step start | job=%s step=%s input_bytes=%s prompt_len=%s prompt=%r",
        job.job_id,
        step.step,
        len(input_bytes),
        len(step.prompt),
        step.prompt,
    )
    with job.lock:
        step.status = StepStatus.PROCESSING
        step.error_message = None

    try:
        output = client.edit_image(
            image_bytes=input_bytes,
            prompt=step.prompt,
            job_id=job.job_id,
            step=step.step,
        )
        if not output:
            raise OpenAIImageError("OpenAI returned empty image output")
        output_file = Path(job.original_file).parent / f"step-{step.step}.png"
        output_file.write_bytes(output)
        with job.lock:
            step.status = StepStatus.COMPLETED
            step.output_file = str(output_file)
        logger.info(
            "Step completed | job=%s step=%s output_file=%s output_bytes=%s",
            job.job_id,
            step.step,
            output_file.name,
            len(output),
        )
    except Exception as exc:
        logger.exception("Step failed | job=%s step=%s error=%s", job.job_id, step.step, exc)
        poc_job_store.mark_job_failed(job, step.step, str(exc))


def _load_start_input(job: JobRecord, from_step: int) -> bytes:
    if from_step <= 1:
        original = Path(job.original_file).read_bytes()
        logger.info("Loaded original input | job=%s bytes=%s", job.job_id, len(original))
        return original

    previous_step = _get_step(job, from_step - 1)
    if not previous_step or previous_step.status != StepStatus.COMPLETED:
        raise OpenAIImageError("Cannot retry: previous step has not completed")
    if not previous_step.output_file:
        raise OpenAIImageError("Cannot retry: previous step output is missing")
    data = Path(previous_step.output_file).read_bytes()
    logger.info(
        "Loaded previous step input | job=%s from_step=%s bytes=%s",
        job.job_id,
        from_step,
        len(data),
    )
    return data


def _get_step(job: JobRecord, step_number: int) -> StepRecord | None:
    for step in job.steps:
        if step.step == step_number:
            return step
    return None


def validate_retryable(job: JobRecord, from_step: int) -> None:
    if from_step < 1 or from_step > job.total_steps:
        raise OpenAIImageError("Retry step is out of range")
    if job.status != JobStatus.FAILED:
        raise OpenAIImageError("Retry is only available for failed jobs")

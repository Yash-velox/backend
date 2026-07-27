from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.poc.schemas import JobStatus, PromptInput, StepStatus


@dataclass
class StepRecord:
    step: int
    prompt: str
    status: StepStatus = StepStatus.WAITING
    output_file: str | None = None
    error_message: str | None = None


@dataclass
class JobRecord:
    job_id: str
    created_at: float
    status: JobStatus
    original_file: str
    steps: list[StepRecord]
    failed_step: int | None = None
    mime_type: str = "image/png"
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def completed_steps(self) -> int:
        return len([step for step in self.steps if step.status == StepStatus.COMPLETED])

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def final_output_file(self) -> str | None:
        for step in reversed(self.steps):
            if step.output_file:
                return step.output_file
        return None


class PocJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._global_lock = threading.Lock()

    @property
    def storage_root(self) -> Path:
        return Path(settings.poc_storage_dir)

    def create_job(self, *, original_bytes: bytes, mime_type: str, prompts: list[PromptInput]) -> JobRecord:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        job_id = f"poc_job_{uuid.uuid4().hex[:12]}"
        job_dir = self.storage_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        original_file = job_dir / "original.png"
        original_file.write_bytes(original_bytes)

        steps = [StepRecord(step=item.step, prompt=item.prompt.strip()) for item in prompts]
        record = JobRecord(
            job_id=job_id,
            created_at=time.time(),
            status=JobStatus.PROCESSING,
            original_file=str(original_file),
            steps=steps,
            mime_type=mime_type,
        )
        with self._global_lock:
            self._jobs[job_id] = record
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._global_lock:
            return self._jobs.get(job_id)

    def get_job_or_raise(self, job_id: str) -> JobRecord:
        record = self.get_job(job_id)
        if not record:
            raise KeyError(job_id)
        return record

    def mark_job_failed(self, record: JobRecord, step_number: int, message: str) -> None:
        with record.lock:
            record.status = JobStatus.FAILED
            record.failed_step = step_number
            for step in record.steps:
                if step.step == step_number:
                    step.status = StepStatus.FAILED
                    step.error_message = message
                elif step.step > step_number and step.status == StepStatus.WAITING:
                    step.status = StepStatus.WAITING

    def mark_job_completed_if_done(self, record: JobRecord) -> None:
        with record.lock:
            if all(step.status == StepStatus.COMPLETED for step in record.steps):
                record.status = JobStatus.COMPLETED
                record.failed_step = None

    def reset_for_retry(self, record: JobRecord, from_step: int) -> None:
        with record.lock:
            for step in record.steps:
                if step.step >= from_step:
                    step.status = StepStatus.WAITING
                    step.error_message = None
                    step.output_file = None
            record.status = JobStatus.PROCESSING
            record.failed_step = None

    def iter_jobs(self) -> list[JobRecord]:
        with self._global_lock:
            return list(self._jobs.values())

    def delete_job(self, job_id: str) -> None:
        with self._global_lock:
            self._jobs.pop(job_id, None)


poc_job_store = PocJobStore()

from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.config import settings
from app.poc.job_store import poc_job_store


def cleanup_expired_jobs() -> None:
    now = time.time()
    ttl_seconds = max(settings.poc_job_ttl_hours, 1) * 3600

    for record in poc_job_store.iter_jobs():
        if now - record.created_at <= ttl_seconds:
            continue
        _delete_job_dir(Path(record.original_file).parent)
        poc_job_store.delete_job(record.job_id)


def _delete_job_dir(path: Path) -> None:
    if path.exists() and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)

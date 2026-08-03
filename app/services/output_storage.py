from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Protocol

from app.config import settings

logger = logging.getLogger("app.services.output_storage")


class OutputStorage(Protocol):
    def save_bytes(self, *, key: str, data: bytes, content_type: str = "image/png") -> str:
        """Persist bytes and return a reference URL or path string."""

    def resolve_path(self, key: str) -> Path:
        """Resolve a storage key to a readable filesystem path (local impl)."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """Delete a storage key if present. Returns True when deleted."""


class LocalFilesystemOutputStorage:
    """Temporary processing output storage. Not durable Shopify CDN storage."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.processing_output_directory)
        self.root.mkdir(parents=True, exist_ok=True)

    def save_bytes(self, *, key: str, data: bytes, content_type: str = "image/png") -> str:
        safe_key = key.lstrip("/").replace("..", "")
        path = self.root / safe_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info(
            "Output saved | key=%s bytes=%s content_type=%s",
            safe_key,
            len(data),
            content_type,
        )
        return f"file://{path.resolve()}"

    def resolve_path(self, key: str) -> Path:
        safe_key = key.lstrip("/").replace("..", "")
        path = (self.root / safe_key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("Invalid storage key")
        return path

    def exists(self, key: str) -> bool:
        return self.resolve_path(key).is_file()

    def delete(self, key: str) -> bool:
        try:
            path = self.resolve_path(key)
        except ValueError:
            return False
        if path.is_file():
            path.unlink(missing_ok=True)
            logger.info("Output deleted | key=%s", key.lstrip("/").replace("..", ""))
            # Best-effort remove empty parents under root
            parent = path.parent
            root = self.root.resolve()
            while parent != root and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            return True
        return False


def checksum_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_output_storage() -> LocalFilesystemOutputStorage:
    return LocalFilesystemOutputStorage()


def cleanup_expired_temp_outputs(*, max_age_hours: float | None = None) -> dict[str, int]:
    """Delete abandoned local processing outputs older than retention. Never touches Shopify."""
    retention_h = max_age_hours if max_age_hours is not None else float(settings.processing_temp_retry_retention_hours)
    root = Path(settings.processing_output_directory)
    if not root.exists():
        return {"scanned": 0, "deleted": 0}
    cutoff = time.time() - (retention_h * 3600)
    scanned = 0
    deleted = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        scanned += 1
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        except OSError:
            logger.warning("Failed to cleanup temp output | path=%s", path.name)
    return {"scanned": scanned, "deleted": deleted}

from __future__ import annotations

import hashlib
import logging
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


class LocalFilesystemOutputStorage:
    """Development fallback output storage. Not the final production architecture."""

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


def checksum_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_output_storage() -> LocalFilesystemOutputStorage:
    return LocalFilesystemOutputStorage()

"""Local rembg background removal. Runs after OpenAI, before Shopify upload.

This is not an OpenAI call. It only writes a PNG alpha channel onto the already
enhanced image. The ONNX session is loaded lazily and reused so we do not pay
model-load cost (or extra RAM) on every image.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services.shopify_file_upload import png_has_alpha

logger = logging.getLogger("app.services.background_cutout")

# Keep inference on one thread. The UAT box is memory-tight; two concurrent
# rembg runs can OOM a ~1 GB instance.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("ORT_INTRA_OP_NUM_THREADS", "1")

_ALLOWED_MODELS = frozenset(
    {
        "u2netp",
        "u2net",
        "u2net_human_seg",
        "silueta",
        "isnet-general-use",
    }
)

_session_lock = threading.RLock()
_session: Any = None
_session_model: str | None = None


class CutoutError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CutoutResult:
    png_bytes: bytes
    applied: bool
    has_alpha: bool
    skipped_reason: str | None = None


def reset_cutout_session() -> None:
    """Test helper. Drops the cached ONNX session."""
    global _session, _session_model
    with _session_lock:
        _session = None
        _session_model = None


def _resolved_model() -> str:
    model = (settings.rembg_model or "u2netp").strip()
    if model not in _ALLOWED_MODELS:
        raise CutoutError(
            f"Unsupported rembg model '{model}'",
            code="CUTOUT_MODEL_INVALID",
            retryable=False,
        )
    return model


def _get_session() -> Any:
    global _session, _session_model
    model = _resolved_model()
    if _session is not None and _session_model == model:
        return _session
    try:
        from rembg import new_session
    except ImportError as exc:
        raise CutoutError(
            "rembg is not installed on this worker",
            code="REMBG_NOT_INSTALLED",
            retryable=False,
        ) from exc
    logger.info("Loading rembg session | model=%s", model)
    try:
        _session = new_session(model)
        _session_model = model
    except Exception as exc:
        raise CutoutError(
            "Failed to load rembg model",
            code="REMBG_SESSION_FAILED",
            retryable=True,
        ) from exc
    return _session


def apply_background_cutout(png_bytes: bytes) -> CutoutResult:
    """Return PNG bytes with an alpha channel, or skip when already transparent.

    Callers must not log ``png_bytes``. Inference is serialized so two products
    cannot load the model twice on a small worker.
    """
    if not settings.rembg_enabled:
        return CutoutResult(
            png_bytes=png_bytes,
            applied=False,
            has_alpha=png_has_alpha(png_bytes),
            skipped_reason="disabled",
        )
    if not png_bytes:
        raise CutoutError("Cut-out input is empty", code="CUTOUT_EMPTY_INPUT", retryable=False)
    if png_has_alpha(png_bytes):
        logger.info("Skipping rembg | source already has alpha")
        return CutoutResult(
            png_bytes=png_bytes,
            applied=False,
            has_alpha=True,
            skipped_reason="already_has_alpha",
        )

    with _session_lock:
        session = _get_session()
        try:
            from rembg import remove
        except ImportError as exc:
            raise CutoutError(
                "rembg is not installed on this worker",
                code="REMBG_NOT_INSTALLED",
                retryable=False,
            ) from exc
        try:
            output = remove(png_bytes, session=session)
        except MemoryError as exc:
            raise CutoutError(
                "rembg ran out of memory",
                code="REMBG_OUT_OF_MEMORY",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise CutoutError(
                "rembg failed to remove the background",
                code="REMBG_FAILED",
                retryable=True,
            ) from exc

    if not isinstance(output, (bytes, bytearray)):
        output = bytes(output)
    else:
        output = bytes(output)
    has_alpha = png_has_alpha(output)
    if settings.rembg_require_alpha and not has_alpha:
        raise CutoutError(
            "Cut-out did not produce a transparent PNG",
            code="CUTOUT_NO_ALPHA",
            retryable=False,
        )
    logger.info(
        "rembg applied | model=%s in_bytes=%s out_bytes=%s has_alpha=%s",
        _session_model,
        len(png_bytes),
        len(output),
        has_alpha,
    )
    return CutoutResult(png_bytes=output, applied=True, has_alpha=has_alpha)

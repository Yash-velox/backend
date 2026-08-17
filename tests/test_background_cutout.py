"""Background cut-out (rembg) unit tests. Inference is mocked; no model download."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services.background_cutout import (
    CutoutError,
    apply_background_cutout,
    reset_cutout_session,
)
from app.services.image_processor import ImageProcessor, ProcessingError
from app.services.shopify_file_upload import png_has_alpha
from tests.test_publishing import PNG_BYTES

# 1x1 RGBA PNG (color type 6) so png_has_alpha is true.
RGBA_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _reset_session():
    reset_cutout_session()
    yield
    reset_cutout_session()


@pytest.fixture
def fake_rembg(monkeypatch):
    module = ModuleType("rembg")
    module.new_session = MagicMock(return_value=object())
    module.remove = MagicMock(return_value=RGBA_PNG)
    monkeypatch.setitem(sys.modules, "rembg", module)
    reset_cutout_session()
    return module


def test_png_has_alpha_detects_color_types():
    assert png_has_alpha(PNG_BYTES) is False
    assert png_has_alpha(RGBA_PNG) is True


def test_cutout_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rembg_enabled", False)
    result = apply_background_cutout(PNG_BYTES)
    assert result.applied is False
    assert result.skipped_reason == "disabled"
    assert result.png_bytes == PNG_BYTES


def test_cutout_skipped_when_already_transparent(monkeypatch):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    result = apply_background_cutout(RGBA_PNG)
    assert result.applied is False
    assert result.skipped_reason == "already_has_alpha"
    assert result.has_alpha is True


def test_cutout_applies_mocked_rembg(monkeypatch, fake_rembg):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    monkeypatch.setattr(settings, "rembg_model", "u2netp")
    monkeypatch.setattr(settings, "rembg_require_alpha", True)
    result = apply_background_cutout(PNG_BYTES)
    assert result.applied is True
    assert result.has_alpha is True
    assert result.png_bytes == RGBA_PNG
    fake_rembg.new_session.assert_called_once_with("u2netp")
    fake_rembg.remove.assert_called_once()


def test_cutout_rejects_flattened_output(monkeypatch, fake_rembg):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    monkeypatch.setattr(settings, "rembg_require_alpha", True)
    fake_rembg.remove.return_value = PNG_BYTES
    with pytest.raises(CutoutError) as exc:
        apply_background_cutout(PNG_BYTES)
    assert exc.value.code == "CUTOUT_NO_ALPHA"
    assert exc.value.retryable is False


def test_cutout_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    monkeypatch.setattr(settings, "rembg_model", "not-a-model")
    with pytest.raises(CutoutError) as exc:
        apply_background_cutout(PNG_BYTES)
    assert exc.value.code == "CUTOUT_MODEL_INVALID"


def test_prepare_output_png_rewrites_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    monkeypatch.setattr(settings, "rembg_require_alpha", True)
    path = tmp_path / "out.png"
    path.write_bytes(PNG_BYTES)
    image = MagicMock()
    image.output_checksum = "old"
    processor = ImageProcessor(db=MagicMock())
    with patch(
        "app.services.image_processor.apply_background_cutout",
        return_value=MagicMock(applied=True, png_bytes=RGBA_PNG, has_alpha=True, skipped_reason=None),
    ):
        meta = processor._prepare_output_png(image, path)
    assert path.read_bytes() == RGBA_PNG
    assert meta["has_alpha"] is True
    assert meta["cutout"]["applied"] is True
    assert image.output_checksum != "old"


def test_prepare_output_png_maps_cutout_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rembg_enabled", True)
    path = tmp_path / "out.png"
    path.write_bytes(PNG_BYTES)
    processor = ImageProcessor(db=MagicMock())
    with patch(
        "app.services.image_processor.apply_background_cutout",
        side_effect=CutoutError("boom", code="REMBG_FAILED", retryable=True),
    ):
        with pytest.raises(ProcessingError) as exc:
            processor._prepare_output_png(MagicMock(), path)
    assert exc.value.code == "REMBG_FAILED"
    assert exc.value.retryable is True

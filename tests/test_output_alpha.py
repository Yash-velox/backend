"""Generated PNG alpha validation. No rembg; OpenAI must return a transparent PNG."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import settings
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


def test_png_has_alpha_detects_color_types():
    assert png_has_alpha(PNG_BYTES) is False
    assert png_has_alpha(RGBA_PNG) is True


def test_prepare_output_png_accepts_transparent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "openai_require_output_alpha", True)
    monkeypatch.setattr(settings, "openai_transparent_background", True)
    monkeypatch.setattr(settings, "openai_image_model", "gpt-image-1.5")
    path = tmp_path / "out.png"
    path.write_bytes(RGBA_PNG)
    processor = ImageProcessor(db=MagicMock())
    meta = processor._prepare_output_png(MagicMock(), path)
    assert meta["has_alpha"] is True


def test_prepare_output_png_rejects_opaque_when_alpha_required(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "openai_require_output_alpha", True)
    monkeypatch.setattr(settings, "openai_transparent_background", True)
    monkeypatch.setattr(settings, "openai_image_model", "gpt-image-1.5")
    path = tmp_path / "out.png"
    path.write_bytes(PNG_BYTES)
    processor = ImageProcessor(db=MagicMock())
    with pytest.raises(ProcessingError) as exc:
        processor._prepare_output_png(MagicMock(), path)
    assert exc.value.code == "OUTPUT_NO_ALPHA"
    assert exc.value.retryable is False


def test_prepare_output_png_allows_opaque_when_alpha_not_required(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "openai_require_output_alpha", False)
    path = tmp_path / "out.png"
    path.write_bytes(PNG_BYTES)
    processor = ImageProcessor(db=MagicMock())
    meta = processor._prepare_output_png(MagicMock(), path)
    assert meta["has_alpha"] is False


def test_prepare_output_png_allows_opaque_when_skip_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "skip_ai_provider_call", True)
    monkeypatch.setattr(settings, "openai_require_output_alpha", True)
    monkeypatch.setattr(settings, "openai_transparent_background", True)
    monkeypatch.setattr(settings, "openai_image_model", "gpt-image-1.5")
    path = tmp_path / "out.png"
    path.write_bytes(PNG_BYTES)
    processor = ImageProcessor(db=MagicMock())
    meta = processor._prepare_output_png(MagicMock(), path)
    assert meta["has_alpha"] is False

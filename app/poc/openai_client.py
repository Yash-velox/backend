from __future__ import annotations

import base64
import logging
import time

import httpx
from openai import OpenAI

from app.config import settings
from app.services.openai_image_compat import supports_transparent_background

logger = logging.getLogger("app.poc.openai")


class OpenAIImageError(RuntimeError):
    pass


class OpenAIImageClient:
    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise OpenAIImageError("OPENAI_API_KEY is not configured")
        timeout = float(settings.openai_image_timeout_seconds)
        self._client = OpenAI(api_key=settings.openai_api_key, timeout=timeout)
        logger.info(
            "OpenAI client ready | model=%s | quality=%s | timeout_s=%s | key_configured=true",
            settings.openai_image_model,
            settings.openai_image_quality,
            timeout,
        )

    def edit_image(
        self,
        *,
        image_bytes: bytes,
        prompt: str,
        job_id: str = "",
        step: int = 0,
        transparent_background: bool = False,
    ) -> bytes:
        model = settings.openai_image_model
        use_transparent = transparent_background and supports_transparent_background(model)
        if transparent_background and not use_transparent:
            logger.info(
                "Skipping background=transparent | model=%s does not support it",
                model,
            )
        logger.info(
            "OpenAI request start | job=%s step=%s model=%s quality=%s input_bytes=%s prompt_len=%s transparent=%s prompt=%r",
            job_id or "-",
            step or "-",
            model,
            settings.openai_image_quality,
            len(image_bytes),
            len(prompt),
            use_transparent,
            prompt,
        )
        started = time.perf_counter()
        try:
            # Opaque PNG by default. Only set background=transparent when explicitly
            # requested and the model supports it (gpt-image-2 rejects that flag).
            edit_kwargs: dict = {
                "model": model,
                "image": ("input.png", image_bytes, "image/png"),
                "prompt": prompt,
                "size": "1024x1024",
                "quality": settings.openai_image_quality,
                "output_format": "png",
            }
            if use_transparent:
                edit_kwargs["background"] = "transparent"
            response = self._client.images.edit(**edit_kwargs)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            logger.exception(
                "OpenAI request failed | job=%s step=%s elapsed_s=%.2f error=%s",
                job_id or "-",
                step or "-",
                elapsed,
                exc,
            )
            raise OpenAIImageError(str(exc)) from exc

        elapsed = time.perf_counter() - started
        if not response.data:
            logger.error(
                "OpenAI empty response | job=%s step=%s elapsed_s=%.2f",
                job_id or "-",
                step or "-",
                elapsed,
            )
            raise OpenAIImageError("OpenAI returned no output image data")

        item = response.data[0]
        b64_data = getattr(item, "b64_json", None)
        if b64_data:
            output = base64.b64decode(b64_data)
            logger.info(
                "OpenAI request success | job=%s step=%s elapsed_s=%.2f output_bytes=%s format=b64_json",
                job_id or "-",
                step or "-",
                elapsed,
                len(output),
            )
            return output

        image_url = getattr(item, "url", None)
        if image_url:
            logger.info(
                "OpenAI returned URL | job=%s step=%s elapsed_s=%.2f downloading...",
                job_id or "-",
                step or "-",
                elapsed,
            )
            result = httpx.get(image_url, timeout=60.0)
            result.raise_for_status()
            logger.info(
                "OpenAI download success | job=%s step=%s output_bytes=%s",
                job_id or "-",
                step or "-",
                len(result.content),
            )
            return result.content

        logger.error(
            "OpenAI response missing image payload | job=%s step=%s elapsed_s=%.2f",
            job_id or "-",
            step or "-",
            elapsed,
        )
        raise OpenAIImageError("OpenAI output does not contain image bytes")

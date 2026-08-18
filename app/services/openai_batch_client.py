"""OpenAI Platform Batch API client (distinct from Primary Queue batches)."""

from __future__ import annotations

import base64
import io
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI

from app.config import settings
from app.services.openai_image_compat import supports_transparent_background

logger = logging.getLogger("app.services.openai_batch_client")

IMAGE_EDITS_ENDPOINT = "/v1/images/edits"
RESPONSES_ENDPOINT = "/v1/responses"


class OpenAIBatchClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = "OPENAI_BATCH_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BatchLine:
    custom_id: str
    method: str
    url: str
    body: dict[str, Any]


def build_image_edit_body(
    *,
    model: str,
    prompt: str,
    image_url: str | None = None,
    file_id: str | None = None,
    transparent_background: bool = False,
) -> dict[str, Any]:
    if bool(image_url) == bool(file_id):
        raise OpenAIBatchClientError(
            "Exactly one of image_url or file_id is required for image edits",
            code="INVALID_IMAGE_REFERENCE",
        )
    image_ref: dict[str, str]
    if file_id:
        image_ref = {"file_id": file_id}
    else:
        image_ref = {"image_url": str(image_url)}
    # Batch JSON (/application/json) requires `images` (array), not multipart `image`.
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "images": [image_ref],
        "size": "1024x1024",
        "output_format": "png",
    }
    # gpt-image-1.5 supports transparent PNG. gpt-image-2 rejects the flag.
    use_transparent = transparent_background and supports_transparent_background(model)
    if use_transparent:
        body["background"] = "transparent"
    elif transparent_background:
        logger.info(
            "Skipping background=transparent | model=%s does not support it",
            model,
        )
    return body


def build_description_body(
    *,
    model: str,
    prompt: str,
    image_url: str | None = None,
    file_id: str | None = None,
    prior_description: str | None = None,
) -> dict[str, Any]:
    if bool(image_url) == bool(file_id):
        raise OpenAIBatchClientError(
            "Exactly one of image_url or file_id is required for description steps",
            code="INVALID_IMAGE_REFERENCE",
        )
    text_parts = [prompt]
    if prior_description and prior_description.strip():
        text_parts.append(f"Previous description context:\n{prior_description.strip()}")
    content: list[dict[str, Any]] = [{"type": "input_text", "text": "\n\n".join(text_parts)}]
    if file_id:
        content.append({"type": "input_image", "file_id": file_id})
    else:
        content.append({"type": "input_image", "image_url": str(image_url)})
    return {
        "model": model,
        "input": [{"role": "user", "content": content}],
    }


def lines_to_jsonl(lines: Iterable[BatchLine]) -> bytes:
    chunks: list[str] = []
    for line in lines:
        chunks.append(
            json.dumps(
                {
                    "custom_id": line.custom_id,
                    "method": line.method,
                    "url": line.url,
                    "body": line.body,
                },
                separators=(",", ":"),
            )
        )
    return ("\n".join(chunks) + "\n").encode("utf-8")


def parse_jsonl(content: str | bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def extract_image_bytes_from_response_body(body: dict[str, Any]) -> bytes:
    data = body.get("data") or []
    if not data:
        raise OpenAIBatchClientError("OpenAI image response missing data", code="EMPTY_OUTPUT")
    item = data[0] if isinstance(data, list) else data
    b64_data = item.get("b64_json") if isinstance(item, dict) else None
    if b64_data:
        return base64.b64decode(b64_data)
    raise OpenAIBatchClientError("OpenAI image response missing b64_json", code="EMPTY_OUTPUT")


def extract_text_from_responses_body(body: dict[str, Any]) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = body.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and part.get("text"):
                chunks.append(str(part["text"]))
    text = "\n".join(chunks).strip()
    if not text:
        raise OpenAIBatchClientError("OpenAI responses body missing text", code="EMPTY_OUTPUT")
    return text


class OpenAIBatchClient:
    def __init__(self, client: OpenAI | None = None) -> None:
        if client is not None:
            self._client = client
            return
        if not settings.openai_api_key:
            raise OpenAIBatchClientError("OPENAI_API_KEY is not configured", code="OPENAI_NOT_CONFIGURED")
        self._client = OpenAI(api_key=settings.openai_api_key, timeout=settings.openai_image_timeout_seconds)

    def upload_batch_jsonl(self, data: bytes, *, filename: str = "batch_input.jsonl") -> str:
        bio = io.BytesIO(data)
        bio.name = filename  # type: ignore[attr-defined]
        uploaded = self._client.files.create(file=bio, purpose="batch")
        return uploaded.id

    def upload_vision_image(self, image_bytes: bytes, *, filename: str = "intermediate.png") -> str:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            path = Path(tmp.name)
        try:
            with path.open("rb") as fh:
                uploaded = self._client.files.create(file=fh, purpose="vision")
            return uploaded.id
        finally:
            path.unlink(missing_ok=True)

    def create_batch(self, *, input_file_id: str, endpoint: str, metadata: dict[str, str] | None = None) -> Any:
        return self._client.batches.create(
            input_file_id=input_file_id,
            endpoint=endpoint,  # type: ignore[arg-type]
            completion_window=settings.openai_batch_completion_window,  # type: ignore[arg-type]
            metadata=metadata or None,
        )

    def retrieve_batch(self, openai_batch_id: str) -> Any:
        return self._client.batches.retrieve(openai_batch_id)

    def cancel_batch(self, openai_batch_id: str) -> Any:
        return self._client.batches.cancel(openai_batch_id)

    def download_file_text(self, file_id: str) -> str:
        return self._client.files.content(file_id).text

    def delete_file(self, file_id: str) -> None:
        try:
            self._client.files.delete(file_id)
        except Exception as exc:
            raise OpenAIBatchClientError(f"Failed to delete OpenAI file {file_id}: {exc}", code="FILE_DELETE_FAILED") from exc

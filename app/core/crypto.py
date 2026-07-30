from __future__ import annotations

import base64
import hashlib
import hmac
import time

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    raw = (settings.token_encryption_key or settings.app_secret_key or "change-me").encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_token(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str | None) -> str | None:
    if ciphertext is None or ciphertext == "":
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt token") from exc


def sign_internal_payload(body: bytes, *, timestamp: str | None = None) -> tuple[str, str]:
    ts = timestamp or str(int(time.time()))
    secret = (settings.internal_handoff_secret or settings.app_secret or "").encode("utf-8")
    digest = hmac.new(secret, f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return ts, digest


def verify_internal_signature(
    body: bytes,
    *,
    timestamp: str,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = int(time.time())
    if abs(now - ts) > max_age_seconds:
        return False
    secret = (settings.internal_handoff_secret or settings.app_secret or "").encode("utf-8")
    expected = hmac.new(secret, f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_shopify_webhook_hmac(raw_body: bytes, hmac_header: str | None) -> bool:
    if not hmac_header:
        return False
    secret = (settings.shopify_api_secret or "").encode("utf-8")
    if not secret:
        return False
    digest = base64.b64encode(hmac.new(secret, raw_body, hashlib.sha256).digest()).decode("utf-8")
    return hmac.compare_digest(digest, hmac_header)

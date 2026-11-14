from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import re
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def token_hash(token: str, pepper: bytes) -> str:
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._aes = AESGCM(key)

    def seal(self, value: dict[str, Any], *, purpose: str) -> str:
        nonce = os.urandom(12)
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encrypted = self._aes.encrypt(nonce, raw, purpose.encode("ascii"))
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def open(self, value: str, *, purpose: str) -> dict[str, Any]:
        packed = base64.urlsafe_b64decode(value.encode("ascii"))
        raw = self._aes.decrypt(packed[:12], packed[12:], purpose.encode("ascii"))
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("encrypted payload is not an object")
        return decoded


def redact(text: str, *secrets_to_hide: str) -> str:
    result = text
    for secret in secrets_to_hide:
        if secret and len(secret) >= 6:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", result)
    result = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]{12,}", r"\1[REDACTED]", result)
    result = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", result)
    result = re.sub(r"\bsk-[A-Za-z0-9._-]{12,}\b", "[REDACTED_API_KEY]", result)
    return result

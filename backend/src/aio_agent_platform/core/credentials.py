"""At-rest encryption for third-party credentials.

Legacy plaintext values are returned unchanged, so no migration is required:
each credential is encrypted the next time it is written.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from aio_agent_platform.core.config import settings

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    material = settings.credentials.key or settings.jwt.secret
    key = hashlib.sha256(f"aio-credentials:{material}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str | None) -> str | None:
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return value

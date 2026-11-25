"""At-rest encryption for third-party credentials.

Legacy plaintext values are returned unchanged, so no migration is required:
each credential is encrypted the next time it is written.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from agent_platform.core.config import settings

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


def decrypt_tool_secret(value: str) -> str:
    """Legacy cleartext is valid. Unreadable ciphertext must never be sent upstream."""
    plain = decrypt_secret(value)
    if plain is None or (value.startswith(_PREFIX) and plain == value):
        raise ValueError("Tool credential cannot be decrypted with the configured key")
    return plain


def _map_fields(values, transform):
    if isinstance(values, dict):
        return {key: _map_fields(value, transform) for key, value in values.items()}
    if isinstance(values, list):
        return [_map_fields(value, transform) for value in values]
    return transform(values) if isinstance(values, str) else values


def decrypt_fields(values: dict | None) -> dict | None:
    return _map_fields(values, decrypt_tool_secret)


def encrypt_fields(values: dict | None) -> dict | None:
    return _map_fields(values, encrypt_secret)


def merge_masked_fields(values: dict, previous: dict | None) -> dict:
    """The edit form posts masked values back for fields the operator kept."""
    stored = previous or {}
    merged = {}
    for key, value in values.items():
        if value == "****" and key in stored:
            merged[key] = stored[key]
        elif isinstance(value, dict):
            nested = stored.get(key)
            merged[key] = merge_masked_fields(value, nested if isinstance(nested, dict) else None)
        else:
            merged[key] = value
    return merged

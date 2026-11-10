"""Short-lived HMAC tokens bound to a single resource (image, websocket, page)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from aio_agent_platform.core.config import settings


def _sign(resource: str, expires_at: int) -> str:
    payload = f"{resource}:{expires_at}"
    signature = hmac.new(
        settings.jwt.secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def mint_resource_token(resource: str, ttl_seconds: int = 3600) -> str:
    return _sign(resource, int(time.time()) + max(1, ttl_seconds))


def verify_resource_token(token: str, resource: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        signed_resource, expires_at, _signature = raw.rsplit(":", 2)
        if signed_resource != resource:
            return False
        if int(expires_at) < int(time.time()):
            return False
        expected = _sign(resource, int(expires_at))
        return hmac.compare_digest(token, expected)
    except Exception:
        return False

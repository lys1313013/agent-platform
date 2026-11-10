"""Credentials and private command storage never use the platform JWT key."""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException

from aio_agent_platform.core.config import settings


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def enabled() -> None:
    if not settings.browser.enabled:
        raise HTTPException(503, "本地浏览器功能未启用")
    if (len(settings.browser.secret_key.get_secret_value()) < 32
            or settings.browser.secret_key.get_secret_value() == settings.jwt.secret):
        raise HTTPException(503, "浏览器服务尚未配置独立密钥")


def key(purpose: bytes) -> bytes:
    enabled()
    return hmac.digest(settings.browser.secret_key.get_secret_value().encode(), purpose, "sha256")


def digest(value: str) -> str:
    return hmac.new(key(b"credential-hash"), value.encode(), hashlib.sha256).hexdigest()


def seal(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return Fernet(base64.urlsafe_b64encode(key(b"private-content"))).encrypt(encoded).decode()


def unseal(value: str):
    return json.loads(Fernet(base64.urlsafe_b64encode(key(b"private-content"))).decrypt(value.encode()))


def valid_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", value))


def origin(url: str) -> str:
    """Normalize the command destination; this is not a browser network sandbox."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode().lower().rstrip(".")
        port = parts.port
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise ValueError
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
            raise ValueError
        if any(c in host for c in "\\%#? "):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                address = None
        if address is not None and not address.is_global:
            raise ValueError

        host_display = f"[{host}]" if ":" in host else host
        suffix = f":{port}" if port and port != (443 if parts.scheme == "https" else 80) else ""
        return f"{parts.scheme}://{host_display}{suffix}"
    except (ValueError, UnicodeError):
        raise HTTPException(422, "请输入公开网站的 http/https 地址，不允许本地地址或 URL 凭证") from None


def public_endpoint() -> str:
    value = settings.browser.public_wss_url.rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
        local_ws = (settings.browser.allow_loopback_ws and parts.scheme == "ws"
                    and parts.hostname in {"127.0.0.1", "localhost", "::1"})
        if (parts.scheme != "wss" and not local_ws) or not parts.hostname or parts.query or parts.fragment or parts.username is not None:
            raise ValueError
        if port == 0:
            raise ValueError
    except ValueError:
        raise HTTPException(503, "浏览器服务需要有效的 WSS 地址；本机 WS 需显式启用且仅限回环地址") from None
    if parts.path != "/extension":
        raise HTTPException(503, "浏览器 WSS 路径必须为 /extension")
    return value

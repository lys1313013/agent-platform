import pytest
from fastapi import HTTPException

from agent_platform.browser.security import public_endpoint
from agent_platform.core.config import settings


@pytest.mark.parametrize("url,allowed,valid", [
    ("wss://browser.example.com/extension", False, True),
    ("ws://127.0.0.1:8101/extension", False, False),
    ("ws://127.0.0.1:8101/extension", True, True),
    ("ws://localhost:8101/extension", True, True),
    ("ws://[::1]:8101/extension", True, True),
    ("ws://browser.example.com/extension", True, False),
    ("ws://192.168.1.2/extension", True, False),
    ("ws://127.0.0.1.evil.example/extension", True, False),
    ("ws://127.0.0.1@evil.example/extension", True, False),
    ("ws://@127.0.0.1/extension", True, False),
    ("ws://127.0.0.1:invalid/extension", True, False),
    ("ws://127.0.0.1:0/extension", True, False),
    ("ws://127.0.0.1/extension?token=secret", True, False),
    ("ws://127.0.0.1/extension#secret", True, False),
    ("ws://127.0.0.1/other", True, False),
    ("wss://[invalid/extension", False, False),
])
def test_gateway_endpoint(monkeypatch, url, allowed, valid):
    monkeypatch.setattr(settings.browser, "public_wss_url", url)
    monkeypatch.setattr(settings.browser, "allow_loopback_ws", allowed)
    if valid:
        assert public_endpoint() == url
    else:
        with pytest.raises(HTTPException) as error:
            public_endpoint()
        assert error.value.status_code == 503

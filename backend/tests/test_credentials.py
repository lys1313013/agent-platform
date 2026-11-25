"""Credential encryption round-trips and tolerates legacy plaintext rows."""

from agent_platform.core.credentials import decrypt_secret, encrypt_secret


def test_encrypt_roundtrip():
    ciphertext = encrypt_secret("super-secret")
    assert ciphertext is not None
    assert ciphertext.startswith("enc:v1:")
    assert ciphertext != "super-secret"
    assert decrypt_secret(ciphertext) == "super-secret"


def test_encrypt_is_idempotent_for_ciphertext():
    ciphertext = encrypt_secret("value")
    assert encrypt_secret(ciphertext) == ciphertext


def test_legacy_plaintext_stays_readable():
    assert decrypt_secret("plain-legacy-value") == "plain-legacy-value"


def test_empty_values_pass_through():
    assert encrypt_secret(None) is None
    assert decrypt_secret(None) is None
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_remote_field_rotation_preserves_nested_masked_and_legacy_values():
    from agent_platform.core.credentials import decrypt_fields, encrypt_fields, merge_masked_fields

    previous = {"headers": {"A": encrypt_secret("one"), "B": "legacy"}}
    edited = {"headers": {"A": "****", "B": "new"}}
    stored = encrypt_fields(merge_masked_fields(edited, previous))
    assert stored["headers"]["A"] == previous["headers"]["A"]
    assert stored["headers"]["B"] != "new"
    assert decrypt_fields(stored) == {"headers": {"A": "one", "B": "new"}}
    assert decrypt_fields(previous)["headers"]["B"] == "legacy"


def test_unreadable_remote_ciphertext_is_rejected():
    import pytest

    from agent_platform.core.credentials import decrypt_fields

    with pytest.raises(ValueError, match="cannot be decrypted"):
        decrypt_fields({"token": "enc:v1:invalid"})


def test_web_overlay_decrypts_only_db_backed_secrets():
    from agent_platform.core.config import WebSettings
    from agent_platform.tools.web.config import overlay

    actual = overlay(WebSettings(), {"brave_api_key": encrypt_secret("one"), "tavily_api_key": "legacy"})
    assert actual.brave_api_key == "one" and actual.tavily_api_key == "legacy"


async def test_explicit_conversion_verifies_dry_run_and_is_idempotent(monkeypatch, capsys):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from agent_platform import manage

    tool = SimpleNamespace(auth_config={"token": "secret"}, headers={"X": "old"})
    web = SimpleNamespace(value="web-key")

    @asynccontextmanager
    async def context(result):
        yield result

    session = Mock()
    session.begin.side_effect = lambda: context(None)
    session.scalars = AsyncMock(side_effect=lambda _: None)
    monkeypatch.setattr(manage, "get_session_factory", lambda: lambda: context(session))
    monkeypatch.setattr(manage, "close_db", AsyncMock())

    async def run(apply):
        session.scalars.side_effect = [Mock(all=lambda: [tool]), Mock(all=lambda: [web])]
        await manage.encrypt_tool_credentials(apply)
        return capsys.readouterr().out

    assert "待转换远程工具 1 条，Web 密钥 1 条" in await run(False)
    assert tool.auth_config["token"] == "secret" and web.value == "web-key"
    assert "已转换远程工具 1 条，Web 密钥 1 条" in await run(True)
    assert decrypt_secret(tool.auth_config["token"]) == "secret"
    assert decrypt_secret(web.value) == "web-key"
    assert "待转换远程工具 0 条，Web 密钥 0 条" in await run(False)

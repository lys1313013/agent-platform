"""Regression tests for the user-facing clock injected into model prompts."""

from datetime import datetime, timedelta

from agent_platform.core import prompt as prompt_module


def test_formatted_current_time_uses_beijing_timezone():
    value = prompt_module._format_current_datetime()
    iso_value, zone = value.rsplit(" (", 1)
    parsed = datetime.fromisoformat(iso_value)

    assert parsed.utcoffset() == timedelta(hours=8)
    assert zone == "Asia/Shanghai)"


def test_default_prompt_keeps_clock_out_of_static_prefix(monkeypatch):
    current = "2026-09-16 21:33:08+08:00 (Asia/Shanghai)"
    monkeypatch.setattr(prompt_module, "_format_current_datetime", lambda: current)

    rendered = prompt_module.build_system_prompt()

    assert "Current time" not in rendered
    assert current not in rendered
    assert current in prompt_module.build_current_time_context()
    assert "13:33 UTC" not in rendered


def test_custom_prompt_keeps_clock_out_of_static_prefix(monkeypatch):
    current = "2026-09-16 21:33:08+08:00 (Asia/Shanghai)"
    monkeypatch.setattr(prompt_module, "_format_current_datetime", lambda: current)

    rendered = prompt_module.build_system_prompt(agent_prompt="You are a test agent.")

    assert "Current time" not in rendered
    assert current not in rendered
    assert current in prompt_module.build_current_time_context()
    assert "13:33 UTC" not in rendered

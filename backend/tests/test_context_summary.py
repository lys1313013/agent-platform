"""Incremental context summaries fold new turns into the previous summary."""

from types import SimpleNamespace

from agent_platform.core.context import generate_summary
from agent_platform.llm import LLMMessage


class _CapturingProvider:
    def __init__(self) -> None:
        self.prompt = ""

    async def complete(self, messages, max_tokens=None):
        self.prompt = messages[0].content
        return SimpleNamespace(content="merged summary")


async def test_incremental_summary_passes_existing_text_and_new_messages():
    provider = _CapturingProvider()
    result = await generate_summary(
        [LLMMessage(role="user", content="new question")],
        provider,
        max_chars=500,
        existing_summary="old context",
    )
    assert result == "merged summary"
    assert "old context" in provider.prompt
    assert "new question" in provider.prompt
    assert provider.prompt.index("old context") < provider.prompt.index("new question")

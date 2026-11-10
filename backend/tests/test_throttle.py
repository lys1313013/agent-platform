"""Throttle helpers degrade to process-local state when Redis is unavailable."""

from uuid import uuid4

from agent_platform.core import throttle


async def test_allow_once_blocks_repeats_within_ttl():
    key = f"test:allow:{uuid4()}"
    assert await throttle.allow_once(key, 60) is True
    assert await throttle.allow_once(key, 60) is False


async def test_claim_message_batch_requires_minimum_delta():
    key = f"test:batch:{uuid4()}"
    assert await throttle.claim_message_batch(key, 10, min_delta=4) == 0
    assert await throttle.claim_message_batch(key, 12, min_delta=4) is None
    assert await throttle.claim_message_batch(key, 14, min_delta=4) == 10

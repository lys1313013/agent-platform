"""后台 LLM 工作的 Redis 节流器（进程内兜底）。

记忆提取、上下文摘要这类「每轮都可能触发」的后台调用用这里的节流器降频：
多 worker 靠 Redis 协调，Redis 不可用时每个进程各自按本地时钟节流。
"""

from __future__ import annotations

import time

import redis.asyncio as aioredis
import structlog

from aio_agent_platform.core.config import settings

logger = structlog.get_logger()

_LOCAL_PRUNE_THRESHOLD = 4096
_COUNT_TTL_SECONDS = 7 * 24 * 3600
_client: aioredis.Redis | None = None
_local_until: dict[str, float] = {}
_local_counts: dict[str, int] = {}


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis.from_url(
            settings.redis.url, decode_responses=True,
            socket_connect_timeout=1.0, socket_timeout=1.0,
        )
    return _client


def _prune(now: float) -> None:
    if len(_local_until) < _LOCAL_PRUNE_THRESHOLD:
        return
    for key, until in list(_local_until.items()):
        if until <= now:
            _local_until.pop(key, None)


async def allow_once(key: str, ttl_seconds: int) -> bool:
    """True at most once per ttl_seconds across workers."""
    now = time.monotonic()
    _prune(now)
    if _local_until.get(key, 0.0) > now:
        return False
    try:
        allowed = bool(await _redis().set(key, "1", nx=True, ex=max(1, ttl_seconds)))
    except Exception:
        allowed = True
    # A Redis rejection means another worker recently claimed it; re-check
    # soon instead of paying a Redis round-trip on every turn.
    _local_until[key] = now + (ttl_seconds if allowed else min(5, ttl_seconds))
    return allowed


async def claim_message_batch(
    key: str, current: int, min_delta: int, ttl_seconds: int = _COUNT_TTL_SECONDS,
) -> int | None:
    """Claim a summary update once at least min_delta messages accumulated.

    Returns the previous message count (0 when unknown), or None when the
    caller should skip this round.
    """
    previous = _local_counts.get(key)
    if previous is None:
        try:
            raw = await _redis().get(key)
            previous = int(raw) if raw else 0
        except Exception:
            previous = 0
    if current - previous < min_delta:
        _local_counts[key] = previous
        return None
    _local_counts[key] = current
    try:
        await _redis().set(key, str(current), ex=ttl_seconds)
    except Exception:
        logger.debug("throttle_claim_not_persisted", key=key)
    return previous

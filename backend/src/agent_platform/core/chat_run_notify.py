"""后台任务的唤醒通道。

DB 仍是事件的唯一事实来源；Redis Pub/Sub 只负责通知订阅方「有新事件或终态」，
让空闲 SSE 连接不再每 200ms 查库并锁行。Redis 不可用时退化为进程内唤醒 +
订阅方超时回查，跨进程仍能通过轮询兜底。
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import redis.asyncio as aioredis
import structlog

from agent_platform.core.config import settings

logger = structlog.get_logger()

CHANNEL_PREFIX = "aio:chat_run:"
_RECONNECT_SECONDS = 1.0


class ChatRunNotifier:
    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._listen_task: asyncio.Task | None = None
        self._client: aioredis.Redis | None = None
        self._publish_tasks: set[asyncio.Task] = set()
        self._publish_warned_at = 0.0
        self._listen_warned = False

    def version(self, run_id: UUID | str) -> int:
        """Monotonic per-run counter; lets a reader detect a wake-up it missed."""
        return self._versions.get(str(run_id), 0)

    def _signal(self, run_id: str) -> None:
        self._versions[run_id] = self._versions.get(run_id, 0) + 1
        for waiter in self._waiters.get(run_id, ()):
            waiter.set()

    async def publish(self, run_id: UUID, sequence: int, status: str) -> None:
        key = str(run_id)
        self._signal(key)
        # Never put Redis latency on the producer's hot path.
        task = asyncio.create_task(self._publish(key, sequence, status))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish(self, key: str, sequence: int, status: str) -> None:
        try:
            await self._redis().publish(
                f"{CHANNEL_PREFIX}{key}",
                json.dumps({"run_id": key, "sequence": sequence, "status": status}),
            )
        except Exception:
            now = asyncio.get_running_loop().time()
            if now - self._publish_warned_at >= 60:
                self._publish_warned_at = now
                logger.warning("chat_run_publish_failed", run_id=key)

    async def wait(self, run_id: UUID, since: int, timeout: float) -> None:
        """Wait until the run's version changes, or the timeout expires."""
        key = str(run_id)
        waiter = asyncio.Event()
        self._waiters.setdefault(key, set()).add(waiter)
        # Register before re-checking: a publish between the caller's read and
        # this call must not be lost.
        if self.version(key) != since:
            self._drop_waiter(key, waiter)
            return
        self._ensure_listener()
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._drop_waiter(key, waiter)

    def _drop_waiter(self, key: str, waiter: asyncio.Event) -> None:
        waiters = self._waiters.get(key)
        if waiters is None:
            return
        waiters.discard(waiter)
        if not waiters:
            self._waiters.pop(key, None)
            self._versions.pop(key, None)

    def _ensure_listener(self) -> None:
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        pattern = f"{CHANNEL_PREFIX}*"
        try:
            client = self._redis()
            while True:
                pubsub = client.pubsub()
                try:
                    await pubsub.psubscribe(pattern)
                    self._listen_warned = False
                    async for message in pubsub.listen():
                        if message.get("type") != "pmessage":
                            continue
                        channel = message.get("channel") or ""
                        if isinstance(channel, bytes):
                            channel = channel.decode()
                        self._signal(channel.removeprefix(CHANNEL_PREFIX))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not self._listen_warned:
                        self._listen_warned = True
                        logger.warning("chat_run_listen_failed", exc_info=True)
                    else:
                        logger.debug("chat_run_listen_retry")
                finally:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                await asyncio.sleep(_RECONNECT_SECONDS)
        finally:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()

    async def shutdown(self) -> None:
        task, self._listen_task = self._listen_task, None
        pending, self._publish_tasks = list(self._publish_tasks), set()
        self._waiters.clear()
        self._versions.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _redis(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.Redis.from_url(
                settings.redis.url, decode_responses=True,
                socket_connect_timeout=1.0, socket_timeout=1.0,
            )
        return self._client


notifier = ChatRunNotifier()

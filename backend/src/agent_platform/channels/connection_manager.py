"""Channel connection manager — lifecycle management for all channel transports."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.channels.adapter import ChannelAdapter
from agent_platform.channels.registry import get_channel_spec
from agent_platform.db.connection import get_session_factory
from agent_platform.db.models import ChannelConfig
from agent_platform.tools.executor import ToolExecutor

logger = structlog.get_logger()

# Process-wide singleton, set during app lifespan. Command handlers have no
# app.state access, so /status reaches the channel manager through this.
_global_channel_manager: ChannelConnectionManager | None = None


def set_global_channel_manager(manager: ChannelConnectionManager | None) -> None:
    global _global_channel_manager
    _global_channel_manager = manager


def get_global_channel_manager() -> ChannelConnectionManager | None:
    return _global_channel_manager


class ChannelConnectionManager:
    """Manages the lifecycle of all channel connections.

    On startup, loads all enabled channels and starts their transports.
    Provides methods to enable/disable channels dynamically.
    """

    def __init__(self, tool_executor: ToolExecutor):
        self.tool_executor = tool_executor
        self._lock = asyncio.Lock()
        self._reconciler: asyncio.Task | None = None
        self._versions: dict[UUID, UUID | None] = {}
        self._online = False
        self._adapters: dict[UUID, ChannelAdapter] = {}
        self._channel_configs: dict[UUID, ChannelConfig] = {}

    async def start_all(self, db: AsyncSession) -> None:
        """Load and start all enabled channels."""
        self._online = True
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.status == "enabled")
        )
        channels = result.scalars().all()
        for channel in channels:
            try:
                await self._start_channel(channel)
            except Exception:
                logger.exception(
                    "channel_start_failed",
                    channel_id=str(channel.id),
                    name=channel.name,
                )
                channel.status = "error"
                channel.last_error = "Failed to start channel"
                await db.commit()

        self._reconciler = asyncio.create_task(self._reconcile_loop())

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                async with self._lock:
                    async with get_session_factory()() as db:
                        result = await db.scalars(select(ChannelConfig).where(ChannelConfig.status == "enabled"))
                        channels = {channel.id: channel for channel in result}
                    for channel_id in self._adapters.keys() - channels.keys():
                        await self._stop_channel(channel_id)
                    for channel in channels.values():
                        if self._versions.get(channel.id) != channel.transport_version:
                            await self._stop_channel(channel.id)
                            try:
                                await self._start_channel(channel)
                            except Exception:
                                logger.exception("channel_reconcile_start_failed", channel_id=str(channel.id))
            except Exception:
                logger.exception("channel_reconcile_failed")

    async def stop_all(self) -> None:
        """Stop all active channels."""
        if self._reconciler is not None:
            self._reconciler.cancel()
            await asyncio.gather(self._reconciler, return_exceptions=True)
            self._reconciler = None
        async with AsyncExitStack() as cleanup:
            for channel_id in reversed(list(self._adapters)):
                cleanup.push_async_callback(self.stop_channel, channel_id)

    async def start_channel(self, channel: ChannelConfig) -> None:
        """Start a single channel."""
        async with self._lock:
            version = getattr(channel, "transport_version", None)
            if channel.id in self._adapters:
                if self._versions.get(channel.id) == version:
                    return
                await self._stop_channel(channel.id)
            await self._start_channel(channel)

    async def stop_channel(self, channel_id: UUID) -> None:
        async with self._lock:
            await self._stop_channel(channel_id)

    async def _stop_channel(self, channel_id: UUID) -> None:
        adapter = self._adapters.pop(channel_id, None)
        if adapter is None:
            return
        try:
            # Stop accepting/drain execution before the adapter closes the
            # transport/client used by an interrupted turn's final response.
            pipeline = getattr(adapter, "pipeline", None)
            if pipeline is not None:
                await pipeline.shutdown()
        except Exception:
            logger.exception("channel_pipeline_stop_failed", channel_id=str(channel_id))
        finally:
            try:
                await adapter.stop()
            except Exception:
                logger.exception("channel_stop_failed", channel_id=str(channel_id))
            finally:
                self._channel_configs.pop(channel_id, None)
                self._versions.pop(channel_id, None)
        logger.info("channel_stopped", channel_id=str(channel_id))

    async def _start_channel(self, channel: ChannelConfig) -> None:
        """Internal: build the adapter via the channel-type spec, then start it.

        The spec's ``build`` creates the client → pipeline → adapter → transport
        chain; webhook transports register themselves with the shared router on
        ``adapter.start()``.
        """
        spec = get_channel_spec(channel.channel_type)
        if spec.build is None:
            raise ValueError(
                f"Channel type {channel.channel_type} has no adapter builder"
            )

        adapter = spec.build(channel, self.tool_executor)

        # Own the adapter before start: it may allocate a client or background
        # task and then fail or be cancelled.
        self._adapters[channel.id] = adapter
        self._channel_configs[channel.id] = channel
        self._versions[channel.id] = getattr(channel, "transport_version", None)
        try:
            await adapter.start()
            if self._online:
                adapter.pipeline.inbox.start()
        except BaseException:
            await self._stop_channel(channel.id)
            raise
        logger.info("channel_started", channel_id=str(channel.id), mode=channel.mode)

    def get_adapter(self, channel_id: UUID) -> ChannelAdapter | None:
        return self._adapters.get(channel_id)

    def get_status(self, tenant_id: UUID | None = None) -> list[dict[str, Any]]:
        """Return live channel status, optionally restricted to one tenant."""
        statuses = []
        for channel_id, adapter in self._adapters.items():
            channel = self._channel_configs.get(channel_id)
            if channel is None or (tenant_id is not None and channel.tenant_id != tenant_id):
                continue
            statuses.append({
                "channel_id": str(channel_id),
                "name": channel.name,
                "channel_type": channel.channel_type,
                "mode": channel.mode,
                "transport_state": adapter.transport.state.value if adapter.transport else "unknown",
            })
        return statuses

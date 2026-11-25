"""Durable IM receipts, ordered per conversation with one fenced consumer per channel."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from agent_platform.channels.adapter import AttachmentInfo, ChatKind, InboundEvent
from agent_platform.core.credentials import decrypt_secret, encrypt_secret
from agent_platform.db.connection import get_session_factory
from agent_platform.db.models import (
    ChannelBinding,
    ChannelConfig,
    ChannelInboxEvent,
    ChannelInboxLease,
)

if TYPE_CHECKING:
    from agent_platform.channels.pipeline import ChannelInboundPipeline

log = structlog.get_logger()
POLL_SECONDS = 2
LEASE = timedelta(seconds=30)
MAX_ACTIVE = 12


class ChannelUnavailable(RuntimeError):
    """The transport has been disabled or replaced before this receipt."""


class ChannelInbox:
    def __init__(self, pipeline: ChannelInboundPipeline):
        self.pipeline = pipeline
        self.channel_id = pipeline.channel.id
        self.owner = uuid4()
        self.wake = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.tasks: dict[asyncio.Task, int] = {}
        self.next_cleanup = 0.0

    def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())

    async def check_available(self) -> None:
        async with get_session_factory()() as db:
            if self.pipeline._closed or await db.scalar(self._active_channel()) is None:
                raise ChannelUnavailable("渠道配置已变更")

    async def receive(self, event: InboundEvent) -> None:
        if self.pipeline._closed or event.channel_id != self.channel_id or not event.event_id:
            raise ChannelUnavailable("渠道已关闭或回调缺少事件标识")
        async with get_session_factory()() as db, db.begin():
            # This short lock serializes sequence allocation and credential rotation.
            # A revoked transport cannot enqueue after an administrator commits a change.
            active = await db.scalar(self._active_channel().with_for_update())
            if active is None:
                raise ChannelUnavailable("渠道配置已变更")
            binding_id = await db.scalar(select(ChannelBinding.id).where(
                ChannelBinding.channel_id == self.channel_id,
                ChannelBinding.external_id == event.external_id,
            ))
            payload = asdict(event)
            payload.pop("channel_id")
            # Feishu raw envelopes include a verification token. Retain only
            # the response routing fields actually consumed by each adapter.
            raw = {key: event.raw[key] for key in ("req_id", "chatid") if key in event.raw}
            if event.raw.get("aeskey"):
                raw["aeskey"] = encrypt_secret(event.raw["aeskey"])
            if event.kind == "recall":
                raw["event"] = {key: event.raw.get("event", {}).get(key)
                                for key in ("recall_time", "recall_type")}
            payload["raw"] = raw
            payload["transport_version"] = str(self.pipeline.transport_version)
            payload["binding_captured"] = True
            payload["binding_id"] = str(binding_id) if binding_id else None
            kind = event.kind
            if event.text.strip() == "/stop" and (event.chat_kind == ChatKind.DIRECT or event.mentions_bot):
                kind = "stop"
            recalled = False
            if event.message_id and kind == "message":
                recalled = await db.scalar(select(exists(select(1).where(
                    ChannelInboxEvent.channel_id == self.channel_id, ChannelInboxEvent.kind == "recall",
                    ChannelInboxEvent.message_id == event.message_id,
                ))))
            await db.execute(insert(ChannelInboxEvent).values(
                channel_id=self.channel_id, event_id=event.event_id, chat_id=event.chat_id,
                external_id=event.external_id, message_id=event.message_id, kind=kind,
                payload=None if recalled else payload, status="interrupted" if recalled else "queued",
                finished_at=func.now() if recalled else None,
            ).on_conflict_do_nothing(constraint="uq_channel_inbox_delivery"))
        self.wake.set()

    def _active_channel(self):
        return select(ChannelConfig.id).where(
            ChannelConfig.id == self.channel_id, ChannelConfig.status == "enabled",
            ChannelConfig.transport_version == self.pipeline.transport_version,
        )

    async def _run(self) -> None:
        while True:
            self.wake.clear()
            try:
                await self._poll()
            except Exception:
                log.exception("channel_inbox_poll_failed", channel_id=str(self.channel_id))
                # Without lease renewal another replica will eventually take ownership.
                await self._cancel_turns()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass

    async def _poll(self) -> None:
        rows = []
        async with get_session_factory()() as db, db.begin():
            if not self.tasks and time.monotonic() < self.next_cleanup:
                has_work = await db.scalar(select(exists(select(1).where(
                    ChannelInboxEvent.channel_id == self.channel_id,
                    ChannelInboxEvent.status.in_(("queued", "claimed", "running")),
                ))))
                if not has_work:
                    return
            if await db.scalar(self._active_channel()) is None:
                owns = False
            else:
                lease = insert(ChannelInboxLease).values(
                    channel_id=self.channel_id, owner=self.owner, expires_at=func.now() + LEASE,
                )
                owns = await db.scalar(lease.on_conflict_do_update(
                    index_elements=[ChannelInboxLease.channel_id],
                    set_={"owner": self.owner, "expires_at": func.now() + LEASE},
                    where=((ChannelInboxLease.owner == self.owner) | (ChannelInboxLease.expires_at <= func.now())),
                ).returning(ChannelInboxLease.owner)) is not None
            if owns:
                # Only a committed transition to running is permitted to invoke tools.
                # Reclaim reserved work immediately; preserve indeterminate attempts for audit.
                abandoned = [ChannelInboxEvent.channel_id == self.channel_id,
                             ChannelInboxEvent.id.not_in(list(self.tasks.values()))]
                await db.execute(update(ChannelInboxEvent).where(
                    *abandoned, ChannelInboxEvent.status == "claimed",
                ).values(status="queued", owner=None))
                await db.execute(update(ChannelInboxEvent).where(
                    *abandoned, ChannelInboxEvent.status == "running",
                ).values(status="uncertain", finished_at=func.now()))
                if time.monotonic() >= self.next_cleanup:
                    await db.execute(delete(ChannelInboxEvent).where(
                        ChannelInboxEvent.channel_id == self.channel_id,
                        ChannelInboxEvent.finished_at < func.now() - timedelta(days=30),
                    ))
                    self.next_cleanup = time.monotonic() + 3600
                rows = await self._claim(db)
        if not owns:
            self.next_cleanup = time.monotonic() + 60
            await self._cancel_turns()
            return
        for row in rows:
            task = asyncio.create_task(self._process(row))
            self.tasks[task] = row.id
            task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task) -> None:
        self.tasks.pop(task, None)
        if not task.cancelled() and task.exception():
            log.error("channel_inbox_completion_failed", channel_id=str(self.channel_id), error=str(task.exception()))
        self.wake.set()

    async def _claim(self, db) -> list[ChannelInboxEvent]:
        pending = select(ChannelInboxEvent).where(
            ChannelInboxEvent.channel_id == self.channel_id, ChannelInboxEvent.status == "queued",
        )
        # Controls must run during a long turn. Consume one control at a time so
        # it can cancel the previous turn before dispatching any subsequent text.
        control = await db.scalar(pending.where(ChannelInboxEvent.kind.in_(("stop", "recall")))
                                  .order_by(ChannelInboxEvent.id).limit(1).with_for_update())
        if control:
            if not await self._delivery_current(db, control):
                control.status, control.finished_at = "superseded", func.now()
                return []
            targets = [ChannelInboxEvent.channel_id == self.channel_id, ChannelInboxEvent.kind == "message",
                       ChannelInboxEvent.status.in_(("queued", "claimed")), ChannelInboxEvent.id < control.id]
            if control.kind == "stop":
                targets.extend([ChannelInboxEvent.chat_id == control.chat_id,
                                ChannelInboxEvent.external_id == control.external_id])
            else:
                targets.append(ChannelInboxEvent.message_id == control.message_id)
            dropped = await db.execute(update(ChannelInboxEvent).where(*targets).values(
                status="interrupted", payload=None, finished_at=func.now()))
            control.payload = {**control.payload, "raw": {**control.payload.get("raw", {}),
                               "_inbox_dropped": dropped.rowcount}}
            rows = [control]
        else:
            slots = MAX_ACTIVE - len(self.tasks)
            if slots <= 0:
                return []
            earlier = aliased(ChannelInboxEvent)
            blocked = exists(select(1).where(
                earlier.channel_id == self.channel_id, earlier.chat_id == ChannelInboxEvent.chat_id,
                earlier.external_id == ChannelInboxEvent.external_id,
                earlier.id < ChannelInboxEvent.id, earlier.status.in_(("queued", "claimed", "running")),
            ))
            rows = list((await db.scalars(pending.where(~blocked)
                                          .order_by(ChannelInboxEvent.id).limit(slots).with_for_update())).all())
        for row in rows:
            row.status = "claimed"
            row.owner = self.owner
        return rows

    async def _delivery_current(self, db, row):
        if row.payload.get("transport_version") != str(self.pipeline.transport_version):
            return False
        if row.kind == "recall":
            return True
        binding_id = await db.scalar(select(ChannelBinding.id).where(
            ChannelBinding.channel_id == self.channel_id, ChannelBinding.external_id == row.external_id,
        ).with_for_update(read=True))
        return row.payload["binding_id"] == (str(binding_id) if binding_id else None)

    async def _process(self, row: ChannelInboxEvent) -> None:
        from agent_platform.channels.pipeline import _RunningTask
        # A crash before this transaction commits leaves a claim safe to retry.
        async with get_session_factory()() as db, db.begin():
            live_lease = exists(select(1).where(ChannelInboxLease.channel_id == self.channel_id,
                                                ChannelInboxLease.owner == self.owner,
                                                ChannelInboxLease.expires_at > func.now()))
            delivery_current = await self._delivery_current(db, row)
            transition = {"status": "running"} if delivery_current else {"status": "superseded", "finished_at": func.now()}
            started = await db.scalar(update(ChannelInboxEvent).where(
                ChannelInboxEvent.id == row.id, ChannelInboxEvent.status == "claimed",
                ChannelInboxEvent.owner == self.owner, live_lease, exists(self._active_channel()),
            ).values(**transition).returning(ChannelInboxEvent.id))
        if started is None or not delivery_current:
            return
        status = "failed"
        try:
            payload = dict(row.payload)
            payload.pop("transport_version")
            payload["chat_kind"] = ChatKind(payload["chat_kind"])
            if payload["raw"].get("aeskey"):
                payload["raw"]["aeskey"] = decrypt_secret(payload["raw"]["aeskey"])
            if payload.get("attachment"):
                payload["attachment"] = AttachmentInfo(**payload["attachment"])
            event = InboundEvent(channel_id=self.channel_id, **payload)
            if row.kind == "stop":
                await self.pipeline._safe_handle_stop(event)
            elif row.kind == "recall":
                await self.pipeline._safe_handle_recall(event)
            else:
                key = self.pipeline._chat_key(event)
                self.pipeline._running[key] = _RunningTask(asyncio.current_task(), event.message_id)
                if event.message_id:
                    self.pipeline._trigger_index[event.message_id] = key
                try:
                    if await self.pipeline._safe_handle(event) is False:
                        return
                finally:
                    self.pipeline._running.pop(key, None)
                    if event.message_id:
                        self.pipeline._trigger_index.pop(event.message_id, None)
            status = "done"
        except asyncio.CancelledError:
            status = "uncertain"
            raise
        finally:
            values = {"status": status, "finished_at": func.now()}
            if status == "done":
                values["payload"] = None
            async with get_session_factory()() as db, db.begin():
                await db.execute(update(ChannelInboxEvent).where(
                    ChannelInboxEvent.id == row.id, ChannelInboxEvent.owner == self.owner,
                    ChannelInboxEvent.status == "running",
                ).values(**values))

    async def _cancel_turns(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        if self.worker is None:
            return
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None
        await self._cancel_turns()
        async with get_session_factory()() as db, db.begin():
            await db.execute(delete(ChannelInboxLease).where(
                ChannelInboxLease.channel_id == self.channel_id, ChannelInboxLease.owner == self.owner,
            ))

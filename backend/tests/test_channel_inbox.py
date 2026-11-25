"""Regression coverage using independent receiver/consumer sessions on PostgreSQL."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_platform.channels.adapter import InboundEvent
from agent_platform.channels.binding import unbind_external
from agent_platform.channels.inbox import ChannelUnavailable
from agent_platform.channels.pipeline import ChannelInboundPipeline
from agent_platform.db.models import (
    ChannelBinding,
    ChannelConfig,
    ChannelInboxEvent,
    ChannelInboxLease,
    ChannelSessionMapping,
    Session,
    Tenant,
    TenantMembership,
    User,
)


def config():
    return ChannelConfig(
        tenant_id=uuid4(), name="inbox", agent_id=uuid4(), app_id="app",
        app_secret_encrypted="unused", mode="webhook", status="enabled", channel_key=uuid4().hex,
        created_by=uuid4(),
    )


def message(channel, event_id="first", text="hello"):
    return InboundEvent(channel.id, event_id, "chat", "person", text, raw={"header": {"token": "secret"}})


def setup_worker(channel, engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("agent_platform.channels.inbox.get_session_factory", lambda: factory)
    monkeypatch.setattr("agent_platform.channels.pipeline.get_session_factory", lambda: factory)
    pipeline = ChannelInboundPipeline(channel, SimpleNamespace(send=AsyncMock()), None)
    return pipeline


async def test_durable_ack_dedup_order_and_restart(db_session, engine, monkeypatch):
    first, other = config(), config()
    db_session.add_all([first, other])
    await db_session.commit()
    receiver = setup_worker(first, engine, monkeypatch)
    independent = setup_worker(other, engine, monkeypatch)
    event = message(first)
    await asyncio.gather(receiver.receive(event), receiver.receive(event))
    await receiver.receive(message(first, "second"))
    await independent.receive(message(other))
    rows = list(await db_session.scalars(select(ChannelInboxEvent).order_by(ChannelInboxEvent.id)))
    assert len(rows) == 3
    assert all(row.status == "queued" and "token" not in str(row.payload) for row in rows)
    await receiver.shutdown()
    worker = setup_worker(first, engine, monkeypatch)
    worker._safe_handle = AsyncMock(return_value=True)
    competitor = setup_worker(first, engine, monkeypatch)
    competitor._safe_handle = AsyncMock(return_value=True)
    for _ in range(2):
        await asyncio.gather(worker.inbox._poll(), competitor.inbox._poll())
        await asyncio.gather(*worker.inbox.tasks, *competitor.inbox.tasks)
    for row in rows:
        await db_session.refresh(row)
    assert [(r.event_id, r.status) for r in rows] == [("first", "done"), ("second", "done"), ("first", "queued")]
    assert rows[0].payload is None
    handled = [p._safe_handle for p in (worker, competitor) if p._safe_handle.await_count]
    assert len(handled) == 1
    assert [c.args[0].event_id for c in handled[0].await_args_list] == ["first", "second"]


async def test_takeover_recovers_claimed_but_preserves_unknown_side_effect(db_session, engine, monkeypatch):
    channel = config()
    db_session.add(channel)
    await db_session.commit()
    worker = setup_worker(channel, engine, monkeypatch)
    worker._safe_handle = AsyncMock(return_value=True)
    await worker.receive(message(channel, "started"))
    await worker.receive(message(channel, "reserved"))
    rows = list(await db_session.scalars(select(ChannelInboxEvent).order_by(ChannelInboxEvent.id)))
    old_owner = uuid4()
    rows[0].status, rows[1].status = "running", "claimed"
    for row in rows:
        row.owner = old_owner
    db_session.add(ChannelInboxLease(channel_id=channel.id, owner=old_owner,
                                     expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db_session.commit()
    await worker.inbox._poll()
    await asyncio.gather(*worker.inbox.tasks)
    for row in rows:
        await db_session.refresh(row)
    assert rows[0].status == "uncertain" and rows[0].payload["text"] == "hello"
    assert rows[1].status == "done"
    assert worker._safe_handle.await_args.args[0].event_id == "reserved"


async def test_rotation_fences_prior_receiver(db_session, engine, monkeypatch):
    channel = config()
    db_session.add(channel)
    await db_session.commit()
    old = setup_worker(channel, engine, monkeypatch)
    channel.transport_version = uuid4()
    await db_session.commit()
    new = setup_worker(channel, engine, monkeypatch)
    with pytest.raises(ChannelUnavailable):
        await old.receive(message(channel))
    await new.receive(message(channel))


async def test_rebind_invalidates_mapping_and_queued_identity(db_session, engine, monkeypatch):
    channel = config()
    tenant = Tenant(id=channel.tenant_id, name="tenant", slug="inbox")
    users = [User(id=uuid4(), username=name, email=f"{name}@test.net", password_hash="x", tenant_id=tenant.id)
             for name in ("previous", "next")]
    db_session.add_all([channel, tenant, *users, *(TenantMembership(tenant_id=tenant.id, user_id=u.id) for u in users)])
    def binding(user):
        return ChannelBinding(channel_id=channel.id, tenant_id=tenant.id, external_id="person", user_id=user.id)
    await db_session.flush()
    original = Session(user_id=users[0].id, agent_id=channel.agent_id, title="private")
    db_session.add_all([original, binding(users[0])])
    await db_session.flush()
    mapped = ChannelSessionMapping(channel_id=channel.id, external_id="person", chat_id="chat", session_id=original.id)
    db_session.add(mapped)
    await db_session.commit()
    worker = setup_worker(channel, engine, monkeypatch)
    await worker.receive(message(channel))
    await unbind_external(db_session, tenant.id, channel.id, "person")
    await db_session.refresh(mapped)
    assert not mapped.is_active
    # Simulate a legacy active mapping that predates the unbind correction.
    mapped.is_active = True
    db_session.add(binding(users[1]))
    await db_session.commit()
    worker._drive_agent = AsyncMock()
    await worker.inbox._poll()
    await asyncio.gather(*worker.inbox.tasks)
    worker._drive_agent.assert_not_awaited()
    ctx = await worker._resolve_context(db_session, message(channel, "new"))
    assert ctx.user_id == users[1].id and ctx.session_id != original.id
    assert (await db_session.get(Session, ctx.session_id)).user_id == users[1].id


async def test_pending_file_survives_restart_and_expires_in_same_session(db_session, engine, monkeypatch):
    from agent_platform.channels.pipeline import _ResolvedContext, _take_pending_files

    channel = config()
    db_session.add(channel)
    await db_session.commit()
    worker = setup_worker(channel, engine, monkeypatch)
    for name in ("stale", "current"):
        await worker.receive(message(channel, name))
    ctx = _ResolvedContext(uuid4(), uuid4())
    rows = list(await db_session.scalars(select(ChannelInboxEvent).order_by(ChannelInboxEvent.id)))
    for row in rows:
        row.status = "awaiting_text"
        row.finished_at = datetime.now(UTC) - timedelta(hours=1 if row.event_id == "stale" else 0)
        row.payload = {"session_id": str(ctx.session_id), "user_id": str(ctx.user_id), "ref": {"filename": row.event_id}}
    await db_session.commit()
    await worker.shutdown()
    assert await _take_pending_files(db_session, message(channel), _ResolvedContext(uuid4(), uuid4())) == []
    assert await _take_pending_files(db_session, message(channel), ctx) == [{"filename": "current"}]
    await db_session.commit()
    assert all(row.status == "done" and row.payload is None for row in rows)


async def test_enable_after_rotation_replaces_registered_transport(db_session, monkeypatch):
    from agent_platform.channels.connection_manager import ChannelConnectionManager
    from agent_platform.channels.webhook import _webhook_registry
    from agent_platform.interface.routes import channels

    channel = config()
    channel.verification_token_encrypted = "prior"
    db_session.add(channel)
    await db_session.commit()
    manager = ChannelConnectionManager(None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(channel_connection_manager=manager)))
    user = SimpleNamespace(tenant_id=channel.tenant_id)
    monkeypatch.setattr(channels, "_verify_channel_credentials", AsyncMock())
    await manager.start_channel(channel)
    try:
        old = _webhook_registry[channel.channel_key]
        await channels.update_channel(channel.id, channels.ChannelUpdate(verification_token="new"), user, db_session, request)
        assert channel.channel_key not in _webhook_registry
        assert channel.status == "disabled"
        await channels.enable_channel(channel.id, request, user, db_session)
        new = _webhook_registry[channel.channel_key]
        assert new is not old and channel.status == "enabled"
        assert new.channel._verification_token == "new"
    finally:
        await manager.stop_all()

"""Account resets, device refreshes and owned sessions through real HTTP requests."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_platform.auth import routes as auth
from agent_platform.auth.jwt_handler import decode_token
from agent_platform.core.config import settings
from agent_platform.core.ui_action import ui_action_manager
from agent_platform.db.connection import get_db
from agent_platform.db.models import Agent, RefreshToken, Session, Tenant, TenantMembership, User
from agent_platform.interface.routes import sessions, ui_actions, users
from agent_platform.interface.routes import settings as profile

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def auth_http(engine, db_session, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def database():
        async with factory.begin() as db:
            yield db

    from unittest.mock import AsyncMock

    monkeypatch.setattr("agent_platform.core.throttle.exceeded", AsyncMock(return_value=False))
    monkeypatch.setattr("agent_platform.core.throttle.clear_counter", AsyncMock())
    app = FastAPI()
    for router in [auth.router, profile.router, users.router, sessions.router, ui_actions.router]:
        app.include_router(router)
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def register(http, username):
    result = await http.post("/api/auth/register", json={
        "username": username, "password": "test-password", "email": f"{username}@example.com",
    })
    assert result.status_code == 201, result.text
    return result.json()


def headers(pair):
    return {"Authorization": f"Bearer {pair['access_token']}"}


async def rotate(client, pair):
    return await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})


@pytest.mark.parametrize("by_admin", [True, False])
async def test_password_reset_revokes_both_credentials(auth_http, by_admin):
    client = auth_http
    admin = await register(client, "admin-test")
    before = await register(client, "password-test")
    uid = decode_token(before["access_token"]).sub
    if by_admin:
        result = await client.put(f"/api/admin/users/{uid}", headers=headers(admin),
                                  json={"password": "changed-password"})
    else:
        result = await client.put("/api/settings/password", headers=headers(before),
                                  json={"current_password": "test-password", "new_password": "changed-password"})
    assert result.status_code == 200, result.text
    assert (await client.get("/api/settings/profile", headers=headers(before))).status_code == 401
    assert (await rotate(client, before)).status_code == 401
    after = await client.post("/api/auth/login", json={
        "username_or_email": "password-test", "password": "changed-password",
    })
    assert after.status_code == 200
    assert (await rotate(client, after.json())).status_code == 200


async def test_concurrent_rotation_is_single_use_and_other_devices_remain_valid(auth_http):
    client = auth_http
    pair = await register(client, "two-devices")
    other = (await client.post("/api/auth/login", json={
        "username_or_email": "two-devices", "password": "test-password",
    })).json()
    first, second = await asyncio.gather(rotate(client, pair), rotate(client, pair))
    assert sorted([first.status_code, second.status_code]) == [200, 401]
    rotated = first if first.status_code == 200 else second
    assert (await rotate(client, rotated.json())).status_code == 401
    assert (await rotate(client, other)).status_code == 200


async def test_pre_upgrade_refresh_survives_rotation(auth_http, db_session):
    pair = await register(auth_http, "old-device")
    uid = UUID(decode_token(pair["access_token"]).sub)
    expires = datetime.now(UTC) + timedelta(hours=1)
    raw = jwt.encode({"sub": str(uid), "type": "refresh", "jti": str(uuid4()), "exp": expires},
                     settings.jwt.secret, algorithm=settings.jwt.algorithm)
    old_hash = hashlib.sha256(raw.encode()).hexdigest()
    db_session.add(RefreshToken(user_id=uid, token_hash=old_hash, expires_at=expires,
                                family_id=UUID(old_hash[:32])))
    await db_session.commit()
    migrated = await rotate(auth_http, {"refresh_token": raw})
    assert migrated.status_code == 200
    assert decode_token(migrated.json()["refresh_token"]).family_id == UUID(old_hash[:32])
    assert (await rotate(auth_http, migrated.json())).status_code == 200
    assert (await rotate(auth_http, pair)).status_code == 200


async def test_session_list_legacy_order_limit_and_opt_in_pinned_pages(auth_http, db_session):
    pair = await register(auth_http, "history-order")
    uid = UUID(decode_token(pair["access_token"]).sub)
    now = datetime.now(UTC)
    # Equal timestamps force a stable ID tie-breaker across pages.
    rows = [
        Session(id=UUID(int=i), user_id=uid, title=f"history {i}", updated_at=now)
        for i in range(1, 204)
    ]
    rows[0].is_pinned = True
    rows[0].updated_at = now - timedelta(days=2)
    db_session.add_all(rows)
    await db_session.commit()

    async def ids(**params):
        response = await auth_http.get("/api/sessions", params=params, headers=headers(pair))
        assert response.status_code == 200, response.text
        return [UUID(item["id"]).int for item in response.json()]

    assert await ids(limit=250) == list(range(203, 0, -1))
    assert await ids(limit=2) == [203, 202]
    assert await ids(limit=2, offset=2) == [201, 200]
    assert await ids(limit=2, pinned_first=True) == [1, 203]
    assert await ids(limit=2, offset=2, pinned_first=True) == [202, 201]


async def test_disabled_selection_recovery_and_object_authorization(auth_http, db_session):
    client = auth_http
    pair = await register(client, "tenant-test")
    uid = UUID(decode_token(pair["access_token"]).sub)
    user = await db_session.get(User, uid)
    old = await db_session.get(Tenant, user.tenant_id)
    other = Tenant(id=uuid4(), name="other", slug="other")
    agent = Agent(id=uuid4(), tenant_id=old.id, created_by=uid, name="old")
    former = Session(id=uuid4(), agent_id=agent.id, user_id=uid, title="100% former", is_pinned=True)
    current = Session(id=uuid4(), user_id=uid, title="100% available")
    db_session.add_all([other, agent, former, current, TenantMembership(user_id=uid, tenant_id=other.id)])
    old.is_active = False
    await db_session.commit()
    identity = await client.get("/api/settings/profile", headers=headers(pair))
    assert identity.status_code == 200
    assert identity.json()["username"] == "tenant-test" and identity.json()["tenant_name"] == ""
    assert identity.json()["display_name"] is None
    response = await client.get("/api/settings/tenants", headers=headers(pair))
    assert response.status_code == 200 and any(t["id"] == str(other.id) for t in response.json())
    response = await client.put("/api/settings/active-tenant", headers=headers(pair), json={"tenant_id": str(other.id)})
    assert response.status_code == 200
    pending = ui_action_manager.create_action(str(former.id), str(uid), "call", "ui_click", {},
                                               execution_tenant_id=old.id)
    try:
        result = await client.get("/api/sessions", params={"limit": 1, "source": "chat", "search": "%"},
                                  headers=headers(pair))
        assert result.status_code == 200
        assert [s["id"] for s in result.json()] == [str(current.id)]
        from agent_platform.interface.commands.handlers.session import cmd_sessions, cmd_switch
        from agent_platform.interface.commands.models import CommandContext

        ctx = CommandContext(user=user, user_id=str(uid), db=db_session, raw="/sessions",
                             execution_tenant_id=other.id, allow_private_sessions=False)
        titles = await cmd_sessions(ctx)
        assert "available" in titles.content and "former" not in titles.content
        ctx.args = {"id": str(former.id)}
        assert "switch_session_id" not in (await cmd_switch(ctx)).data
        base = f"/api/ui-actions/sessions/{former.id}"
        assert (await client.get(f"{base}/pending", headers=headers(pair))).status_code == 403
        assert (await client.post(f"{base}/cancel_all", headers=headers(pair))).status_code == 403
        assert (await client.post(f"/api/ui-actions/{pending.id}/respond", headers=headers(pair),
                                  json={"status": "ok"})).status_code == 403
        assert pending.result is None
        assert (await client.get("/api/sessions", params={"offset": -1}, headers=headers(pair))).status_code == 422
    finally:
        ui_action_manager.discard(pending.id)

    await db_session.refresh(user)
    user.tenant_id = old.id
    await db_session.commit()
    assert (await rotate(client, pair)).status_code == 200
    assert await db_session.scalar(select(User.tenant_id).where(User.id == uid)) == other.id


async def test_websocket_requires_authentication_frame_and_checks_password_reset(auth_http, engine, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_platform.interface.routes import chat

    pair = await register(auth_http, "ws-user")
    socket = AsyncMock()
    socket.app = SimpleNamespace(state=SimpleNamespace(tool_executor=None))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(chat, "get_session_factory", lambda: factory)
    session_id = uuid4()
    socket.receive_json.return_value = {"type": "message", "content": "hello"}
    await chat.chat_websocket(socket, session_id)
    assert socket.close.call_args.kwargs["code"] == 4001
    socket.receive_json.return_value = {"type": "authenticate", "token": pair["access_token"]}
    await chat.chat_websocket(socket, session_id)
    assert socket.close.call_args.kwargs["code"] == 4004  # Valid user, missing session.
    changed = await auth_http.put("/api/settings/password", headers=headers(pair),
                                  json={"current_password": "test-password", "new_password": "new-test-password"})
    assert changed.status_code == 200
    await chat.chat_websocket(socket, session_id)
    assert socket.close.call_args.kwargs["code"] == 4001

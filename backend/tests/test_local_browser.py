"""Browser capability and wire tests against a disposable, migrated PostgreSQL DB."""
import asyncio
import secrets
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aio_agent_platform.browser import service
from aio_agent_platform.browser.context import BrowserRunContext, browser_run
from aio_agent_platform.browser.gateway import BrowserGateway, Peer
from aio_agent_platform.browser.models import (
    BrowserCommand,
    BrowserDevice,
    BrowserPairing,
    BrowserSession,
)
from aio_agent_platform.browser.security import now, origin, unseal
from aio_agent_platform.browser.tools import NAMES, handle, register
from aio_agent_platform.core import chat_runs
from aio_agent_platform.core.config import settings
from aio_agent_platform.core.execution_scope import bind_execution_scope
from aio_agent_platform.db.models import Agent, Session, Tenant, TenantMembership, User
from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings.browser, "enabled", True)
    monkeypatch.setattr(settings.browser, "secret_key", SecretStr("test-browser-key-independent-of-jwt-123456789"))
    monkeypatch.setattr(settings.browser, "public_wss_url", "wss://browser.example.com/extension")


async def seed(db):
    a, b = Tenant(id=uuid4(), name="A", slug=uuid4().hex), Tenant(id=uuid4(), name="B", slug=uuid4().hex)
    user = User(id=uuid4(), username=uuid4().hex, email=f"{uuid4()}@test.invalid", password_hash="test", tenant_id=a.id)
    other = User(id=uuid4(), username=uuid4().hex, email=f"{uuid4()}@test.invalid", password_hash="test", tenant_id=a.id)
    agent = Agent(id=uuid4(), name="browser", tenant_id=a.id, created_by=user.id, visibility="private", enabled_tools=sorted(NAMES))
    chat = Session(id=uuid4(), user_id=user.id, agent_id=agent.id, source="chat")
    db.add_all([a, b, user, other, agent, chat, TenantMembership(user_id=user.id, tenant_id=a.id), TenantMembership(user_id=user.id, tenant_id=b.id), TenantMembership(user_id=other.id, tenant_id=a.id)])
    await db.commit()
    paired = await service.create_pairing(db, user.id, a.id, uuid4())
    secret = paired['url'].split('#')[1]
    token = secrets.token_urlsafe(32)
    receipt = await service.exchange(db, secret, "pair", token, "test browser")
    device = await db.get(BrowserDevice, UUID(receipt['device_id']))
    device.connected, device.connection_epoch, device.last_seen_at = True, 1, now()
    await db.commit()
    grant = await service.create_grant(db, user.id, chat.id, device.id, ['https://www.baidu.com', 'https://www.jd.com'])
    run = await chat_runs.reserve(db, user.id, chat.id, {'browser_grant_id': str(grant.id), 'browser_private': True})
    await service.bind_grant(db, grant.id, run, agent.id)
    session = await service.open_session(db, grant)
    await db.commit()
    return SimpleNamespace(a=a, b=b, user=user, other=other, agent=agent, chat=chat, secret=secret, token=token, device=device, grant=grant, run=run, session=session)


async def test_pairing_and_renewal_exact_retry_never_transfer_identity(db_session):
    f = await seed(db_session)
    receipt = await service.exchange(db_session, f.secret, 'pair', f.token, 'ignored')
    assert receipt['device_id'] == f.device.id.hex
    with pytest.raises(HTTPException):
        await service.exchange(db_session, f.secret, 'pair', secrets.token_urlsafe(32), 'hijack')
    next_token = secrets.token_urlsafe(32)
    renewed = await service.exchange(db_session, f.token, 'renew', next_token, 'ignored')
    await db_session.commit()
    assert renewed == await service.exchange(db_session, f.token, 'renew', next_token, 'ignored')
    with pytest.raises(HTTPException):
        await service.authenticate_device(db_session, f.token)
    assert (await service.authenticate_device(db_session, next_token)).user_id == f.user.id
    pairing = await db_session.scalar(select(BrowserPairing))
    assert pairing.secret_ciphertext is None
    assert next_token not in f.device.token_digest


async def test_actual_tenant_pinned_and_owner_membership_enforced(db_session):
    f = await seed(db_session)
    f.user.tenant_id = f.b.id
    await db_session.commit()
    assert (await service.execution_session(db_session, f.session.id, f.user.id))[1].tenant_id == f.a.id
    assert (await db_session.get(User, f.user.id)).tenant_id == f.b.id
    with pytest.raises(HTTPException) as error:
        await service.owned(db_session, BrowserSession, f.session.id, f.other.id)
    assert error.value.status_code == 404
    await db_session.execute(delete(TenantMembership).where(TenantMembership.user_id == f.user.id, TenantMembership.tenant_id == f.a.id))
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await service.execution_session(db_session, f.session.id, f.user.id)
    assert error.value.status_code == 403


async def test_no_shared_device_and_grant_not_reusable(db_session):
    f = await seed(db_session)
    with pytest.raises(HTTPException):
        await service.create_grant(db_session, f.other.id, f.chat.id, f.device.id, ['https://www.baidu.com'])
    with pytest.raises(HTTPException):
        await service.bind_grant(db_session, f.grant.id, f.run, f.agent.id)
    second = BrowserSession(user_id=f.user.id, tenant_id=f.a.id, device_id=f.device.id,
                            grant_id=uuid4(), chat_run_id=uuid4(), connection_epoch=1)
    db_session.add(second)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


class ScriptedPeer:
    def __init__(self, fixture):
        self.device_id, self.epoch, self.closed = fixture.device.id, 1, False
        self.calls = []
        self.url = 'https://www.baidu.com'
        self.fail = False

    async def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if self.fail:
            raise TimeoutError
        if method == 'tool.tab_create':
            self.url = params['url']
            return {'tab_id': 41, 'window_id': 6, 'url': self.url}
        if method == 'tool.tab_list':
            return {'tabs': [{'tab_id': 41, 'url': self.url}, {'tab_id': 999, 'url': 'https://private.example/'}]}
        if method == 'tool.cooperate':
            return {'tab_id': 41, 'context_id': 'single-use-context', 'kind': 'baseline', 'text': 'secret page content', 'ref': 'e1'}
        return {}


async def dispatch(factory, gateway, peer, command):
    assert await gateway.claim(peer) == command.id
    await gateway.execute(peer, command.id)
    async with factory() as db:
        return await db.get(BrowserCommand, command.id)


@pytest.mark.parametrize('remove_membership', [False, True])
async def test_close_receipt_committed_during_wait_keeps_access_checks(db_session, monkeypatch, remove_membership):
    f = await seed(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, autoflush=False)
    gateway, peer = BrowserGateway(factory), ScriptedPeer(f)
    command = await service.enqueue(db_session, f.session, 'close', {}, 'close-race')
    await db_session.commit()
    original = service.execution_session

    async def finish_before_session_check(db, session_id, user_id, **kwargs):
        await dispatch(factory, gateway, peer, command)
        if remove_membership:
            async with factory() as other_db:
                await other_db.execute(delete(TenantMembership).where(
                    TenantMembership.user_id == user_id, TenantMembership.tenant_id == f.a.id))
                await other_db.commit()
        return await original(db, session_id, user_id, **kwargs)

    monkeypatch.setattr(service, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(service, 'execution_session', finish_before_session_check)
    if remove_membership:
        with pytest.raises(HTTPException) as error:
            await service.wait_command(command.id, f.user.id)
        assert error.value.status_code == 403
    else:
        assert await service.wait_command(command.id, f.user.id) == {'closed': True}
    assert len([call for call in peer.calls if call[0] == 'tool.session_stop']) == 1


async def test_open_baidu_observe_confirm_fill_encrypted_and_single_use(db_session):
    f = await seed(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gateway, peer = BrowserGateway(factory), ScriptedPeer(f)
    command = await service.enqueue(db_session, f.session, 'open', {}, 'open-1')
    await db_session.commit()
    assert (await dispatch(factory, gateway, peer, command)).status == 'succeeded'
    navigate = await service.enqueue(db_session, f.session, 'navigate', {'url': 'https://www.baidu.com'}, 'nav-1', approval=True)
    await db_session.commit()
    assert await gateway.claim(peer) is None
    with pytest.raises(HTTPException):
        await service.approve_command(db_session, f.other.id, navigate.id, True, navigate.payload_digest)
    with pytest.raises(HTTPException):
        await service.approve_command(db_session, f.user.id, navigate.id, True, '0' * 64)
    await service.approve_command(db_session, f.user.id, navigate.id, True, navigate.payload_digest)
    await db_session.commit()
    assert (await dispatch(factory, gateway, peer, navigate)).status == 'succeeded'
    observe = await service.enqueue(db_session, f.session, 'observe', {}, 'obs-1')
    await db_session.commit()
    result = await dispatch(factory, gateway, peer, observe)
    assert 'secret page content' not in result.result_ciphertext
    content = unseal(result.result_ciphertext)
    assert content['page']['kind'] == 'baseline'
    observation_id = UUID(content['observation_handle'])
    payload = {'observation_handle': str(observation_id), 'action': 'fill', 'ref': 'e1', 'value': '耳机'}
    command = await service.enqueue(db_session, f.session, 'interact', payload, 'fill-1', approval=True)
    await service.claim_observation(db_session, f.session, observation_id, command.id)
    await service.approve_command(db_session, f.user.id, command.id, True, command.payload_digest)
    await db_session.commit()
    assert (await dispatch(factory, gateway, peer, command)).status == 'succeeded'
    assert peer.calls[-1] == ('tool.fill', {'session_id': f.session.id.hex, 'tab_id': 41, 'context_id': 'single-use-context', 'ref': 'e1', 'value': '耳机'})
    with pytest.raises(HTTPException):
        await service.claim_observation(db_session, f.session, observation_id, uuid4())
    assert await gateway.claim(peer) is None


async def test_timeout_unknown_not_replayed_and_recovery_quarantines(db_session):
    f = await seed(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gateway, peer = BrowserGateway(factory), ScriptedPeer(f)
    peer.fail = True
    command = await service.enqueue(db_session, f.session, 'open', {}, 'open-timeout')
    await db_session.commit()
    row = await dispatch(factory, gateway, peer, command)
    assert row.status == 'unknown' and row.effect_state == 'unknown'
    assert await gateway.claim(peer) is None
    await gateway.recover()
    async with factory() as db:
        session = await db.get(BrowserSession, f.session.id)
        assert session.occupied and session.stop_requested
        assert not (await db.get(BrowserDevice, f.device.id)).connected


async def test_expired_approval_and_lease_loss_refuse_dispatch(db_session):
    f = await seed(db_session)
    command = await service.enqueue(db_session, f.session, 'navigate', {'url': 'https://www.jd.com'}, 'nav-expired', approval=True)
    command.expires_at = now() - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await service.approve_command(db_session, f.user.id, command.id, True, command.payload_digest)
    f.run.heartbeat_at = now() - timedelta(minutes=1)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await service.execution_session(db_session, f.session.id, f.user.id)


async def test_handler_requires_root_run_context(db_session, monkeypatch):
    f = await seed(db_session)
    with pytest.raises(HTTPException):
        await handle('open', {}, str(f.user.id), str(f.chat.id), tool_call_id='call')
    token = browser_run.set(BrowserRunContext(f.run.id, f.grant.id, f.user.id))
    try:
        with pytest.raises(HTTPException):
            await handle('open', {}, str(f.user.id), str(f.chat.id), tool_call_id='call', delegation=SimpleNamespace(delegation_depth=1))
        with bind_execution_scope(f.user.id, f.b.id, agent_id=f.agent.id):
            monkeypatch.setattr(service, 'get_session_factory', lambda: async_sessionmaker(db_session.bind, expire_on_commit=False))
            import aio_agent_platform.browser.tools as browser_tools
            monkeypatch.setattr(browser_tools, 'get_session_factory', service.get_session_factory)
            with pytest.raises(HTTPException):
                await handle('open', {}, str(f.user.id), str(f.chat.id), tool_call_id='call')
    finally:
        browser_run.reset(token)


async def test_persistent_authorization_requires_owner_and_revocation_never_revives_grants(db_session):
    f = await seed(db_session)
    with pytest.raises(HTTPException):
        await service.create_grant(db_session, f.user.id, f.chat.id, f.device.id, [], persistent=True)
    with pytest.raises(HTTPException):
        await service.set_persistent_authorization(db_session, f.other.id, f.device.id, True)
    await service.set_persistent_authorization(db_session, f.user.id, f.device.id, True)
    await db_session.commit()
    grant = await service.create_grant(db_session, f.user.id, f.chat.id, f.device.id, [], persistent=True)
    await db_session.commit()
    assert grant.expires_at is None
    assert service.allows_url(grant, 'https://www.jd.com')
    assert service.allows_url(grant, 'https://www.example.org/new-page')
    with pytest.raises(HTTPException):
        service.allows_url(grant, 'http://127.0.0.1/admin')
    f.user.tenant_id = f.b.id
    await db_session.commit()
    assert (await service.validate_grant(db_session, grant)).tenant_id == f.a.id
    await service.set_persistent_authorization(db_session, f.user.id, f.device.id, False)
    await db_session.commit()
    await db_session.refresh(grant)
    with pytest.raises(HTTPException):
        await service.validate_grant(db_session, grant)
    await service.set_persistent_authorization(db_session, f.user.id, f.device.id, True)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await service.validate_grant(db_session, grant)
    replacement = await service.create_grant(db_session, f.user.id, f.chat.id, f.device.id, [], persistent=True)
    assert replacement.device_authorization_version != grant.device_authorization_version


@pytest.mark.parametrize('persistent', [False, True])
async def test_persistent_navigate_skips_confirmation_only_after_valid_grant(db_session, monkeypatch, persistent):
    import aio_agent_platform.browser.tools as browser_tools
    f = await seed(db_session)
    if persistent:
        f.device.persistent_authorized_at = now()
        f.device.authorization_version = 1
        f.grant.device_authorization_version = 1
        f.grant.origins = ['*']
        f.grant.expires_at = None
        await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(browser_tools, 'get_session_factory', lambda: factory)
    async def receipt(command_id, user_id):
        async with factory() as db:
            command = await db.get(BrowserCommand, command_id)
            assert command.status == ('queued' if persistent else 'pending')
        return {}
    monkeypatch.setattr(service, 'wait_command', receipt)
    token = browser_run.set(BrowserRunContext(f.run.id, f.grant.id, f.user.id))
    try:
        with bind_execution_scope(f.user.id, f.a.id, agent_id=f.agent.id):
            await handle('navigate', {'handle': str(f.session.id), 'url': 'https://www.baidu.com'},
                         str(f.user.id), str(f.chat.id), tool_call_id='trusted-nav')
            if persistent:
                f.device.authorization_version += 1
                await db_session.commit()
                with pytest.raises(HTTPException):
                    await handle('navigate', {'handle': str(f.session.id), 'url': 'https://www.jd.com'},
                                 str(f.user.id), str(f.chat.id), tool_call_id='revoked-nav')
    finally:
        browser_run.reset(token)


def test_no_implicit_tools_no_private_tracing():
    from aio_agent_platform.browser.context import private_browser_history
    from aio_agent_platform.core.chat import filter_tools_by_agent
    from aio_agent_platform.observation.client import get_langfuse_client
    executor = ToolExecutor(registry=ToolRegistry(), sandbox_mgr=None)
    register(executor)
    assert not filter_tools_by_agent(executor, None)[0]
    assert not filter_tools_by_agent(executor, None, allow_local_browser=True)[0]
    token = private_browser_history.set(True)
    try:
        assert get_langfuse_client() is None
    finally:
        private_browser_history.reset(token)


def test_browser_confirmation_stays_inside_capability_handler():
    from aio_agent_platform.tools.policy import requires_approval
    executor = ToolExecutor(registry=ToolRegistry(), sandbox_mgr=None)
    register(executor)
    assert not requires_approval(executor, 'local_browser_interact', 'ask_always')
    assert requires_approval(executor, 'local_browser_unknown', 'ask_dangerous')


@pytest.mark.parametrize('purpose', ['login', 'verification', 'payment'])
async def test_sensitive_interaction_requires_human_handoff(purpose):
    user_id, chat_id = uuid4(), uuid4()
    token = browser_run.set(BrowserRunContext(uuid4(), uuid4(), user_id))
    try:
        with pytest.raises(HTTPException) as error:
            await handle('interact', {'handle': str(uuid4()), 'observation_handle': str(uuid4()),
                                     'action': 'click', 'ref': 'e1', 'purpose': purpose},
                         str(user_id), str(chat_id), tool_call_id='sensitive')
        assert error.value.status_code == 403
        assert 'local_browser_request_help' in error.value.detail
    finally:
        browser_run.reset(token)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'chrome://settings', 'http://127.0.0.1', 'http://[::1]', 'http://localhost', 'https://a:b@example.com'])
def test_bad_origins(url):
    with pytest.raises(HTTPException):
        origin(url)


async def test_protocol_handshake_no_query_auth(db_session):
    f = await seed(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gateway = BrowserGateway(factory)
    app = FastAPI()
    @app.websocket('/extension')
    async def ws(socket: WebSocket):
        await gateway.websocket(socket)
    # TestClient owns its event loop; NullPool (test engine) prevents loop reuse.
    def run_client():
        with TestClient(app) as client:
            with client.websocket_connect('/extension', headers={'origin': 'chrome-extension://' + 'a' * 32}, subprotocols=['bsk-auth.' + f.token]) as socket:
                socket.send_json({'id': 'h', 'method': 'system.handshake', 'params': {'protocol_version': '1.7', 'min_compatible_protocol': '1.6', 'version': '0.4.0', 'instance_id': str(f.other.id)}})
                result = socket.receive_json()
                assert result['id'] == 'h' and result['result']['protocol_version'] == '1.7'
                socket.send_json({'id': 'unknown', 'result': {'secret': 'ignored'}})
    await asyncio.to_thread(run_client)
    await db_session.refresh(f.device)
    assert f.device.user_id == f.user.id and f.device.connection_epoch == 2
    assert not f.device.connected


async def test_peer_cancellation_sends_cancel_not_replay():
    frames = []
    class Socket:
        async def send_json(self, frame):
            frames.append(frame)
    peer = Peer(Socket(), uuid4(), 1)
    task = asyncio.create_task(peer.call('tool.click', {'ref': 'e1'}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [f['method'] for f in frames] == ['tool.click', 'cancel']
    assert frames[1]['params']['rpc_id'] == frames[0]['id']
    assert not peer.pending


async def test_gateway_heartbeat_survives_active_session_authorization_refresh(db_session, monkeypatch):
    f = await seed(db_session)
    f.session.state = 'active'
    f.device.last_seen_at = now() - timedelta(minutes=2)
    await db_session.commit()
    # Match production: owned(..., populate_existing=True) must not discard an
    # unflushed heartbeat when authorizing the active session in this transaction.
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, autoflush=False)
    gateway = BrowserGateway(factory)
    class Socket:
        async def close(self, **kwargs):
            pass
    peer = Peer(Socket(), f.device.id, f.device.connection_epoch)
    cleaned = []
    async def claim(_peer):
        peer.closed = True
        return None
    async def cleanup(_peer, session_id):
        cleaned.append(session_id)
        peer.closed = True
    monkeypatch.setattr(gateway, 'claim', claim)
    monkeypatch.setattr(gateway, 'cleanup', cleanup)
    await asyncio.wait_for(gateway.supervise(peer), 10)
    await db_session.refresh(f.device)
    await db_session.refresh(f.session)
    assert not cleaned
    assert not f.session.stop_requested
    assert service.online(f.device)


async def test_browser_wait_yields_and_cancel_drains_child(monkeypatch):
    from unittest.mock import Mock

    from aio_agent_platform.core.agent import AgentLoop, AgentStep, ToolContext
    from aio_agent_platform.core.agent_events import ControlEvent
    from aio_agent_platform.llm import ToolCall
    cancelled = asyncio.Event()
    async def execute(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    executor = ToolExecutor(registry=ToolRegistry(), sandbox_mgr=None)
    register(executor)
    loop = AgentLoop(Mock(), executor, execution_tenant_id=uuid4())
    monkeypatch.setattr(loop, '_execute_tool_traced', execute)
    step = AgentStep(step=1, tool_calls=[ToolCall('c', 'local_browser_navigate', {'url': 'private-query'})])
    events = loop._execute_step_tools(step, ToolContext(uuid4(), uuid4()))
    event = await anext(events)
    assert isinstance(event, ControlEvent) and event.kind == 'browser_flow'
    await events.aclose()
    assert cancelled.is_set()


async def test_http_owner_scope_and_original_tenant_after_ui_switch(db_session):
    from httpx import ASGITransport, AsyncClient

    from aio_agent_platform.auth.jwt_handler import create_access_token
    from aio_agent_platform.browser.routes import router
    from aio_agent_platform.db.connection import get_db
    f = await seed(db_session)
    f.user.tenant_id = f.b.id
    f.b.is_active = False
    await db_session.commit()
    app = FastAPI()
    app.include_router(router)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async def database():
        async with factory() as db:
            yield db
            await db.commit()
    app.dependency_overrides[get_db] = database
    headers = {'Authorization': 'Bearer ' + create_access_token(f.user.id, 'user')}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(f'/api/local-browser/chats/{f.chat.id}', headers=headers)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['devices'][0]['id'] == str(f.device.id)
        response = await client.post(f'/api/local-browser/sessions/{f.session.id}/stop', headers={'Authorization': 'Bearer ' + create_access_token(f.other.id, 'superadmin')})
        assert response.status_code == 404
        response = await client.post(f'/api/local-browser/sessions/{f.session.id}/stop', headers=headers)
        assert response.status_code == 200
        assert (await client.get('/api/local-browser/devices', headers=headers)).status_code == 403


async def test_private_history_blocks_rebinding_and_removed_members(db_session):
    f = await seed(db_session)
    f.chat.browser_private = True
    await db_session.commit()
    await service.private_chat_scope(db_session, f.chat, f.user.id)
    f.agent.tenant_id = f.b.id
    await db_session.commit()
    with pytest.raises(HTTPException):
        await service.private_chat_scope(db_session, f.chat, f.user.id)
    assert not chat_runs.describe(f.run)['can_resume']


async def test_denied_read_keeps_session_and_redacts_redirect_metadata(db_session):
    f = await seed(db_session)
    f.session.state = 'active'
    f.session.tabs, f.session.active_tab = {'page': 41}, 'page'
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gateway, peer = BrowserGateway(factory), ScriptedPeer(f)
    peer.url = 'https://unapproved.example/?token=private'
    command = await service.enqueue(db_session, f.session, 'screenshot', {}, 'read-denied')
    await db_session.commit()
    row = await dispatch(factory, gateway, peer, command)
    assert row.status == 'failed' and row.effect_state == 'none'
    await db_session.refresh(f.session)
    assert not f.session.stop_requested
    command = await service.enqueue(db_session, f.session, 'tabs', {}, 'tabs-redirect')
    await db_session.commit()
    row = await dispatch(factory, gateway, peer, command)
    result = unseal(row.result_ciphertext)
    assert result == {'tabs': [{'tab_handle': 'page', 'origin': 'https://unapproved.example', 'needs_authorization': True}]}


async def test_retention_erases_content_but_preserves_receipt(db_session):
    from aio_agent_platform.browser.security import seal
    f = await seed(db_session)
    command = await service.enqueue(db_session, f.session, 'open', {}, 'expired-content')
    command.status = 'succeeded'
    command.created_at = now() - timedelta(days=2)
    command.result_ciphertext = seal({'private': 'page'})
    command.effect_state = 'committed'
    await db_session.commit()
    gateway = BrowserGateway(async_sessionmaker(db_session.bind, expire_on_commit=False))
    await gateway.expire_content()
    await db_session.refresh(command)
    assert command.payload_ciphertext == '' and command.result_ciphertext is None
    assert command.status == 'succeeded' and command.effect_state == 'committed'


async def test_gateway_single_owner_lock_is_released_on_shutdown(db_session, monkeypatch):
    from aio_agent_platform.browser import app as gateway_app
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(gateway_app, 'get_engine', lambda: db_session.bind)
    monkeypatch.setattr(gateway_app, 'get_session_factory', lambda: factory)
    first, second = FastAPI(), FastAPI()
    async with gateway_app.lifespan(first):
        assert first.state.ready
        with pytest.raises(RuntimeError, match='已有实例'):
            async with gateway_app.lifespan(second):
                pytest.fail('second gateway must not acquire the lock')
    assert not first.state.ready
    async with gateway_app.lifespan(second):
        assert second.state.ready

"""Database-backed capabilities. All browser identity comes from owned records."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.browser.models import (
    BrowserCommand,
    BrowserDevice,
    BrowserGrant,
    BrowserObservation,
    BrowserPairing,
    BrowserSession,
)
from agent_platform.browser.security import (
    aware,
    digest,
    enabled,
    now,
    origin,
    public_endpoint,
    seal,
    unseal,
    valid_token,
)
from agent_platform.core.config import settings
from agent_platform.core.execution_scope import ExecutionAccessError
from agent_platform.core.execution_scope import require_agent_tenant as _require_agent_tenant
from agent_platform.core.execution_scope import (
    require_execution_user as _require_execution_user,
)
from agent_platform.db.connection import get_session_factory, set_db_user
from agent_platform.db.models import Agent, ChatRun, Session


async def require_execution_user(db, user_id, tenant_id):
    await set_db_user(db, user_id)
    try:
        return await _require_execution_user(db, user_id, tenant_id)
    except ExecutionAccessError:
        raise HTTPException(403, "用户或租户授权已失效") from None


async def require_agent_tenant(db, user_id, agent_id):
    await set_db_user(db, user_id)
    try:
        return await _require_agent_tenant(db, user_id, agent_id)
    except ExecutionAccessError:
        raise HTTPException(403, "智能体或租户授权已失效") from None


TERMINAL_COMMANDS = {"succeeded", "failed", "unknown", "cancelled", "rejected"}
READ_OPERATIONS = {"observe", "screenshot", "tabs", "preview"}


def missing() -> HTTPException:
    return HTTPException(404, "浏览器资源不存在或无权访问")


async def owned(db: AsyncSession, model, object_id: UUID, user_id: UUID, *, lock=False):
    stmt = select(model).where(model.id == object_id, model.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    value = await db.scalar(stmt.execution_options(populate_existing=True))
    if value is None:
        raise missing()
    await require_execution_user(db, user_id, value.tenant_id)
    return value


def online(device: BrowserDevice) -> bool:
    return bool(device.connected and not device.revoked_at and device.last_seen_at
                and aware(device.expires_at) > now()
                and aware(device.last_seen_at) > now() - timedelta(seconds=settings.browser.heartbeat_timeout))


def device_view(device: BrowserDevice) -> dict:
    return {"id": str(device.id), "tenant_id": str(device.tenant_id), "label": device.label,
            "online": online(device), "revoked": device.revoked_at is not None,
            "persistent_authorized": device.persistent_authorized_at is not None and device.revoked_at is None,
            "expires_at": device.expires_at, "extension_version": device.extension_version,
            "protocol_version": device.protocol_version}


async def create_pairing(db, user_id: UUID, tenant_id: UUID, request_id: UUID) -> dict:
    enabled()
    endpoint = public_endpoint()
    await require_execution_user(db, user_id, tenant_id)
    row = await db.scalar(select(BrowserPairing).where(
        BrowserPairing.user_id == user_id, BrowserPairing.tenant_id == tenant_id,
        BrowserPairing.request_id == request_id).with_for_update())
    if row:
        if aware(row.expires_at) <= now() or row.device_id or not row.secret_ciphertext:
            raise HTTPException(409, "此配对已完成或过期，请显式重新生成")
        secret = unseal(row.secret_ciphertext)
    else:
        count = await db.scalars(select(BrowserPairing.id).where(
            BrowserPairing.user_id == user_id, BrowserPairing.expires_at > now(),
            BrowserPairing.device_id.is_(None)))
        if len(count.all()) >= 5:
            raise HTTPException(429, "有效配对链接过多，请等待过期")
        secret = secrets.token_urlsafe(32)
        row = BrowserPairing(tenant_id=tenant_id, user_id=user_id, request_id=request_id,
                             secret_digest=digest(secret), secret_ciphertext=seal(secret),
                             expires_at=now() + timedelta(seconds=settings.browser.pairing_ttl))
        db.add(row)
        await db.flush()
    return {"id": str(row.id), "url": f"{endpoint}#{secret}", "expires_at": row.expires_at}


def credential_receipt(device: BrowserDevice) -> dict:
    return {"device_id": device.id.hex, "expires_at": aware(device.expires_at).isoformat(),
            "renew_after": (aware(device.expires_at) - timedelta(seconds=settings.browser.credential_ttl // 7)).isoformat(),
            "service_name": "Agent Platform"}


async def exchange(db, token: str, action: str, next_token: str, label: str) -> dict:
    enabled()
    if not valid_token(token) or not valid_token(next_token) or token == next_token:
        raise HTTPException(401, "无效的设备授权")
    old_hash, new_hash = digest(token), digest(next_token)
    if action == "pair":
        pairing = await db.scalar(select(BrowserPairing).where(
            BrowserPairing.secret_digest == old_hash).with_for_update())
        if pairing is None or aware(pairing.expires_at) <= now():
            raise HTTPException(401, "配对已过期或无效")
        await require_execution_user(db, pairing.user_id, pairing.tenant_id)
        if pairing.device_id:
            device = await db.get(BrowserDevice, pairing.device_id)
            if (device is None or device.revoked_at or pairing.next_digest != new_hash
                    or aware(device.expires_at) <= now()):
                raise HTTPException(401, "配对已被使用")
            return pairing.receipt
        device = BrowserDevice(id=uuid4(), tenant_id=pairing.tenant_id, user_id=pairing.user_id,
                               label=label or "我的浏览器", token_digest=new_hash,
                               expires_at=now() + timedelta(seconds=settings.browser.credential_ttl))
        db.add(device)
        pairing.device_id = device.id
        pairing.next_digest = new_hash
        pairing.secret_ciphertext = None
        pairing.receipt = credential_receipt(device)
        await db.flush()
        return pairing.receipt
    if action != "renew":
        raise HTTPException(422, "不支持的授权操作")
    device = await db.scalar(select(BrowserDevice).where(or_(
        BrowserDevice.token_digest == old_hash, BrowserDevice.previous_digest == old_hash,
    )).with_for_update())
    if device is None or device.revoked_at or aware(device.expires_at) <= now():
        raise HTTPException(401, "设备授权已失效")
    await require_execution_user(db, device.user_id, device.tenant_id)
    if device.previous_digest == old_hash:
        if device.token_digest != new_hash or not device.retry_until or aware(device.retry_until) <= now():
            raise HTTPException(401, "续期重试与原请求不匹配")
        return device.renewal_receipt
    device.previous_digest, device.token_digest = old_hash, new_hash
    device.expires_at = now() + timedelta(seconds=settings.browser.credential_ttl)
    device.retry_until = now() + timedelta(minutes=10)
    device.renewal_receipt = credential_receipt(device)
    return device.renewal_receipt


async def authenticate_device(db, token: str) -> BrowserDevice:
    enabled()
    if not valid_token(token):
        raise HTTPException(401, "无效设备凭证")
    device = await db.scalar(select(BrowserDevice).where(BrowserDevice.token_digest == digest(token)))
    if device is None or device.revoked_at or aware(device.expires_at) <= now():
        raise HTTPException(401, "设备授权已失效")
    await require_execution_user(db, device.user_id, device.tenant_id)
    return device


async def create_grant(db, user_id: UUID, session_id: UUID, device_id: UUID, urls: list[str], *, persistent=False) -> BrowserGrant:
    enabled()
    chat = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id))
    if chat is None or not chat.agent_id or chat.source not in {"chat", "pet"}:
        raise HTTPException(403, "浏览器仅支持绑定智能体的 Web 聊天")
    tenant = await require_agent_tenant(db, user_id, chat.agent_id)
    agent = await db.get(Agent, chat.agent_id)
    if "local_browser_open" not in (agent.enabled_tools or []):
        raise HTTPException(403, "请先在智能体工具配置中显式启用本地浏览器工具")
    device = await owned(db, BrowserDevice, device_id, user_id, lock=True)
    if device.tenant_id != tenant:
        raise missing()
    if not online(device):
        raise HTTPException(409, "浏览器不在线，请在本地扩展连接")
    if persistent and not device.persistent_authorized_at:
        raise HTTPException(403, "此设备尚未获得持续授权")
    origins = ["*"] if persistent else sorted({origin(url) for url in urls})
    if not origins or len(origins) > 10:
        raise HTTPException(422, "本次任务需指定 1 至 10 个网站地址")
    grant = BrowserGrant(tenant_id=tenant, user_id=user_id, device_id=device.id,
                         agent_id=chat.agent_id, chat_session_id=chat.id, origins=origins,
                         expires_at=None if persistent else now() + timedelta(seconds=settings.browser.grant_ttl),
                         device_authorization_version=device.authorization_version if persistent else None)
    db.add(grant)
    await db.flush()
    return grant


async def validate_grant(db, grant: BrowserGrant, *, executing=False, tool: str | None = None) -> BrowserDevice:
    enabled()
    await require_execution_user(db, grant.user_id, grant.tenant_id)
    tenant = await require_agent_tenant(db, grant.user_id, grant.agent_id)
    chat = await db.get(Session, grant.chat_session_id)
    if tenant != grant.tenant_id or not chat or chat.user_id != grant.user_id or chat.agent_id != grant.agent_id or chat.source not in {"chat", "pet"}:
        raise missing()
    agent = await db.get(Agent, grant.agent_id)
    if "local_browser_open" not in (agent.enabled_tools or []) or (tool and tool not in (agent.enabled_tools or [])):
        raise HTTPException(403, "智能体未获准使用此浏览器工具")
    if grant.revoked_at or (grant.expires_at is not None and aware(grant.expires_at) <= now()):
        raise HTTPException(403, "本次任务的浏览器授权已失效")
    device = await owned(db, BrowserDevice, grant.device_id, grant.user_id)
    if device.tenant_id != grant.tenant_id or not online(device):
        raise HTTPException(409, "浏览器不在线或授权已失效")
    if grant.device_authorization_version is not None:
        if not device.persistent_authorized_at or device.authorization_version != grant.device_authorization_version:
            raise HTTPException(403, "设备的持续授权已撤销或变更，请重新发起任务")
    elif grant.expires_at is None or "*" in grant.origins:
        raise HTTPException(403, "浏览器授权范围无效")
    if executing:
        run = await db.get(ChatRun, grant.chat_run_id) if grant.chat_run_id else None
        if (not run or run.user_id != grant.user_id or run.session_id != grant.chat_session_id
                or run.status != "running" or run.stop_requested
                or str(run.request.get("browser_grant_id")) != str(grant.id)
                or aware(run.heartbeat_at) < now() - timedelta(seconds=30)):
            raise HTTPException(409, "浏览器任务已结束或执行租约失效")
    return device


async def bind_grant(db, grant_id: UUID, run: ChatRun, agent_id: UUID | None) -> BrowserGrant:
    grant = await owned(db, BrowserGrant, grant_id, run.user_id, lock=True)
    await validate_grant(db, grant)
    if grant.chat_run_id is not None or grant.chat_session_id != run.session_id or grant.agent_id != agent_id:
        raise HTTPException(409, "浏览器授权已被使用或与本次任务不匹配")
    grant.chat_run_id = run.id
    return grant


def allows_url(grant: BrowserGrant, url: str) -> bool:
    destination = origin(url)  # All-site authorization still rejects local/privileged URLs.
    return destination in grant.origins or (grant.device_authorization_version is not None and grant.origins == ["*"])


async def set_persistent_authorization(db, user_id: UUID, device_id: UUID, authorized: bool):
    device = await owned(db, BrowserDevice, device_id, user_id, lock=True)
    if device.revoked_at:
        raise HTTPException(403, "设备连接已撤销，请重新配对")
    if bool(device.persistent_authorized_at) == authorized:
        return device
    device.persistent_authorized_at = now() if authorized else None
    device.authorization_version += 1
    # Flush before owner refreshes; production sessions disable autoflush.
    await db.flush()
    for grant in (await db.scalars(select(BrowserGrant).where(
            BrowserGrant.device_id == device.id, BrowserGrant.device_authorization_version.is_not(None),
            BrowserGrant.revoked_at.is_(None)))).all():
        grant.revoked_at = now()
    for session in (await db.scalars(select(BrowserSession).where(
            BrowserSession.device_id == device.id, BrowserSession.occupied))).all():
        await request_stop(db, session)
    return device


async def execution_session(db, session_id: UUID, user_id: UUID, *, tool=None, lock=False):
    session = await owned(db, BrowserSession, session_id, user_id, lock=lock)
    grant = await owned(db, BrowserGrant, session.grant_id, user_id)
    device = await validate_grant(db, grant, executing=True, tool=tool)
    if (session.tenant_id != grant.tenant_id or session.device_id != device.id
            or session.chat_run_id != grant.chat_run_id or session.connection_epoch != device.connection_epoch):
        raise missing()
    if session.stop_requested or session.state not in {"starting", "active", "waiting_user"}:
        raise HTTPException(409, "浏览器会话已失效，请核对结果后重新授权")
    return session, grant, device


async def open_session(db, grant: BrowserGrant) -> BrowserSession:
    device = await validate_grant(db, grant, executing=True)
    # Serializes duplicate open calls for the same grant. Different grants are
    # fenced by the partial unique index on the occupied device.
    await db.execute(select(BrowserGrant.id).where(BrowserGrant.id == grant.id).with_for_update())
    existing = await db.scalar(select(BrowserSession).where(BrowserSession.grant_id == grant.id))
    if existing:
        await execution_session(db, existing.id, grant.user_id)
        return existing
    busy = await db.scalar(select(BrowserSession.id).where(
        BrowserSession.device_id == device.id, BrowserSession.occupied))
    if busy:
        raise HTTPException(409, "此浏览器正在执行任务或等待清理，请先结束原任务")
    session = BrowserSession(id=uuid4(), user_id=grant.user_id, tenant_id=grant.tenant_id,
                             grant_id=grant.id, device_id=device.id, chat_run_id=grant.chat_run_id,
                             connection_epoch=device.connection_epoch)
    db.add(session)
    await db.flush()
    return session


async def enqueue(db, session: BrowserSession, operation: str, payload: dict, request_id: str,
                  *, approval=False) -> BrowserCommand:
    encoded = json.dumps([operation, payload], sort_keys=True, ensure_ascii=False)
    fingerprint = digest(encoded)
    previous = await db.scalar(select(BrowserCommand).where(
        BrowserCommand.browser_session_id == session.id, BrowserCommand.request_id == request_id))
    if previous:
        if previous.payload_digest != fingerprint:
            raise HTTPException(409, "相同操作标识不能更换参数")
        return previous
    command = BrowserCommand(id=uuid4(), user_id=session.user_id, tenant_id=session.tenant_id,
                             browser_session_id=session.id, request_id=request_id, operation=operation,
                             payload_ciphertext=seal(payload), payload_digest=fingerprint,
                             status="pending" if approval else "queued",
                             expires_at=now() + timedelta(seconds=settings.browser.approval_timeout if approval else settings.browser.rpc_timeout + 30))
    db.add(command)
    await db.flush()
    return command


async def approve_command(db, user_id: UUID, command_id: UUID, approved: bool, payload_digest: str):
    command = await owned(db, BrowserCommand, command_id, user_id, lock=True)
    await execution_session(db, command.browser_session_id, user_id)
    if command.payload_digest != payload_digest:
        raise HTTPException(409, "操作内容已变化，请重新查看")
    if command.status != "pending":
        return command
    if aware(command.expires_at) <= now():
        raise HTTPException(409, "此操作确认已过期")
    command.status = "queued" if approved else "rejected"
    command.approved_at = now() if approved else None
    command.expires_at = now() + timedelta(seconds=settings.browser.rpc_timeout + 30)
    if not approved:
        command.completed_at = now()
    return command


async def request_stop(db, session: BrowserSession) -> None:
    session.stop_requested = True
    if session.occupied:
        session.state = "stopping"
    await db.execute(update(BrowserCommand).where(
        BrowserCommand.browser_session_id == session.id,
        BrowserCommand.status.in_(["pending", "queued"]),
        BrowserCommand.operation != "close",
    ).values(status="cancelled", completed_at=now(), error_code="stopped"))


async def revoke_device(db, device: BrowserDevice) -> None:
    device.revoked_at = now()
    device.previous_digest = None
    device.renewal_receipt = None
    for session in (await db.scalars(select(BrowserSession).where(
            BrowserSession.device_id == device.id, BrowserSession.occupied))).all():
        await request_stop(db, session)


async def finish_run(run_id: UUID, user_id: UUID) -> None:
    async with get_session_factory()() as db:
        for grant in (await db.scalars(select(BrowserGrant).where(
                BrowserGrant.chat_run_id == run_id, BrowserGrant.user_id == user_id))).all():
            grant.revoked_at = now()
        for session in (await db.scalars(select(BrowserSession).where(
                BrowserSession.chat_run_id == run_id, BrowserSession.user_id == user_id))).all():
            await request_stop(db, session)
        await db.commit()


async def wait_command(command_id: UUID, user_id: UUID) -> dict:
    deadline = time.monotonic() + settings.browser.approval_timeout + settings.browser.rpc_timeout + 40
    while time.monotonic() < deadline:
        async with get_session_factory()() as db:
            command = await owned(db, BrowserCommand, command_id, user_id)
            if command.status in TERMINAL_COMMANDS:
                # Do not disclose an old result after membership/resource loss.
                session = await owned(db, BrowserSession, command.browser_session_id, user_id)
                grant = await owned(db, BrowserGrant, session.grant_id, user_id)
                await validate_grant(db, grant, executing=True)
                if command.status == "succeeded":
                    return unseal(command.result_ciphertext) if command.result_ciphertext else {}
                if command.effect_state == "none":
                    raise HTTPException(409, f"浏览器操作未执行 ({command.error_code or command.status})；可先 local_browser_observe(mode=wait) 再 sync 重新观察，不要重放原操作")
                raise HTTPException(409, f"浏览器操作 {command.status} ({command.error_code or command.status})；请核对页面，勿自动重试")
            try:
                await execution_session(db, command.browser_session_id, user_id)
            except HTTPException:
                # A close receipt may commit between the command read and the
                # session check. Re-read it, then revalidate access above before
                # returning any result; never replay the operation.
                await db.refresh(command)
                if command.status in TERMINAL_COMMANDS:
                    continue
                raise
        await asyncio.sleep(0.25)
    raise HTTPException(504, "浏览器操作未确认完成，请核对页面，勿自动重试")


async def observe_record(db, session: BrowserSession, result: dict) -> dict:
    context_id, tab_id = result.get("context_id"), result.get("tab_id")
    if not isinstance(context_id, str) or not isinstance(tab_id, int):
        return {"page": {k: v for k, v in result.items() if k not in {"context_id", "tab_id", "session_id"}}, "guidance": "若已就绪请 sync 重新观察；否则等待用户交还控制"}
    # Preserve deltas verbatim: an absent baseline must never be presented as a full page.
    observation = BrowserObservation(tenant_id=session.tenant_id, user_id=session.user_id,
                                     browser_session_id=session.id, connection_epoch=session.connection_epoch,
                                     context_id=context_id, tab_id=tab_id, expires_at=now() + timedelta(minutes=5))
    db.add(observation)
    await db.flush()
    return {"observation_handle": str(observation.id),
            "page": {k: v for k, v in result.items() if k not in {"context_id", "tab_id", "session_id"}}}


async def claim_observation(db, session: BrowserSession, observation_id: UUID, command_id: UUID):
    observation = await owned(db, BrowserObservation, observation_id, session.user_id, lock=True)
    if (observation.browser_session_id != session.id or observation.tenant_id != session.tenant_id
            or observation.connection_epoch != session.connection_epoch or aware(observation.expires_at) <= now()
            or observation.consumed_by not in (None, command_id)):
        raise HTTPException(409, "页面观察已失效，请重新观察并决定下一步")
    observation.consumed_by = command_id
    return observation


async def private_chat_scope(db, chat: Session, user_id: UUID):
    """Protect private history even after a tenant switch or attempted rebind."""
    if not chat.browser_private:
        return
    if chat.user_id != user_id or not chat.agent_id:
        raise missing()
    tenant = await require_agent_tenant(db, user_id, chat.agent_id)
    historical = set((await db.scalars(select(BrowserGrant.tenant_id).where(
        BrowserGrant.chat_session_id == chat.id, BrowserGrant.user_id == user_id,
    ))).all())
    if historical != {tenant}:
        raise HTTPException(403, "浏览器会话的原始租户不匹配")

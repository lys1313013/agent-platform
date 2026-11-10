"""Owner-only control plane. Device tokens cannot access these endpoints."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aio_agent_platform.auth.dependencies import CurrentUser, ExecutionUser
from aio_agent_platform.browser import service
from aio_agent_platform.browser.models import (
    BrowserCommand,
    BrowserDevice,
    BrowserGrant,
    BrowserSession,
)
from aio_agent_platform.browser.schemas import (
    BrowserStateOut,
    DeviceOut,
    GrantOut,
    PairingOut,
    ReleasedOut,
    RevokedOut,
    StoppedOut,
)
from aio_agent_platform.browser.security import enabled, unseal
from aio_agent_platform.browser.service import require_agent_tenant
from aio_agent_platform.db.connection import get_db
from aio_agent_platform.db.models import Agent, Session
from aio_agent_platform.interface.contracts import StatusOut

router = APIRouter(prefix="/api/local-browser", tags=["local-browser"])
DB = Annotated[AsyncSession, Depends(get_db)]


class PairRequest(BaseModel):
    request_id: UUID


class GrantRequest(BaseModel):
    session_id: UUID
    device_id: UUID
    urls: list[str] = Field(default_factory=list, max_length=10)
    persistent: bool = False


class PersistentAuthorizationRequest(BaseModel):
    authorized: bool


class ApprovalRequest(BaseModel):
    approved: bool
    payload_digest: str = Field(min_length=64, max_length=64)


async def chat_scope(db, session_id, user_id):
    chat = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id))
    if not chat or not chat.agent_id or chat.source not in {"chat", "pet"}:
        raise HTTPException(403, "请选择已启用本地浏览器工具的智能体聊天")
    tenant = await require_agent_tenant(db, user_id, chat.agent_id)
    return chat, tenant


@router.get("/devices", response_model=list[DeviceOut])
async def devices(user: CurrentUser, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    enabled()
    rows = (await db.scalars(select(BrowserDevice).where(
        BrowserDevice.user_id == user.id, BrowserDevice.tenant_id == user.tenant_id,
    ).order_by(BrowserDevice.created_at.desc()))).all()
    return [service.device_view(d) for d in rows]


@router.post("/pairings", response_model=PairingOut)
async def pair(body: PairRequest, user: CurrentUser, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.create_pairing(db, user.id, user.tenant_id, body.request_id)
    except IntegrityError:
        raise HTTPException(409, "配对请求正在处理，请使用相同请求标识重试") from None


@router.delete("/devices/{device_id}", response_model=RevokedOut)
async def revoke(device_id: UUID, user: ExecutionUser, db: DB):
    device = await service.owned(db, BrowserDevice, device_id, user.id, lock=True)
    await service.revoke_device(db, device)
    return {"revoked": True}


@router.post("/grants", response_model=GrantOut)
async def grant(body: GrantRequest, user: ExecutionUser, db: DB):
    row = await service.create_grant(db, user.id, body.session_id, body.device_id, body.urls, persistent=body.persistent)
    return {"id": str(row.id), "expires_at": row.expires_at, "origins": row.origins}


@router.put("/devices/{device_id}/persistent-authorization", response_model=DeviceOut)
async def persistent_authorization(device_id: UUID, body: PersistentAuthorizationRequest, user: ExecutionUser, db: DB):
    enabled()
    device = await service.set_persistent_authorization(db, user.id, device_id, body.authorized)
    return service.device_view(device)


@router.delete("/grants/{grant_id}", response_model=RevokedOut)
async def revoke_grant(grant_id: UUID, user: ExecutionUser, db: DB):
    from aio_agent_platform.browser.security import now
    grant = await service.owned(db, BrowserGrant, grant_id, user.id, lock=True)
    grant.revoked_at = now()
    for session in (await db.scalars(select(BrowserSession).where(BrowserSession.grant_id == grant.id))).all():
        await service.request_stop(db, session)
    return {"revoked": True}


@router.get("/chats/{session_id}", response_model=BrowserStateOut)
async def state(session_id: UUID, user: ExecutionUser, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    enabled()
    chat, tenant = await chat_scope(db, session_id, user.id)
    agent = await db.get(Agent, chat.agent_id)
    devices = (await db.scalars(select(BrowserDevice).where(
        BrowserDevice.user_id == user.id, BrowserDevice.tenant_id == tenant, BrowserDevice.revoked_at.is_(None)))).all()
    sessions = (await db.scalars(select(BrowserSession).join(BrowserGrant, BrowserGrant.id == BrowserSession.grant_id).where(
        BrowserGrant.chat_session_id == session_id, BrowserGrant.user_id == user.id, BrowserGrant.tenant_id == tenant,
        BrowserSession.user_id == user.id, BrowserSession.tenant_id == tenant, BrowserSession.occupied))).all()
    pending = (await db.scalars(select(BrowserCommand).where(
        BrowserCommand.browser_session_id.in_([s.id for s in sessions]), BrowserCommand.user_id == user.id,
        BrowserCommand.tenant_id == tenant, BrowserCommand.status == "pending"))).all()
    return {"enabled": "local_browser_open" in (agent.enabled_tools or []),
            "devices": [service.device_view(d) for d in devices],
            "sessions": [{"id": str(s.id), "state": s.state, "cleanup_state": s.cleanup_state,
                          "device_id": str(s.device_id)} for s in sessions],
            "pending": [{"id": str(c.id), "operation": c.operation, "payload": unseal(c.payload_ciphertext),
                         "payload_digest": c.payload_digest, "expires_at": c.expires_at} for c in pending]}


@router.post("/commands/{command_id}/approval", response_model=StatusOut)
async def approve(command_id: UUID, body: ApprovalRequest, user: ExecutionUser, db: DB):
    row = await service.approve_command(db, user.id, command_id, body.approved, body.payload_digest)
    return {"status": row.status}


@router.post("/sessions/{session_id}/stop", response_model=StoppedOut)
async def stop(session_id: UUID, user: ExecutionUser, db: DB):
    session = await service.owned(db, BrowserSession, session_id, user.id, lock=True)
    await service.request_stop(db, session)
    return {"stopped": True}


@router.post("/sessions/{session_id}/confirm-cleanup", response_model=ReleasedOut)
async def confirm_cleanup(session_id: UUID, user: ExecutionUser, db: DB):
    session = await service.owned(db, BrowserSession, session_id, user.id, lock=True)
    device = await service.owned(db, BrowserDevice, session.device_id, user.id, lock=True)
    if not session.stop_requested or (service.online(device) and device.connection_epoch == session.connection_epoch):
        raise HTTPException(409, "请先断开本地扩展，检查并关闭遗留任务窗口，再确认清理")
    session.occupied, session.state, session.cleanup_state = False, "closed", "owner_confirmed"
    return {"released": True}

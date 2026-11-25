"""Session CRUD routes."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.auth.dependencies import CurrentUser, ExecutionUser
from agent_platform.core import task_event_log, task_registry
from agent_platform.core.chat_history import restore_reasoning_order
from agent_platform.core.session_access import owned_session, visible_sessions
from agent_platform.db import Message, Session
from agent_platform.db.connection import get_db, get_session_factory
from agent_platform.db.models import (
    ChatRun,
    ChatRunEvent,
)
from agent_platform.interface.contracts import EventStreamResponse, sse_contract

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _authorize_session(db, session, user):
    from agent_platform.core.session_access import authorize_session
    return await authorize_session(db, session, user.id)


# ---- Schemas ----


class SessionCreate(BaseModel):
    title: str | None = None
    agent_id: UUID | None = None
    workspace_id: UUID | None = None


class SessionUpdate(BaseModel):
    is_pinned: bool | None = None
    is_archived: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    agent_id: UUID | None = None


class SessionOut(BaseModel):
    id: UUID
    title: str | None
    agent_id: UUID | None = None
    workspace_id: UUID | None = None
    is_pinned: bool
    is_archived: bool
    source: str = "chat"
    browser_private: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AttachmentInfo(BaseModel):
    """Image attachment metadata stored alongside a user message."""

    key: str
    url: str
    mime: str
    size: int
    filename: str


class FileAttachmentInfo(BaseModel):
    """File attachment metadata stored alongside a workspace message."""

    file_id: str
    filename: str
    mime: str
    size: int
    workspace_path: str


class FileChangeInfo(BaseModel):
    action: str
    workspace_id: UUID
    path: str
    filename: str
    mime_type: str
    size: int


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str | None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    token_usage: dict | None = None
    attachments: list[AttachmentInfo] | None = None
    file_attachments: list[FileAttachmentInfo] | None = None
    file_changes: list[FileChangeInfo] | None = None
    reasoning: list[dict] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionDetailOut(SessionOut):
    messages: list[MessageOut] = Field(default_factory=list)
    has_more_messages: bool = False


class SessionStatusOut(BaseModel):
    """会话当前处理状态 —— 用于前端进入历史会话时判断是否需要重新连接 SSE。"""

    session_id: str
    is_running: bool
    label: str | None = None
    tool: str | None = None
    source: str | None = None
    started_at: float | None = None


# ---- Routes ----


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    req: SessionCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Session:
    """Create a new conversation session, auto-bound to user's default workspace."""
    from agent_platform.workspaces.service import WorkspaceService

    default_ws = await WorkspaceService.get_or_create_default(db=db, user_id=user.id)
    workspace_id = req.workspace_id or default_ws.id

    session = Session(
        user_id=user.id,
        agent_id=req.agent_id,
        workspace_id=workspace_id,
        title=req.title or "New Chat",
    )
    db.add(session)
    await db.flush()
    return session


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query(max_length=512)] = None,
    source: Annotated[str | None, Query(max_length=32)] = None,
    pinned_first: bool = False,
) -> list[Session]:
    """Page only sessions whose bound agent and tenant remain accessible."""
    query = visible_sessions(user.id, user.tenant_id)
    if agent_id is not None:
        query = query.where(Session.agent_id == agent_id)
    if search:
        query = query.where(Session.title.icontains(search, autoescape=True))
    if source is not None:
        query = query.where(Session.source == source)
    if pinned_first:
        query = query.order_by(Session.is_pinned.desc())
    query = query.order_by(Session.updated_at.desc(), Session.id.desc())
    return list((await db.scalars(query.offset(offset).limit(limit))).all())


@router.get("/{session_id}", response_model=SessionDetailOut)
async def get_session(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    message_limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    before: UUID | None = None,
) -> SessionDetailOut:
    """Read history in chronological order. With a limit, return the latest matching page."""
    from agent_platform.storage.chat_attachments import (
        AttachmentAccessError,
        ChatAttachmentStorage,
        validate_attachment_key,
    )

    session = await owned_session(db, session_id, user.id)
    scope = (Message.session_id == session_id, Message.user_id == user.id)
    query = select(Message).where(*scope)
    if before:
        keys = Message.chronological_order()
        cursor = (await db.execute(select(*keys).where(*scope, Message.id == before))).one_or_none()
        if cursor is None:
            raise HTTPException(422, "无效的消息分页游标")
        query = query.where(tuple_(*keys) < tuple_(*cursor))
    query = query.order_by(*Message.chronological_order(descending=True))
    if message_limit is not None:
        query = query.limit(message_limit + 1)
    rows = list((await db.scalars(query)).all())
    has_more = message_limit is not None and len(rows) > message_limit
    messages = [MessageOut.model_validate(row) for row in reversed(rows[:message_limit])]
    for message in messages:
        if message.attachments:
            visible = []
            for attachment in message.attachments:
                try:
                    validate_attachment_key(attachment.key, user_id=str(user.id), session_id=str(session_id))
                except AttachmentAccessError:
                    continue
                attachment.url = ChatAttachmentStorage.get_public_url(attachment.key)
                visible.append(attachment)
            message.attachments = visible
    detail = SessionDetailOut(**SessionOut.model_validate(session).model_dump(),
                              messages=messages, has_more_messages=has_more)
    # Enrich the response only; do not rewrite historical message content.
    legacy = {message.id: message for message in detail.messages
              if message.reasoning and message.tool_calls
              and any("tool_call_index" not in chunk for chunk in message.reasoning)}
    if legacy:
        event_rows = await db.execute(
            select(ChatRun.assistant_message_id, ChatRunEvent.payload)
            .join(ChatRunEvent, ChatRunEvent.run_id == ChatRun.id)
            .where(ChatRun.session_id == session_id, ChatRun.user_id == user.id,
                   ChatRunEvent.user_id == user.id, ChatRun.assistant_message_id.in_(legacy),
                   ChatRunEvent.payload["type"].as_string().in_(["thinking", "tool_call"]))
            .order_by(ChatRun.id, ChatRunEvent.sequence)
        )
        events_by_message: dict[UUID, list[dict]] = {}
        for message_id, payload in event_rows:
            events_by_message.setdefault(message_id, []).append(payload)
        for message_id, events in events_by_message.items():
            message = legacy[message_id]
            message.reasoning = restore_reasoning_order(message.reasoning, events)
    return detail


@router.get("/{session_id}/status", response_model=SessionStatusOut)
async def get_session_status(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionStatusOut:
    """查询会话当前是否有 AgentLoop / 渠道任务在跑。

    后端不持久化「处理中」状态，只读 Redis 在跑任务注册表。Web 端发送消息时
    没有注册任务，因此 web 会话会返回 is_running=false；从飞书/企微等渠道
    触发的会话会返回 running=true，前端据此显示「重新连接」入口。
    """
    await owned_session(db, session_id, user.id)
    result = await db.execute(
        select(Session.id).where(Session.id == session_id, Session.user_id == user.id, Session.source != "room")
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    task = await task_registry.get_task(user.id, session_id)
    if task is None:
        return SessionStatusOut(session_id=str(session_id), is_running=False)
    return SessionStatusOut(
        session_id=str(session_id),
        is_running=True,
        label=task.label or None,
        tool=task.tool,
        source=task.source or None,
        started_at=task.started_at,
    )


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line (matches chat.py convention)."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{session_id}/events", response_class=EventStreamResponse, openapi_extra=sse_contract())
async def session_events_stream(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """SSE 回放渠道任务在跑期间的完整 Agent 事件（连接即回放 + 实时尾随）。

    与 ``/{session_id}/status`` 配套：status 告诉前端该会话有渠道任务在跑，这里把
    任务跑过的 thinking/tool_call/tool_result/text_delta/text/done 事件按序下发。
    事件由 channel pipeline 实时写入 Redis Stream（``core.task_event_log``）。
    Redis 不可用时流为空，前端收到 EOF 以 ``closed`` 收尾。
    """
    await owned_session(db, session_id, user.id)
    result = await db.execute(
        select(Session.id).where(Session.id == session_id, Session.user_id == user.id, Session.source != "room")
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.commit()

    async def event_generator():
        last_id = "0"
        events = await task_event_log.read_events(user.id, session_id)
        for stream_id, ev in events:
            last_id = stream_id
            yield _sse_event(ev)
        if events and events[-1][1].get("type") in ("done", "error"):
            return
        if not events:
            # 无事件且任务已不在跑（Redis 不可用或任务刚结束）：无事可回放
            if await task_registry.get_task(user.id, session_id) is None:
                return

        # 实时尾随：等待 done/error 事件，超时兜底防止连接泄漏
        deadline = time.monotonic() + task_event_log.TASK_TTL_SECONDS
        while time.monotonic() < deadline:
            tail = await task_event_log.tail_events(user.id, session_id, last_id)
            async with get_session_factory()() as check_db:
                try:
                    await owned_session(check_db, session_id, user.id)
                except HTTPException:
                    yield _sse_event({"type": "access_revoked"})
                    return
            if tail is None:
                yield ": ping\n\n"
                continue
            last_id, ev = tail
            yield _sse_event(ev)
            if ev.get("type") in ("done", "error"):
                return

    return EventStreamResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: UUID,
    req: SessionUpdate,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Session:
    """Update a session (rename title and/or rebind agent)."""
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.user_id == user.id, Session.source != "room").with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await _authorize_session(db, session, user)
    if session.browser_private and req.agent_id is not None and req.agent_id != session.agent_id:
        raise HTTPException(409, "本地浏览器会话不能改绑智能体，请新建会话")
    if req.is_pinned is not None:
        session.is_pinned = req.is_pinned
    if req.is_archived is not None:
        session.is_archived = req.is_archived
    if req.title is not None:
        session.title = req.title
    if req.agent_id is not None:
        from agent_platform.core.execution_scope import (
            ExecutionAccessError,
            require_agent_tenant,
        )
        try:
            target_tenant = await require_agent_tenant(db, user.id, req.agent_id)
        except ExecutionAccessError as error:
            raise HTTPException(403, str(error)) from error
        original_tenant = await _authorize_session(db, session, user)
        if target_tenant != original_tenant:
            raise HTTPException(409, "跨租户切换智能体请新建会话")
        active = await db.scalar(select(ChatRun.id).where(
            ChatRun.session_id == session.id, ChatRun.status == "running",
        ))
        if active:
            raise HTTPException(409, "请先停止当前任务再切换智能体")
        session.agent_id = req.agent_id
    await db.flush()
    await db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a session and all its messages."""
    from agent_platform.core.session_access import remove_session_history

    await remove_session_history(db, session_id, user.id, delete_session=True)

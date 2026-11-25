"""One object authorization boundary for chat transports and task subscribers."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from agent_platform.core.execution_scope import (
    ExecutionAccessError,
    require_agent_tenant,
    require_execution_user,
)
from agent_platform.db.models import Agent, BrowserGrant, Session, Tenant, TenantMembership, User


def visible_sessions(user_id: UUID, tenant_id: UUID, *, command_scope: bool = False, allow_private: bool = True):
    """Filter before pagination and before publishing session names to a command transport."""
    grant = select(BrowserGrant.id).where(
        BrowserGrant.chat_session_id == Session.id, BrowserGrant.user_id == user_id,
    ).correlate(Agent, Session)
    private_scope = and_(
        Agent.tenant_id == tenant_id,
        grant.where(BrowserGrant.tenant_id == Agent.tenant_id).exists(),
        ~grant.where(BrowserGrant.tenant_id != Agent.tenant_id).exists(),
    )
    visible_agent = (
        select(Agent.id)
        .join(Tenant, Tenant.id == Agent.tenant_id)
        .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .where(
            Agent.id == Session.agent_id, Agent.is_active, Tenant.is_active,
            TenantMembership.user_id == user_id,
            or_(Agent.visibility == "tenant", Agent.created_by == user_id),
            or_(Session.browser_private.is_(False), private_scope),
        )
    )
    if command_scope:
        visible_agent = visible_agent.where(Agent.tenant_id == tenant_id)
    query = select(Session).where(
        Session.user_id == user_id, Session.source != "room",
        or_(and_(Session.agent_id.is_(None), Session.browser_private.is_(False)), visible_agent.exists()),
    )
    if not allow_private:
        query = query.where(Session.browser_private.is_(False))
    return query


async def authorize_session(db, session: Session, user_id: UUID, *, tenant_id: UUID | None = None) -> UUID:
    if session.user_id != user_id or session.source == "room":
        raise HTTPException(404, "Session not found")
    try:
        if session.agent_id:
            actual = await require_agent_tenant(db, user_id, session.agent_id)
            if tenant_id is not None and actual != tenant_id:
                raise ExecutionAccessError("会话与执行租户不一致")
        else:
            # Only unbound interactive sessions use the selected tenant. Runs
            # pass their immutable tenant explicitly, including after UI changes.
            actual = tenant_id or await db.scalar(select(User.tenant_id).where(User.id == user_id))
            if actual is None:
                raise ExecutionAccessError("无法确定会话租户")
            await require_execution_user(db, user_id, actual)
    except ExecutionAccessError as error:
        raise HTTPException(403, str(error)) from error
    if session.browser_private:
        from agent_platform.browser.service import private_chat_scope
        await private_chat_scope(db, session, user_id)
    return actual


async def owned_session(db, session_id: UUID, user_id: UUID, *, lock: bool = False,
                        tenant_id: UUID | None = None) -> Session:
    query = select(Session).where(Session.id == session_id, Session.user_id == user_id, Session.source != "room")
    if lock:
        query = query.with_for_update()
    session = await db.scalar(query.execution_options(populate_existing=True))
    if session is None:
        raise HTTPException(404, "Session not found")
    await authorize_session(db, session, user_id, tenant_id=tenant_id)
    return session


async def remove_session_history(db, session_id: UUID, user_id: UUID, *, delete_session: bool,
                                 allow_browser_private: bool = True, tenant_id: UUID | None = None) -> None:
    """Serialize history changes with run creation and remove replay data in the same transaction."""
    from redis.exceptions import RedisError
    from sqlalchemy import delete, update

    from agent_platform.core import chat_runs, task_event_log, task_registry
    from agent_platform.db.models import ChannelSessionMapping, ChatRun, ChatRunEvent, Message

    await chat_runs.user_scope(db, user_id)
    session = await owned_session(db, session_id, user_id, lock=True, tenant_id=tenant_id)
    if session.browser_private and not allow_browser_private:
        raise HTTPException(403, "本地浏览器私密会话仅支持 Web 聊天")
    runs = (await db.scalars(select(ChatRun).where(
        ChatRun.session_id == session_id, ChatRun.user_id == user_id).with_for_update())).all()
    for run in runs:
        await chat_runs.expire_run(db, run)
        if run.status == "running":
            raise HTTPException(409, "请先停止后台任务，再清理会话")
    # A channel can also be switched onto a session created by the web client.
    channel_history = session.source not in {"chat", "api", "pet", "cron"} or await db.scalar(
        select(ChannelSessionMapping.id).where(ChannelSessionMapping.session_id == session_id).limit(1))
    if channel_history:
        if await task_registry.get_task(user_id, session_id):
            raise HTTPException(409, "请先停止渠道任务，再清理会话")
        try:
            await task_event_log.clear_events(user_id, session_id)
        except RedisError as error:
            raise HTTPException(503, "暂时无法清理渠道回放记录，请重试") from error
        if delete_session:
            await db.execute(update(ChannelSessionMapping).where(
                ChannelSessionMapping.session_id == session_id, ChannelSessionMapping.is_active,
            ).values(is_active=False))
    if runs:
        await db.execute(delete(ChatRunEvent).where(
            ChatRunEvent.run_id.in_([run.id for run in runs]), ChatRunEvent.user_id == user_id))
        await db.execute(delete(ChatRun).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id))
    await db.execute(delete(Message).where(Message.session_id == session_id, Message.user_id == user_id))
    if delete_session:
        await db.execute(delete(Session).where(Session.id == session_id, Session.user_id == user_id))
    else:
        session.context_summary = None

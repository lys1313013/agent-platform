"""One object authorization boundary for chat transports and task subscribers."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from agent_platform.core.execution_scope import (
    ExecutionAccessError,
    require_agent_tenant,
    require_execution_user,
)
from agent_platform.db.models import Session, User


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

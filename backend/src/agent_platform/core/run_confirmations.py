"""Cross-worker confirmation mailbox for the active durable Web turn."""

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.core.confirmation import PendingConfirmation, confirmation_manager
from agent_platform.db.connection import get_session_factory
from agent_platform.db.models import ChatRun

_MAILBOX = "_pending_confirmations"
current_web_run: ContextVar[UUID | None] = ContextVar("confirmation_web_run", default=None)


def record_event(run: ChatRun, event: dict) -> None:
    kind, key = event.get("type"), event.get("confirmation_id")
    if not key or kind not in {"confirmation_required", "confirmation_resolved"}:
        return
    mailbox = dict(run.request.get(_MAILBOX, {}))
    if kind == "confirmation_required":
        mailbox[key] = event
    else:
        mailbox.pop(key, None)
    request = dict(run.request)
    if mailbox:
        request[_MAILBOX] = mailbox
    else:
        request.pop(_MAILBOX, None)
    run.request = request


def clear(run: ChatRun) -> None:
    if _MAILBOX in run.request:
        run.request = {k: v for k, v in run.request.items() if k != _MAILBOX}


def _pending(card: dict) -> bool:
    created = datetime.fromisoformat(card["created_at"])
    timeout = int(card["context"]["timeout_seconds"])
    return "response" not in card and datetime.now(UTC) < created + timedelta(seconds=timeout)


def pending(run: ChatRun) -> list[dict]:
    if run.status != "running" or run.stop_requested:
        return []
    return [card for card in run.request.get(_MAILBOX, {}).values() if _pending(card)]


async def wait_for_reply(card: PendingConfirmation) -> PendingConfirmation | None:
    """Honor an answer committed before expiry even if the heartbeat has yet to see it."""
    deadline = card.created_at + timedelta(seconds=card.timeout_seconds)
    try:
        async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
            return await confirmation_manager.wait_for_response(card.id)
    except TimeoutError:
        run_id = current_web_run.get()
        if run_id is not None:
            from agent_platform.core.chat_runs import owned

            async with get_session_factory()() as db:
                # Serialize with an in-flight submission at the deadline.
                run = await owned(db, UUID(card.user_id), run_id, lock=True)
                saved = run.request.get(_MAILBOX, {}).get(card.id, {})
                if run.status == "running" and not run.stop_requested and "response" in saved:
                    card.response = saved["response"]
                    return card
        raise


async def submit(db: AsyncSession, user_id: UUID, key: str, response: dict) -> bool:
    from agent_platform.core.chat_runs import authorize_run, expire_run

    run = await db.scalar(select(ChatRun).where(
        ChatRun.user_id == user_id, ChatRun.status == "running",
        ChatRun.request[_MAILBOX][key]["confirmation_id"].as_string() == key,
    ).with_for_update().execution_options(populate_existing=True))
    if run is None:
        return False
    await authorize_run(db, run)
    await expire_run(db, run)
    mailbox = dict(run.request.get(_MAILBOX, {}))
    card = mailbox.get(key)
    if run.status != "running" or run.stop_requested or not card or not _pending(card):
        raise HTTPException(409, "该确认已回答或已失效")
    mailbox[key] = {**card, "response": response}
    run.request = {**run.request, _MAILBOX: mailbox}
    await db.commit()
    deliver(run)
    return True


def deliver(run: ChatRun) -> None:
    """The producing worker wakes its local waiters from committed replies."""
    for key, card in run.request.get(_MAILBOX, {}).items():
        reply = card.get("response")
        local = confirmation_manager.get(key)
        if reply is not None and local is not None and local.response is None:
            confirmation_manager.resolve_confirmation(key, reply)

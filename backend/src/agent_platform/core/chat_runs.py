"""Durable Web tasks with leased workers and independent, cursor-based subscribers.

Adjacent streaming deltas are coalesced into one event row; the producer keeps
the live snapshot in memory and persists it at tool/terminal boundaries (with a
short checkpoint), so JSONB writes no longer scale with delta count. Subscribers
read committed rows and wake on a Redis notification instead of polling and
locking the run row.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import delete, select, text

from agent_platform.core.chat_history import _merge_file_changes
from agent_platform.core.chat_run_notify import notifier
from agent_platform.core.session_access import authorize_session
from agent_platform.core.task_scope import call_key, child_tasks, completed_calls
from agent_platform.db.connection import current_user_id, get_session_factory
from agent_platform.db.models import ChatRun, ChatRunEvent, Message, Session
from agent_platform.db.sanitize import sanitize_pg_text

logger = structlog.get_logger()
OWNER = str(uuid4())
LEASE_SECONDS = 30
_workers: dict[UUID, asyncio.Task] = {}
_worker_users: dict[UUID, UUID] = {}

# The producer owns the live snapshot; JSONB is only rewritten at tool/terminal
# boundaries or every checkpoint interval. A crash loses at most one interval
# of streamed text, and ChatRunEvent rows remain the authoritative event log.
_live_snapshots: dict[UUID, dict] = {}
_snapshot_saved_at: dict[UUID, float] = {}
_SNAPSHOT_CHECKPOINT_SECONDS = 2.0

_DELTA_TYPES = {"thinking", "text_delta", "delegation_thinking", "delegation_text_delta"}


def now() -> datetime:
    return datetime.now(UTC)


async def user_scope(db, user_id: UUID) -> None:
    # Every new transaction needs its own RLS context, including background jobs.
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)})


def _apply_event(state: dict, event: dict) -> None:
    kind = event.get("type")
    if kind == "thinking":
        chunks = state.setdefault("reasoning", [])
        tool_count = len(state.get("tool_calls", []))
        if not chunks or chunks[-1].get("tool_call_index") != tool_count:
            chunks.append({"id": f"thinking-{len(chunks)}", "content": "", "tool_call_index": tool_count})
        chunks[-1]["content"] += event.get("content", "")
    elif kind == "text_delta":
        state["content"] = state.get("content", "") + event.get("content", "")
    elif kind == "text":
        state["content"] = event.get("content", "")
    elif kind == "tool_call":
        state.setdefault("tool_calls", []).append({k: event[k] for k in ("id", "name", "arguments")})
    elif kind == "tool_result":
        for call in state.get("tool_calls", []):
            if call["id"] == event.get("tool_call_id"):
                call["result"] = {"status": event.get("status"), "preview": event.get("preview", "")}
    elif kind == "confirmation_required":
        for call in state.get("tool_calls", []):
            if ((call["id"] == event.get("tool_call_id") if event.get("tool_call_id")
                 else call["name"] == "AskUserQuestion") and not call.get("confirmation")):
                call["confirmation"] = dict(event)
                break
    elif kind == "confirmation_resolved":
        for call in state.get("tool_calls", []):
            confirmation = call.get("confirmation") or {}
            if confirmation.get("confirmation_id") == event.get("confirmation_id"):
                confirmation["resolved"] = dict(event)
    elif kind == "delegation_start":
        for call in state.get("tool_calls", []):
            if call["id"] == event.get("tool_call_id"):
                call["delegation"] = {
                    **event, "status": "running", "thinking": "", "toolCalls": [], "result": "",
                }
    elif str(kind).startswith("delegation_"):
        for call in state.get("tool_calls", []):
            delegation = call.get("delegation")
            if not delegation or delegation.get("delegation_id") != event.get("delegation_id"):
                continue
            if kind == "delegation_thinking":
                delegation["thinking"] += event.get("content", "")
            elif kind == "delegation_text_delta":
                delegation["result"] += event.get("content", "")
            elif kind == "delegation_tool_call":
                delegation["toolCalls"].append({k: event.get(k) for k in ("id", "name", "arguments")})
            elif kind == "delegation_tool_result":
                for child in delegation["toolCalls"]:
                    if child["id"] == event.get("tool_call_id"):
                        child["result"] = {"status": event.get("status"), "preview": event.get("preview", "")}
            elif kind == "delegation_end":
                delegation.update(status=event.get("status", "completed"),
                                  duration_ms=event.get("duration_ms", 0), error=event.get("error"))
                delegation["result"] = delegation.get("result") or event.get("result_preview", "")
    elif kind == "file_changes":
        state["file_changes"] = _merge_file_changes(state.get("file_changes", []), event.get("file_changes", []))
    elif kind == "done":
        state["completed"] = True
        for key in ("content", "tool_calls", "reasoning", "file_changes"):
            incoming = event.get(key) or ([] if key != "content" else "")
            if key == "reasoning" and isinstance(incoming, list) and incoming and any("tool_call_index" not in c for c in incoming):
                recorded = state.get("reasoning", [])
                if "".join(c["content"] for c in recorded) == "".join(c["content"] for c in incoming):
                    incoming = recorded
            state[key] = incoming
    if kind not in {"ui_action_heartbeat", "delegation_heartbeat"}:
        state["last_type"] = kind


def snapshot_event(snapshot: dict, event: dict) -> dict:
    state = copy.deepcopy(snapshot)
    _apply_event(state, event)
    return state


def _coalesce(events: list[dict]) -> list[dict]:
    """Merge adjacent streaming deltas so one DB row carries a whole batch."""
    merged: list[dict] = []
    for event in events:
        kind = event.get("type")
        if (merged and kind in _DELTA_TYPES and merged[-1].get("type") == kind
                and merged[-1].get("delegation_id") == event.get("delegation_id")):
            previous = dict(merged[-1])
            previous["content"] = (previous.get("content") or "") + (event.get("content") or "")
            merged[-1] = previous
        else:
            merged.append(event)
    return merged


def uncertain_tools(run: ChatRun) -> list[str]:
    # Even failed external calls can have side effects. Resume requires all calls
    # to have a durable successful result; do not infer safety from tool names.
    return [call.get("name", "unknown") for call in run.snapshot.get("tool_calls", [])
            if not call.get("result") or call["result"].get("status") != "ok"]


def describe(run: ChatRun) -> dict:
    uncertain = uncertain_tools(run)
    return {"id": str(run.id), "session_id": str(run.session_id), "status": run.status,
            "assistant_message_id": str(run.assistant_message_id),
            "last_sequence": run.last_sequence, "stop_requested": run.stop_requested,
            "can_resume": run.status in {"interrupted", "failed", "stopped"} and not uncertain and not run.request.get("browser_private"),
            "uncertain_tools": uncertain}


async def save_snapshot(db, run: ChatRun) -> None:
    message = await db.scalar(select(Message).where(Message.id == run.assistant_message_id, Message.user_id == run.user_id))
    if message:
        snapshot = copy.deepcopy(run.snapshot)
        if run.status != "completed":
            for call in snapshot.get("tool_calls", []):
                if not call.get("result"):
                    call["result"] = {"status": "unknown", "preview": "任务中断前未保存工具结果，请核对实际执行结果，不要直接重试。"}
                delegation = call.get("delegation")
                if delegation and delegation.get("status") == "running":
                    delegation["status"] = "failed"
                    delegation["error"] = "任务已中断，执行结果待核对"
                confirmation = call.get("confirmation")
                if confirmation and not confirmation.get("resolved"):
                    confirmation["resolved"] = {"status": "cancelled"}
        for key in ("content", "tool_calls", "reasoning", "file_changes"):
            setattr(message, key, sanitize_pg_text(snapshot.get(key) or ("" if key == "content" else None)))


async def expire_run(db, run: ChatRun) -> None:
    heartbeat = run.heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    if run.status == "running" and heartbeat < now() - timedelta(seconds=LEASE_SECONDS):
        run.status = "completed" if run.snapshot.get("completed") else "interrupted"
        await save_snapshot(db, run)
        await db.flush()


async def latest(db, user_id: UUID, session_id: UUID) -> ChatRun | None:
    await user_scope(db, user_id)
    session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id, Session.source != "room"))
    if not session:
        raise HTTPException(404, "Session not found")
    run = await db.scalar(select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id)
                          .order_by(ChatRun.created_at.desc()).limit(1).with_for_update())
    if run:
        await authorize_run(db, run)
        await expire_run(db, run)
    else:
        await authorize_session(db, session, user_id)
    await db.commit()
    return run


async def _load_run(db, user_id: UUID, run_id: UUID, *, lock: bool = False) -> ChatRun:
    """Identity-only load for producer cleanup; never expose it from a route."""
    await user_scope(db, user_id)
    query = select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id)
    if lock:
        query = query.with_for_update()
    run = await db.scalar(query)
    if not run:
        raise HTTPException(404, "Task not found")
    return run


async def authorize_run(db, run: ChatRun) -> None:
    session = await db.get(Session, run.session_id, populate_existing=True)
    if session is None:
        raise HTTPException(404, "Session not found")
    source_agent = run.request.get("_execution_agent_id")
    if "_execution_agent_id" in run.request and (str(session.agent_id) if session.agent_id else None) != source_agent:
        raise HTTPException(403, "任务来源智能体已改变")
    tenant = run.request.get("_execution_tenant_id")
    if tenant is None and session.agent_id is None:
        raise HTTPException(403, "历史任务缺少可信租户归属，无法访问或续跑")
    await authorize_session(db, session, run.user_id, tenant_id=UUID(tenant) if tenant else None)


async def owned(db, user_id: UUID, run_id: UUID, *, lock: bool = False) -> ChatRun:
    run = await _load_run(db, user_id, run_id, lock=lock)
    await authorize_run(db, run)
    return run


async def reserve(db, user_id: UUID, session_id: UUID, request: dict, resumed_from: UUID | None = None, *, tenant_id: UUID | None = None) -> ChatRun:
    await user_scope(db, user_id)
    session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id).with_for_update())
    if not session:
        raise HTTPException(404, "Session not found")
    tenant = await authorize_session(db, session, user_id, tenant_id=tenant_id)
    request = {**request, "_execution_tenant_id": str(tenant),
               "_execution_agent_id": str(session.agent_id) if session.agent_id else None}
    active = await db.scalar(select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id,
                                                 ChatRun.status == "running").with_for_update())
    if active:
        await expire_run(db, active)
        if active.status == "running":
            raise HTTPException(409, "该会话已有后台任务，请连接现有任务或先停止任务")
    if resumed_from:
        prior = await owned(db, user_id, resumed_from, lock=True)
        await expire_run(db, prior)
        if prior.session_id != session_id or not describe(prior)["can_resume"]:
            raise HTTPException(409, "任务不可安全续跑：请先核对没有成功结果的工具操作")
        # Only the latest turn may be resumed; concurrent/double clicks cannot
        # fork the same continuation, even after a very fast worker finishes.
        last = await db.scalar(select(ChatRun.id).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id)
                               .order_by(ChatRun.created_at.desc()).limit(1))
        if last != prior.id:
            raise HTTPException(409, "该任务之后已有新一轮对话，请刷新会话")
        cached = dict(prior.request.get("completed_calls", {}))
        for call in prior.snapshot.get("tool_calls", []):
            if call.get("result", {}).get("status") == "ok":
                cached[call_key(call["name"], call.get("arguments", {}))] = call["result"]
        request = {**request, "completed_calls": cached}
    run = ChatRun(id=uuid4(), session_id=session_id, user_id=user_id, assistant_message_id=uuid4(),
                  owner=OWNER, status="running", request=request, snapshot={}, last_sequence=0,
                  heartbeat_at=now(), created_at=now(), resumed_from=resumed_from, stop_requested=False)
    # Keep event rows for the new run and the immediately previous one (live
    # replay/disconnect); older run histories are no longer reachable.
    stale_runs = list((await db.scalars(
        select(ChatRun.id)
        .where(ChatRun.session_id == session_id, ChatRun.user_id == user_id)
        .order_by(ChatRun.created_at.desc())
        .offset(1)
    )).all())
    if stale_runs:
        await db.execute(delete(ChatRunEvent).where(
            ChatRunEvent.user_id == user_id,
            ChatRunEvent.run_id.in_(stale_runs),
        ))
    db.add(run)
    await db.flush()
    return run


async def append(
    run_id: UUID, user_id: UUID, events: list[dict], *, force_snapshot: bool = False,
) -> None:
    events = _coalesce(events)
    if not events:
        return
    async with get_session_factory()() as db:
        run = await owned(db, user_id, run_id, lock=True)
        if run.status != "running" or run.owner != OWNER:
            raise asyncio.CancelledError()
        state = _live_snapshots.get(run_id)
        if state is None:
            state = copy.deepcopy(run.snapshot or {})
        boundary = False
        for event in events:
            run.last_sequence += 1
            payload = sanitize_pg_text(event)
            _apply_event(state, payload)
            db.add(ChatRunEvent(
                run_id=run_id, user_id=user_id, sequence=run.last_sequence, payload=payload,
            ))
            if payload.get("type") not in _DELTA_TYPES:
                boundary = True
        _live_snapshots[run_id] = state
        saved_at = time.monotonic()
        if (force_snapshot or boundary
                or saved_at - _snapshot_saved_at.get(run_id, 0.0) >= _SNAPSHOT_CHECKPOINT_SECONDS):
            run.snapshot = copy.deepcopy(state)
            _snapshot_saved_at[run_id] = saved_at
        await db.commit()
        sequence = run.last_sequence
    await notifier.publish(run_id, sequence, "running")


async def finish(run_id: UUID, user_id: UUID, status: str) -> None:
    changed = False
    async with get_session_factory()() as db:
        # Revocation must stop new work but still permit terminal cleanup.
        run = await _load_run(db, user_id, run_id, lock=True)
        if run.status == "running" and run.owner == OWNER:
            state = _live_snapshots.get(run_id)
            if state is not None:
                run.snapshot = copy.deepcopy(state)
            run.status = status
            await save_snapshot(db, run)
            await db.commit()
            changed = True
        sequence, final_status = run.last_sequence, run.status
    _live_snapshots.pop(run_id, None)
    _snapshot_saved_at.pop(run_id, None)
    if changed:
        await notifier.publish(run_id, sequence, final_status)


async def heartbeat(run_id: UUID, user_id: UUID, worker: asyncio.Task) -> None:
    try:
        while True:
            await asyncio.sleep(1)
            async with get_session_factory()() as db:
                run = await owned(db, user_id, run_id, lock=True)
                if run.status != "running" or run.owner != OWNER or run.stop_requested:
                    if not worker.cancelling():
                        worker.cancel()
                    return
                run.heartbeat_at = now()
                await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("chat_run_heartbeat_failed", run_id=str(run_id))
        if not worker.cancelling():
            worker.cancel()  # Fail closed if we cannot prove ownership.


async def produce(run: ChatRun, source: AsyncIterator[str]) -> None:
    from agent_platform.browser.context import (
        BrowserRunContext,
        browser_run,
        private_browser_history,
    )
    from agent_platform.browser.service import finish_run
    private_token = private_browser_history.set(bool(run.request.get("browser_private")))
    grant_id = run.request.get("browser_grant_id")
    browser_token = browser_run.set(BrowserRunContext(run.id, UUID(grant_id), run.user_id) if grant_id else None)
    token = current_user_id.set(str(run.user_id))
    children: set[asyncio.Task] = set()
    child_token = child_tasks.set(children)
    completed_token = completed_calls.set(run.request.get("completed_calls", {}))
    beat = asyncio.create_task(heartbeat(run.id, run.user_id, asyncio.current_task()))
    pending: list[dict] = []
    last_flush = time.monotonic()
    outcome = "interrupted"
    try:
        async for chunk in source:
            if not chunk.startswith("data: "):
                continue
            event = json.loads(chunk[6:].strip())
            if event.get("type") in {"ui_action_heartbeat", "delegation_heartbeat"}:
                continue
            pending.append(event)
            if (event.get("type") not in {"thinking", "text_delta"}
                    or time.monotonic() - last_flush >= 0.1 or len(pending) >= 64):
                await append(run.id, run.user_id, pending)
                pending.clear()
                last_flush = time.monotonic()
            if event.get("type") == "done":
                # Commit terminal state before clients can enqueue the next turn.
                outcome = "completed"
            elif event.get("type") == "error":
                outcome = "failed"
        if pending:
            await append(run.id, run.user_id, pending, force_snapshot=True)
            pending.clear()
    except asyncio.CancelledError:
        pass
    except Exception:
        outcome = "failed"
        logger.exception("chat_run_failed", run_id=str(run.id))
    finally:
        for child in list(children):
            child.cancel()
        if children:
            await asyncio.gather(*list(children), return_exceptions=True)
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
        with suppress(Exception):
            await source.aclose()
        try:
            if pending:
                with suppress(HTTPException):
                    await append(run.id, run.user_id, pending, force_snapshot=True)
            async with get_session_factory()() as db:
                current = await _load_run(db, run.user_id, run.id)
                if current.stop_requested and outcome != "completed":
                    outcome = "stopped"
            await finish(run.id, run.user_id, outcome)
        except (Exception, asyncio.CancelledError):
            logger.exception("chat_run_finalize_failed", run_id=str(run.id))
        try:
            if grant_id:
                await finish_run(run.id, run.user_id)
        except (Exception, asyncio.CancelledError):
            logger.error("browser_run_cleanup_pending", run_id=str(run.id))
        finally:
            browser_run.reset(browser_token)
            private_browser_history.reset(private_token)
        child_tasks.reset(child_token)
        completed_calls.reset(completed_token)
        current_user_id.reset(token)


def start(run: ChatRun, source: AsyncIterator[str]) -> None:
    task = asyncio.create_task(produce(run, source), name=f"web-chat-{run.id}")
    _workers[run.id] = task
    _worker_users[run.id] = run.user_id

    def remove_worker(_):
        _workers.pop(run.id, None)
        _worker_users.pop(run.id, None)

    task.add_done_callback(remove_worker)


async def subscribe(run_id: UUID, user_id: UUID, after: int = 0, *, replay: bool = True) -> AsyncIterator[str]:
    try:
        async for event in _subscribe(run_id, user_id, after, replay=replay):
            yield event
    except HTTPException as error:
        if error.status_code not in {403, 404}:
            raise
        yield sse({"type": "access_revoked", "message": "任务访问权限已失效"})


async def _subscribe(run_id: UUID, user_id: UUID, after: int = 0, *, replay: bool = True) -> AsyncIterator[str]:
    # Read-only subscriber. Closing this generator does not touch the worker.
    async with get_session_factory()() as db:
        run = await owned(db, user_id, run_id)
        replay_until = run.last_sequence if replay else 0
        info = describe(run)
    yield sse({"type": "run", **info})
    cursor = after
    last_expire_check = 0.0
    while True:
        # Capture the wake-up version before reading, so a notification that
        # lands between the read and the wait is not lost.
        seen = notifier.version(run_id)
        async with get_session_factory()() as db:
            run = await owned(db, user_id, run_id)
            monotonic = time.monotonic()
            if monotonic - last_expire_check >= 5.0:
                # A stalled worker is only detected here; do not take a row
                # lock on the read path.
                await expire_run(db, run)
                last_expire_check = monotonic
            rows = list((await db.scalars(select(ChatRunEvent).where(
                ChatRunEvent.run_id == run_id, ChatRunEvent.user_id == user_id,
                ChatRunEvent.sequence > cursor).order_by(ChatRunEvent.sequence).limit(256))).all())
            info = describe(run)
            await db.commit()
        for row in rows:
            cursor = row.sequence
            # done is withheld until producer finalization so queue flushing
            # cannot race the active-session uniqueness constraint.
            if row.payload.get("type") == "done" and info["status"] == "running":
                cursor -= 1
                break
            yield sse({**row.payload, "run_id": str(run_id), "sequence": cursor,
                       "replay": cursor <= replay_until})
        if info["status"] != "running" and cursor >= info["last_sequence"]:
            yield sse({"type": "run_status", **info})
            return
        if not rows:
            yield ": ping\n\n"
        # Wake on the producer's publish; the timeout is the Redis-down and
        # cross-process fallback (and also refreshes the run's status).
        await notifier.wait(run_id, seen, 1.0)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stop(db, user_id: UUID, run_id: UUID) -> dict:
    run = await owned(db, user_id, run_id, lock=True)
    await expire_run(db, run)
    if run.status == "running":
        run.stop_requested = True
    await db.commit()
    task = _workers.get(run_id)
    if task:
        if not task.cancelling():
            task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 10)
        if task.cancelled():
            # Cancellation before the coroutine's first instruction cannot run
            # its finally block; the explicit stopper must persist the outcome.
            await finish(run_id, user_id, "stopped")
    # Other worker processes observe stop_requested through their heartbeat.
    for _ in range(50):
        async with get_session_factory()() as fresh:
            run = await owned(fresh, user_id, run_id)
            if run.status != "running":
                return describe(run)
        await asyncio.sleep(0.2)
    raise HTTPException(409, "任务仍在停止，请稍后重试；尚未开始新任务")


async def shutdown() -> None:
    jobs = [(run_id, _worker_users[run_id], task) for run_id, task in _workers.items()]
    for _, _, task in jobs:
        if not task.cancelling():
            task.cancel()
    if jobs:
        await asyncio.gather(*(task for _, _, task in jobs), return_exceptions=True)
    for run_id, user_id, task in jobs:
        if task.cancelled():
            await finish(run_id, user_id, "interrupted")
    _live_snapshots.clear()
    _snapshot_saved_at.clear()
    await notifier.shutdown()

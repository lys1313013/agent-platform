"""UI action routes — REST API for frontend (in-browser) tool execution results.

See docs/22-浏览器页面自动化/02-技术方案.md. Mirrors routes/confirmations.py.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.auth.dependencies import ExecutionUser
from agent_platform.core.confirmation import confirmation_manager
from agent_platform.core.session_access import owned_session
from agent_platform.core.ui_action import ui_action_manager
from agent_platform.db.connection import get_db

logger = structlog.get_logger()

router = APIRouter(prefix="/api/ui-actions", tags=["ui-actions"])

# ---- Schemas ----


class UiActionRespondRequest(BaseModel):
    """Request body for reporting a frontend action's execution result."""

    status: str  # "ok" | "error" | "cancelled"
    result: dict[str, Any] | None = None
    error: str | None = None
    warning: str | None = None
    # Session context refresh (the only uplink channel mid-run)
    snapshot_version: int | None = None
    dangerous_refs: list[str] | None = None
    delta_snapshot: str | None = None
    page_context: dict[str, Any] | None = None
    # ui_screenshot: base64 image payload (never persisted, never forwarded in events)
    image: str | None = None


class UiActionRespondResponse(BaseModel):
    success: bool
    message: str = ""


class PendingUiActionResponse(BaseModel):
    id: str
    tool_call_id: str
    action: str
    args: dict[str, Any]
    risk: str
    created_at: str


# ---- Routes ----


@router.post("/{action_id}/respond", response_model=UiActionRespondResponse)
async def respond_to_ui_action(
    action_id: str,
    body: UiActionRespondRequest,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UiActionRespondResponse:
    """Submit the execution result of a pending frontend UI action.

    Only the user the action was issued to may respond.
    """
    pending = ui_action_manager.get(action_id)
    if not pending or pending.user_id != str(user.id):
        logger.info(
            "ui_action_respond_rejected",
            action_id=action_id,
            found=bool(pending),
        )
        raise HTTPException(
            status_code=404,
            detail="UI action not found or already resolved",
        )

    await owned_session(db, UUID(pending.session_id), user.id, tenant_id=pending.execution_tenant_id)
    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    logger.info(
        "ui_action_respond",
        action_id=action_id,
        action=pending.action,
        status=payload.get("status"),
        error=payload.get("error"),
    )
    resolved = ui_action_manager.resolve(action_id, payload)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail="UI action not found or already resolved",
        )
    return UiActionRespondResponse(success=True, message="UI action result submitted")


@router.get("/sessions/{session_id}/pending")
async def get_pending_ui_actions(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PendingUiActionResponse]:
    """List pending UI actions for a session.

    Race-condition fallback only: an SSE disconnect cancels the agent run and
    its finally-block pops pendings, so this normally returns empty.
    """
    actions = ui_action_manager.get_pending(str(session_id))
    await _authorize_pending(db, session_id, user.id, actions)

    return [
        PendingUiActionResponse(
            id=p.id,
            tool_call_id=p.tool_call_id,
            action=p.action,
            args=p.args,
            risk=p.risk,
            created_at=p.created_at.isoformat(),
        )
        for p in actions
    ]


async def _authorize_pending(db, session_id, user_id, pending) -> None:
    for tenant_id in {item.execution_tenant_id for item in pending} or {None}:
        await owned_session(db, session_id, user_id, tenant_id=tenant_id)


@router.post("/sessions/{session_id}/cancel_all")
async def cancel_all_ui_actions(
    session_id: UUID,
    user: ExecutionUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    """Cancel only the pending actions and confirmations authorized for this request."""
    actions = ui_action_manager.get_pending(str(session_id))
    confirmations = confirmation_manager.get_pending(str(session_id))
    await _authorize_pending(db, session_id, user.id, [*actions, *confirmations])
    cancelled = sum(ui_action_manager.resolve(action.id, {"status": "cancelled", "error": "cancelled"})
                    for action in actions)
    confirmations_cancelled = sum(confirmation_manager.resolve_confirmation(
        item.id, {"status": "rejected", "user_input": "session cancelled"}
    ) for item in confirmations)
    return {"cancelled": cancelled, "confirmations_cancelled": confirmations_cancelled}

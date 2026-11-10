"""Read-only audit log API for platform superadmins."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aio_agent_platform.auth.dependencies import SuperAdminUser
from aio_agent_platform.db.connection import get_db
from aio_agent_platform.db.models import AuditLog

router = APIRouter(prefix="/api/admin/audit-logs", tags=["audit"])


class AuditLogItem(BaseModel):
    id: int
    user_id: UUID | None
    tenant_id: UUID | None
    action: str
    resource: str | None
    details: dict | None
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogItem]
    total: int


@router.get("", response_model=AuditLogPage)
async def list_audit_logs(
    _admin: SuperAdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    action: str | None = None,
    user_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> AuditLogPage:
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if user_id:
        conds.append(AuditLog.user_id == user_id)
    if tenant_id:
        conds.append(AuditLog.tenant_id == tenant_id)

    total = int(await db.scalar(
        select(func.count(AuditLog.id)).where(*conds)
    ) or 0)
    rows = list((await db.scalars(
        select(AuditLog)
        .where(*conds)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all())
    return AuditLogPage(
        items=[
            AuditLogItem(
                id=row.id,
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                action=row.action,
                resource=row.resource,
                details=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
    )

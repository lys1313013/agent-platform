"""FastAPI auth dependencies."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.auth.jwt_handler import TokenExpiredError, TokenPayload, decode_token
from agent_platform.db import User
from agent_platform.db.connection import get_db, set_db_user

security = HTTPBearer()


async def get_execution_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
    expected_user_id: Annotated[UUID | None, Header(alias="X-Expected-User-ID")] = None,
) -> User:
    """Validate JWT and active user, then bind the request's database identity."""
    user = await authenticate_access_token(credentials.credentials, db)
    if expected_user_id is not None and user.id != expected_user_id:
        raise HTTPException(status_code=409, detail="登录账号已切换，请刷新页面后重试")
    return user


async def authenticate_access_token(raw: str, db: AsyncSession) -> User:
    try:
        payload: TokenPayload = decode_token(raw)
    except TokenExpiredError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    if payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    # Identity authentication is independent of the mutable UI tenant.
    try:
        user_id = UUID(payload.sub)
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e
    user = await db.scalar(select(User).where(
        User.id == user_id, User.is_active, User.credential_version == payload.ver,
    ))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    await set_db_user(db, user.id)
    return user


def require_expected_tenant(user: User, expected_tenant_id: UUID | None) -> None:
    """Reject stale UI submissions before a management handler touches resources."""
    if expected_tenant_id is not None and user.tenant_id != expected_tenant_id:
        raise HTTPException(status_code=409, detail="当前租户已切换，请刷新页面后重试")


async def get_current_user(
    user: Annotated[User, Depends(get_execution_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    expected_tenant_id: Annotated[UUID | None, Header(alias="X-Expected-Tenant-ID")] = None,
) -> User:
    """Management/new-object entry points additionally require the selected tenant."""
    from agent_platform.core.execution_scope import ExecutionAccessError, require_execution_user

    selected_tenant = user.tenant_id
    require_expected_tenant(user, expected_tenant_id)
    try:
        user = await require_execution_user(db, user.id, selected_tenant)
    except ExecutionAccessError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    # The membership query refreshes User. A concurrent tenant switch must
    # not return a different tenant from the one we just authorized.
    require_expected_tenant(user, selected_tenant)
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require the current user to be an admin."""
    if user.role not in {"admin", "superadmin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_superadmin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require the current user to be a platform super administrator."""
    if user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super administrator access required",
        )
    return user


# Type aliases for dependency injection
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
SuperAdminUser = Annotated[User, Depends(require_superadmin)]
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Object execution callers must validate membership against the object itself.
ExecutionUser = Annotated[User, Depends(get_execution_user)]

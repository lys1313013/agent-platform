"""Auth routes: register, login, refresh, logout."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.auth.jwt_handler import (
    TokenPair,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from agent_platform.auth.password import (
    NewPassword,
    Password,
    async_hash_password,
    async_verify_password,
)
from agent_platform.core.config import settings
from agent_platform.db import (
    RefreshToken,
    Tenant,
    TenantMembership,
    User,
    UserConfig,
    UserProfile,
)
from agent_platform.db.connection import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr
    password: NewPassword
    tenant_name: str | None = Field(default=None, min_length=1, max_length=128)


class LoginRequest(BaseModel):
    username_or_email: str
    password: Password


class RefreshRequest(BaseModel):
    refresh_token: str


def _hash_token(token: str) -> str:
    """Hash a refresh token with SHA-256 for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def _issue_tokens(db: AsyncSession, user: User, record: RefreshToken | None = None) -> TokenPair:
    """Keep one current refresh hash per login, including across rotations."""
    family_id = record.family_id if record is not None else uuid4()
    refresh = create_refresh_token(user.id, family_id=family_id, credential_version=user.credential_version)
    if record is None:
        record = RefreshToken(user_id=user.id, family_id=family_id)
        db.add(record)
    record.token_hash = _hash_token(refresh)
    record.expires_at = datetime.now(UTC) + timedelta(days=settings.jwt.refresh_token_expire_days)
    return TokenPair(access_token=create_access_token(user.id, user.role, user.credential_version),
                     refresh_token=refresh)


async def _ensure_login_tenant(db: AsyncSession, user: User) -> None:
    if not user.is_active:
        raise HTTPException(403, "Account is disabled")
    # An active membership is required. Recover a stale UI selection so the
    # existing profile and landing-page requests can succeed immediately.
    tenant_id = await db.scalar(
        select(Tenant.id).join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .where(TenantMembership.user_id == user.id, Tenant.is_active)
        .order_by((Tenant.id == user.tenant_id).desc(), Tenant.created_at, Tenant.id).limit(1)
    )
    if tenant_id is None:
        raise HTTPException(403, "Account has no active tenant")
    if tenant_id != user.tenant_id:
        user.tenant_id = tenant_id


# ---- Register ----


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenPair:
    # Check duplicate
    existing = await db.execute(
        select(User).where((User.username == req.username) | (User.email == req.email))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered",
        )

    # Determine if this is the first user (make admin). Serialize on PostgreSQL
    # so two concurrent registrations cannot both observe an empty table.
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(721347001)"))
    total_users = await db.scalar(select(func.count(User.id))) or 0
    is_first_user = total_users == 0

    # Public registration creates an isolated tenant. Joining an existing tenant
    # must go through an administrator-controlled membership flow.
    tenant = Tenant(
        name=req.tenant_name or f"{req.username} 的租户",
        slug=f"{req.username.lower()[:40]}-{uuid4().hex[:12]}",
    )
    db.add(tenant)
    await db.flush()

    # Create user
    user = User(
        username=req.username,
        email=req.email,
        password_hash=await async_hash_password(req.password),
        role="superadmin" if is_first_user else "user",
        tenant_id=tenant.id,
    )
    db.add(user)
    await db.flush()
    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id))

    # Create profile + config
    db.add(UserProfile(user_id=user.id, tenant_id=tenant.id))
    db.add(UserConfig(user_id=user.id))
    await db.flush()

    return _issue_tokens(db, user)


# ---- Login ----


@router.post("/login", response_model=TokenPair)
async def login(
    req: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenPair:
    from agent_platform.core.throttle import clear_counter, exceeded

    identity = req.username_or_email.strip().lower()
    client = request.client.host if request.client else "unknown"
    user_key = f"aio:login:user:{client}:{identity}"
    ip_key = f"aio:login:ip:{client}"
    if await exceeded(ip_key, 100, 300) or await exceeded(user_key, 20, 300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过于频繁，请稍后再试",
        )

    result = await db.execute(
        select(User).where(
            (User.username == req.username_or_email) | (User.email == req.username_or_email)
        )
    )
    user = result.scalar_one_or_none()

    if not user or not await async_verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Serialize issuance with a password reset. Verify the previously checked
    # password still belongs to this credential generation after taking the lock.
    generation = user.credential_version
    await db.refresh(user, with_for_update={"of": User})
    if generation != user.credential_version:
        raise HTTPException(401, "Credentials changed")
    await _ensure_login_tenant(db, user)
    await clear_counter(user_key)
    await clear_counter(ip_key)
    return _issue_tokens(db, user)


# ---- Refresh ----


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    req: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenPair:
    try:
        payload = decode_token(req.refresh_token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        ) from e

    if payload.type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a refresh token",
        )

    token_hash = _hash_token(req.refresh_token)
    family_id = payload.family_id or UUID(token_hash[:32])
    user = await db.scalar(select(User).where(User.id == UUID(payload.sub))
                           .with_for_update().execution_options(populate_existing=True))
    if user is None or payload.ver != user.credential_version:
        raise HTTPException(401, "Refresh token revoked or expired")
    record = await db.scalar(select(RefreshToken).where(
        RefreshToken.user_id == user.id, RefreshToken.family_id == family_id,
        RefreshToken.expires_at > datetime.now(UTC),
    ).with_for_update().execution_options(populate_existing=True))
    if record is None:
        raise HTTPException(401, "Refresh token revoked or expired")
    if record.token_hash != token_hash:
        # A validly signed earlier token from this login was presented again.
        await db.delete(record)
        await db.commit()
        raise HTTPException(401, "Refresh token already rotated")
    await _ensure_login_tenant(db, user)
    return _issue_tokens(db, user, record)


# ---- Logout ----


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    req: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Revoke a refresh token."""
    try:
        payload = decode_token(req.refresh_token)
    except Exception:
        return  # Silently succeed on invalid token

    if payload.type != "refresh":
        return
    family_id = payload.family_id or UUID(_hash_token(req.refresh_token)[:32])
    await db.execute(delete(RefreshToken).where(
        RefreshToken.user_id == UUID(payload.sub), RefreshToken.family_id == family_id,
    ))

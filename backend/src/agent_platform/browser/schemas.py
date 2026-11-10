"""Owner control-plane response contracts (never expose device secrets)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DeviceOut(BaseModel):
    id: UUID
    tenant_id: UUID
    label: str
    online: bool
    revoked: bool
    expires_at: datetime
    extension_version: str | None
    protocol_version: str | None
    persistent_authorized: bool = False


class PairingOut(BaseModel):
    id: UUID
    url: str
    expires_at: datetime


class GrantOut(BaseModel):
    id: UUID
    expires_at: datetime | None
    origins: list[str]


class RevokedOut(BaseModel):
    revoked: bool


class StoppedOut(BaseModel):
    stopped: bool


class ReleasedOut(BaseModel):
    released: bool


class BrowserSessionOut(BaseModel):
    id: UUID
    state: str
    cleanup_state: str
    device_id: UUID


class PendingCommandOut(BaseModel):
    id: UUID
    operation: str
    payload: dict
    payload_digest: str
    expires_at: datetime


class BrowserStateOut(BaseModel):
    enabled: bool
    devices: list[DeviceOut]
    sessions: list[BrowserSessionOut]
    pending: list[PendingCommandOut]

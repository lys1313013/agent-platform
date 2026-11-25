"""A form opened in one tab must keep its account and tenant on submission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_platform.auth import dependencies
from agent_platform.core import execution_scope
from agent_platform.db.connection import get_db


async def test_stale_tab_cannot_write_under_new_account_or_selected_tenant(monkeypatch):
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    monkeypatch.setattr(dependencies, "authenticate_access_token", AsyncMock(return_value=user))
    monkeypatch.setattr(execution_scope, "require_execution_user", AsyncMock(return_value=user))
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: None
    writes = []

    @app.post("/draft")
    async def submit(identity: dependencies.CurrentUser):
        writes.append(identity.tenant_id)
        return {}

    @app.post("/bound-object")
    async def run_bound_object(identity: dependencies.ExecutionUser):
        return {"user": str(identity.id)}

    auth = {"Authorization": "Bearer test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        old_account = {**auth, "X-Expected-User-ID": str(uuid4())}
        old_tenant = {**auth, "X-Expected-Tenant-ID": str(uuid4())}
        for headers in (old_account, old_tenant):
            assert (await client.post("/draft", headers=headers)).status_code == 409
        assert not writes
        # Object authorization uses its own tenant, independently of the selected tenant.
        assert (await client.post("/bound-object", headers=old_tenant)).status_code == 200
        assert (await client.post("/bound-object", headers=old_account)).status_code == 409
        current = {
            **auth,
            "X-Expected-Tenant-ID": str(user.tenant_id),
            "X-Expected-User-ID": str(user.id),
        }
        for headers in (current, auth):
            assert (await client.post("/draft", headers=headers)).status_code == 200
        assert writes == [user.tenant_id, user.tenant_id]

        async def switch_during_lookup(db, user_id, tenant_id):
            assert tenant_id == user.tenant_id
            user.tenant_id = uuid4()
            return user

        monkeypatch.setattr(execution_scope, "require_execution_user", switch_during_lookup)
        assert (await client.post("/draft", headers=auth)).status_code == 409
        assert len(writes) == 2

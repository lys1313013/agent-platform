from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_platform.interface.routes import observability as obs


def test_observation_limits_and_exact_bucket_boundaries():
    first = datetime(2026, 1, 1, 12, 23, 45, tzinfo=UTC)
    step = timedelta(minutes=1)
    assert len(obs._fill_series(first, first + timedelta(days=1), step, {})) == 1441
    with pytest.raises(HTTPException) as points:
        obs._fill_series(first, first + timedelta(days=1, minutes=1), step, {})
    assert points.value.status_code == 422
    with pytest.raises(HTTPException) as span:
        obs._resolve_range("7d", first, first + timedelta(days=367))
    assert span.value.status_code == 422


@pytest.mark.postgres
async def test_overview_combines_cards_and_buckets_and_scopes_tenant(db_session):
    from uuid import uuid4

    from agent_platform.db.models import LLMCallLog, ToolCallLog
    first = datetime(2026, 1, 1, tzinfo=UTC)
    tenant = uuid4()
    user = SimpleNamespace(role="admin", tenant_id=tenant)
    for tid, status in ((tenant, "success"), (tenant, "failed"), (uuid4(), "success")):
        db_session.add(LLMCallLog(tenant_id=tid, model="test", provider="test", final_status=status,
                                  ttft_ms=9, duration_ms=10, prompt_tokens=20, total_tokens=30,
                                  completion_tokens=10, cache_read_tokens=5, created_at=first))
        db_session.add(ToolCallLog(tenant_id=tid, tool_name="test", is_error=status == "failed", created_at=first))
    await db_session.flush()
    out = await obs.get_overview(user, db_session, "24h", first, first + timedelta(hours=1))
    c = out.cards
    assert c.llm_requests == c.tool_requests == 2
    assert c.llm_error_rate == c.tool_error_rate == 50
    assert c.total_tokens == 60 and c.cache_hit_rate == 25 and c.p95_latency_ms == 10
    assert [p["value"] for p in out.series["tokens"]] == [60, 0]
    assert [p["value"] for p in out.series["tool_error_rate"]] == [50, 0]
    empty = await obs.get_overview(user, db_session, "24h", first - timedelta(days=2), first - timedelta(days=1))
    assert empty.cards.llm_requests == 0 and empty.cards.p95_latency_ms is None

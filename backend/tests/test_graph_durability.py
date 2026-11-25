"""Database invariants for document visibility, graph budgets and durable extraction."""
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_platform.core.execution_scope import execution_tenant_id
from agent_platform.db.models import (
    GraphChunk,
    GraphDocument,
    GraphEntity,
    GraphExtractionChunk,
    GraphExtractionJob,
    GraphKnowledgeBase,
    GraphRelationship,
    Tenant,
    TenantMembership,
    User,
)
from agent_platform.graph_knowledge import extraction, routes, service
from agent_platform.graph_knowledge.retrieval import MAX_EDGES, expand_subgraph

pytestmark = pytest.mark.postgres


async def test_document_chunks_follow_visible_parent(db_session):
    db = db_session
    actor = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    visible = GraphKnowledgeBase(name="readable", tenant_id=actor.tenant_id, created_by=actor.id)
    others = [GraphKnowledgeBase(name="private", tenant_id=actor.tenant_id, created_by=uuid4(), visibility="private"),
              GraphKnowledgeBase(name="other", tenant_id=uuid4(), created_by=uuid4())]
    db.add_all([visible, *others])
    await db.flush()
    for kb in (visible, *others):
        doc = GraphDocument(knowledge_base_id=kb.id, title="source", content="", created_by=kb.created_by)
        db.add(doc)
        await db.flush()
        if kb == visible:
            assert await routes.list_chunks(kb.id, doc.id, actor, db) == []
        db.add(GraphChunk(knowledge_base_id=kb.id, document_id=doc.id, seq=0, content=kb.name))
        await db.flush()
        if kb == visible:
            assert (await routes.list_chunks(kb.id, doc.id, actor, db))[0]["content"] == "readable"
        else:
            for parent in (kb.id, visible.id):
                with pytest.raises(HTTPException) as denied:
                    await routes.list_chunks(parent, doc.id, actor, db)
                assert denied.value.status_code == 404


async def test_breadth_first_graph_budget_and_cycle(db_session):
    kb, author = uuid4(), uuid4()
    root, child = uuid4(), uuid4()

    def edge(source, target):
        return GraphRelationship(knowledge_base_id=kb, source_entity_id=source, target_entity_id=target,
                                 created_by=author, status="approved", relation_type="knows")
    db_session.add_all([edge(root, child), edge(child, root)])
    await db_session.flush()
    _, edges = await expand_subgraph(db_session, kb, [root], 5)
    assert [e["depth"] for e in edges] == [1, 2]
    db_session.add_all([edge(child, uuid4()) for _ in range(MAX_EDGES * 2)])
    await db_session.flush()
    _, edges = await expand_subgraph(db_session, kb, [root], 5)
    assert len(edges) == len({e["id"] for e in edges}) == MAX_EDGES
    assert [e["depth"] for e in edges] == [1] + [2] * (MAX_EDGES - 1)


async def test_restart_fences_old_worker_and_retries_only_failed_chunks(db_session, engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = Tenant(id=uuid4(), name="workspace", slug="workspace")
    actor = User(id=uuid4(), tenant_id=tenant.id, username="owner", email="owner@example.test",
                 password_hash="test", role="admin")
    kb = GraphKnowledgeBase(id=uuid4(), name="graph", tenant_id=tenant.id, created_by=actor.id)
    db_session.add_all([tenant, actor, TenantMembership(tenant_id=tenant.id, user_id=actor.id), kb])
    for i in range(1, 4):
        db_session.add(GraphChunk(id=UUID(int=i), knowledge_base_id=kb.id,
                                  document_id=uuid4(), seq=i, content=str(i)))
    await db_session.commit()
    async with factory() as db:
        job, _ = await service.create_extraction_job(db, kb, actor)
    # Changing the management UI tenant during a queued run leaves its source pinned.
    other = Tenant(id=uuid4(), name="other", slug="other")
    db_session.add_all([other, TenantMembership(tenant_id=other.id, user_id=actor.id)])
    actor.tenant_id = other.id
    await db_session.commit()
    claim = await extraction.claim_job(factory)
    assert await extraction.claim_job(factory) is None
    calls = Counter()
    paused = asyncio.Event()
    resume = asyncio.Event()

    async def model_call(provider, message_type, content):
        assert execution_tenant_id(actor.id) == tenant.id
        calls[content] += 1
        if content == "2" and calls[content] == 1:
            paused.set()
            await resume.wait()
            return {"entities": [{"name": "stale result"}]}
        if content == "3" and calls[content] == 1:
            raise ValueError("transient model failure")
        return {"entities": [{"name": content}, {"name": "shared"}],
                "relationships": [{"source": content, "target": "shared", "relation_type": "uses"}]}

    monkeypatch.setattr(extraction, "_get_default_provider", AsyncMock(return_value=(object(), object())))
    monkeypatch.setattr(extraction, "_extract_from_chunk", model_call)
    old = asyncio.create_task(extraction._process_claim(factory, claim))
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        async with factory.begin() as db:
            await db.execute(update(GraphExtractionJob).where(GraphExtractionJob.id == job.id).values(
                lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        replacement = await extraction.claim_job(factory)
        assert replacement[0] == job.id and replacement[2] != claim[2]
        await extraction._process_claim(factory, replacement)
    finally:
        resume.set()
        await old
    actor.tenant_id = tenant.id
    await db_session.commit()
    async with factory() as db:
        failed = await db.get(GraphExtractionJob, job.id)
        assert failed.status == "failed" and failed.processed_chunks == 3
        retry, started = await service.create_extraction_job(db, kb, actor, retry_job_id=job.id)
        assert started and retry.id == job.id
        assert (await service.create_extraction_job(db, kb, actor, retry_job_id=job.id))[0].id == job.id
    await extraction._process_claim(factory, await extraction.claim_job(factory))
    async with factory() as db:
        finished = await db.get(GraphExtractionJob, job.id)
        assert (finished.status, finished.processed_chunks, finished.total_chunks) == ("done", 3, 3)
        assert finished.entities_found == 4 and finished.relationships_found == 3
        assert await db.scalar(select(func.count()).select_from(GraphEntity)) == 4
        assert await db.scalar(select(func.count()).select_from(GraphRelationship)) == 3
        assert await db.scalar(select(func.count()).select_from(GraphExtractionChunk).where(
            GraphExtractionChunk.status == "done")) == 3
    assert calls == {"1": 1, "2": 2, "3": 2}

"""LLM entity/relationship extraction pipeline for graph knowledge bases.

Documents are split into chunks; each chunk is sent to the LLM which returns a
JSON payload of entities + relationships. Results are deduplicated by normalized
entity name (within a knowledge base) and by (source, target, relation_type).

Workers claim durable jobs in short transactions and checkpoint graph writes together with each chunk.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, insert, literal, or_, select, tuple_, update

from agent_platform.core.execution_scope import (
    ExecutionAccessError,
    bind_execution_scope,
    require_execution_user,
)
from agent_platform.db.connection import get_session_factory, set_db_user
from agent_platform.db.models import (
    GraphChunk,
    GraphEntity,
    GraphExtractionChunk,
    GraphExtractionJob,
    GraphKnowledgeBase,
    GraphRelationship,
)

logger = structlog.get_logger(__name__)

# A model call times out before its claim can be taken over.
_MODEL_TIMEOUT = 120
_LEASE = timedelta(seconds=150)


_EXTRACTION_PROMPT = """你是知识图谱构建助手。从给定的文档片段中抽取实体与关系，严格输出 JSON，不要输出其他文字。

输出格式：
{
  "entities": [
    {"name": "实体名称", "type": "实体类型(人物/组织/产品/概念/事件/地点/其他)", "description": "一句话描述"}
  ],
  "relationships": [
    {"source": "来源实体名称", "target": "目标实体名称", "relation_type": "关系类型(如 负责/属于/开发/依赖/位于)", "description": "关系描述", "confidence": 0.9}
  ]
}

规则：
1. 只抽取片段中明确提到的信息，不要编造。
2. relationships 中的 source/target 必须是 entities 中列出的实体名称。
3. confidence 为 0-1 的浮点数，表示对该关系的把握程度。
4. 每个实体名称唯一；不同片段中的同一实体请保持名称一致。

文档片段：
"""


def normalize_name(name: str) -> str:
    """Normalize an entity name for dedup: collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", name or "").strip().lower()


async def _get_default_provider(tenant_id: UUID):
    """Load the tenant's default LLM provider. Returns None if unavailable."""
    try:
        from sqlalchemy.orm import selectinload

        from agent_platform.db.models import LLMModel
        from agent_platform.llm import LLMMessage, create_provider

        factory = get_session_factory()
        async with factory() as db:
            result = await db.execute(
                select(LLMModel)
                .options(selectinload(LLMModel.provider))
                .where(LLMModel.is_default, LLMModel.is_active, LLMModel.tenant_id == tenant_id)
                .limit(1)
            )
            model = result.scalar_one_or_none()
        if not model or not model.provider or not model.provider.is_active or model.provider.tenant_id != tenant_id:
            return None, None
        provider = create_provider(
            provider=model.provider.provider_type,
            model=model.model_name,
            base_url=model.provider.base_url,
            api_key=model.provider.api_key_encrypted,
        )
        return provider, LLMMessage
    except Exception:
        logger.exception("graph_extraction_default_provider_failed")
        return None, None


def _parse_llm_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("LLM 输出不是合法 JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM 输出不是对象")
    return data


async def _extract_from_chunk(provider, llm_message_cls, chunk_text: str) -> dict:
    # Reasoning models (e.g. deepseek) burn several thousand tokens on thinking
    # before emitting any content — a small max_tokens leaves content empty.
    response = await provider.complete(
        messages=[llm_message_cls(role="user", content=_EXTRACTION_PROMPT + chunk_text)],
        max_tokens=16000,
    )
    return _parse_llm_json(response.content or "")


async def _upsert_extraction_result(
    db,
    kb_id: UUID,
    data: dict,
    chunk_id: UUID,
    created_by: UUID,
) -> tuple[int, int]:
    """Resolve only the names and edges referenced by this chunk."""
    entities = data.get("entities") or []
    relations = data.get("relationships") or []
    names = {normalize_name(str(item.get("name") or "")) for item in entities}
    for rel in relations:
        names.update(normalize_name(str(rel.get(key) or "")) for key in ("source", "target"))
    entity_ids = dict((await db.execute(
        select(GraphEntity.name_norm, GraphEntity.id).where(
            GraphEntity.knowledge_base_id == kb_id, GraphEntity.name_norm.in_(names)
        )
    )).all())
    new_entities = 0
    for item in entities:
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 128:
            continue
        norm = normalize_name(name)
        if norm in entity_ids:
            continue
        entity = GraphEntity(
            knowledge_base_id=kb_id,
            name=name,
            name_norm=norm,
            type=str(item.get("type") or "其他")[:64],
            description=str(item.get("description") or "").strip() or None,
            source_chunk_id=chunk_id,
            created_by=created_by,
        )
        db.add(entity)
        await db.flush()
        entity_ids[norm] = entity.id
        new_entities += 1

    relation_keys = {
        (entity_ids.get(normalize_name(str(r.get("source") or ""))),
         entity_ids.get(normalize_name(str(r.get("target") or ""))),
         str(r.get("relation_type") or "").strip()[:64])
        for r in relations
    }
    cols = (GraphRelationship.source_entity_id, GraphRelationship.target_entity_id, GraphRelationship.relation_type)
    existing_rel_keys = set((await db.execute(
        select(*cols).where(GraphRelationship.knowledge_base_id == kb_id, tuple_(*cols).in_(relation_keys))
    )).all()) if relation_keys else set()
    new_rels = 0
    for rel in relations:
        source = str(rel.get("source") or "").strip()
        target = str(rel.get("target") or "").strip()
        rel_type = str(rel.get("relation_type") or "").strip()[:64]
        if not source or not target or not rel_type:
            continue
        source_id = entity_ids.get(normalize_name(source))
        target_id = entity_ids.get(normalize_name(target))
        if not source_id or not target_id or source_id == target_id:
            continue
        key = (source_id, target_id, rel_type)
        if key in existing_rel_keys:
            continue
        existing_rel_keys.add(key)
        try:
            confidence = max(0.0, min(1.0, float(rel.get("confidence") or 0.8)))
        except (TypeError, ValueError):
            confidence = 0.8
        db.add(
            GraphRelationship(
                knowledge_base_id=kb_id,
                source_entity_id=source_id,
                target_entity_id=target_id,
                relation_type=rel_type,
                description=str(rel.get("description") or "").strip() or None,
                confidence=confidence,
                source_chunk_id=chunk_id,
                created_by=created_by,
            )
        )
        new_rels += 1

    return new_entities, new_rels


async def claim_job(factory) -> tuple[UUID, UUID, UUID] | None:
    """Claim a pending job, or fence out a worker whose lease has expired."""
    async with factory.begin() as db:
        job = await db.scalar(
            select(GraphExtractionJob)
            .where(GraphExtractionJob.status.in_(("pending", "running")),
                   or_(GraphExtractionJob.lease_until.is_(None), GraphExtractionJob.lease_until <= func.now()))
            .order_by(GraphExtractionJob.created_at, GraphExtractionJob.id)
            .limit(1).with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        token = uuid4()
        job.status = "running"
        job.lease_token = token
        job.lease_until = func.now() + _LEASE
        has_manifest = await db.scalar(select(GraphExtractionChunk.chunk_id).where(
            GraphExtractionChunk.job_id == job.id).limit(1))
        if has_manifest is None:
            # Historical interrupted jobs lack per-chunk checkpoints. Re-read
            # their manifests once; writes resolve existing entities and edges.
            await db.execute(insert(GraphExtractionChunk).from_select(
                ["job_id", "chunk_id"],
                select(literal(job.id), GraphChunk.id).where(GraphChunk.knowledge_base_id == job.knowledge_base_id),
            ))
            job.total_chunks = await db.scalar(select(func.count()).select_from(GraphExtractionChunk)
                                              .where(GraphExtractionChunk.job_id == job.id))
            job.processed_chunks = 0
        return job.id, job.knowledge_base_id, token


async def _owned_job(db, claim):
    job_id, kb_id, token = claim
    # Serialize mutations with job admission/deletion using the same lock order.
    kb = await db.scalar(select(GraphKnowledgeBase).where(GraphKnowledgeBase.id == kb_id).with_for_update())
    job = await db.scalar(select(GraphExtractionJob).where(
        GraphExtractionJob.id == job_id, GraphExtractionJob.status == "running",
        GraphExtractionJob.lease_token == token).with_for_update())
    if job is None:
        return None, None
    if kb is None or (kb.visibility != "tenant" and kb.created_by != job.created_by):
        raise ExecutionAccessError("知识库不存在或已不可见")
    await set_db_user(db, job.created_by)
    user = await require_execution_user(db, job.created_by, kb.tenant_id)
    if user.role not in {"admin", "superadmin"}:
        raise ExecutionAccessError("图谱抽取需要管理员权限")
    return job, kb


def _finish(job, error=None):
    job.status = "failed" if error else "done"
    job.error = error
    job.finished_at = datetime.now(UTC)
    job.lease_token = None
    job.lease_until = None


async def _process_claim(factory, claim):
    job_id, _, _ = claim
    provider = None
    try:
        while True:
            async with factory.begin() as db:
                job, kb = await _owned_job(db, claim)
                if job is None:
                    return
                user_id, tenant_id = job.created_by, kb.tenant_id
                part = await db.scalar(select(GraphExtractionChunk).where(
                    GraphExtractionChunk.job_id == job_id,
                    GraphExtractionChunk.status == "pending",
                ).order_by(GraphExtractionChunk.chunk_id).limit(1))
                if part is None:
                    failed = await db.scalar(select(func.count()).select_from(GraphExtractionChunk).where(
                        GraphExtractionChunk.job_id == job_id, GraphExtractionChunk.status == "failed"))
                    _finish(job, f"{failed} 个分块抽取失败，可以重试" if failed else None)
                    return
                chunk_id = part.chunk_id
                content = await db.scalar(select(GraphChunk.content).where(
                    GraphChunk.id == chunk_id, GraphChunk.knowledge_base_id == kb.id))
                if content is None:
                    part.status = "skipped"
                    job.processed_chunks += 1
                    continue
                job.lease_until = func.now() + _LEASE

            with bind_execution_scope(user_id, tenant_id):
                if provider is None:
                    provider, llm_message_cls = await _get_default_provider(tenant_id)
                    if provider is None:
                        raise ValueError("没有可用的默认模型，请先在模型管理中配置")
                try:
                    async with asyncio.timeout(_MODEL_TIMEOUT):
                        data = await _extract_from_chunk(provider, llm_message_cls, content)
                except Exception:
                    logger.exception("graph_extraction_chunk_failed", job_id=str(job_id), chunk_id=str(chunk_id))
                    data = None

            async with factory.begin() as db:
                job, kb = await _owned_job(db, claim)
                if job is None:
                    return
                part = await db.get(GraphExtractionChunk, (job_id, chunk_id))
                exists = await db.scalar(select(GraphChunk.id).where(
                    GraphChunk.id == chunk_id, GraphChunk.knowledge_base_id == kb.id).with_for_update())
                if exists is None:
                    part.status = "skipped"
                elif data is None:
                    part.status = "failed"
                else:
                    try:
                        async with db.begin_nested():
                            new_e, new_r = await _upsert_extraction_result(
                                db, kb.id, data, chunk_id, job.created_by)
                    except Exception:
                        logger.exception("graph_chunk_write_failed", job_id=str(job_id), chunk_id=str(chunk_id))
                        part.status = "failed"
                    else:
                        job.entities_found += new_e
                        job.relationships_found += new_r
                        part.status = "done"
                job.processed_chunks += 1
    finally:
        client = getattr(provider, "client", None)
        if client is not None:
            await client.close()


async def _worker(factory):
    while True:
        claim = None
        try:
            claim = await claim_job(factory)
            if claim:
                await _process_claim(factory, claim)
                continue
        except asyncio.CancelledError:
            if claim:
                async with factory.begin() as db:
                    await db.execute(update(GraphExtractionJob).where(
                        GraphExtractionJob.id == claim[0], GraphExtractionJob.lease_token == claim[2]
                    ).values(lease_token=None, lease_until=None))
            raise
        except Exception as error:
            logger.exception("graph_extraction_job_failed")
            if claim:
                try:
                    async with factory.begin() as db:
                        job = await db.scalar(select(GraphExtractionJob).where(
                            GraphExtractionJob.id == claim[0], GraphExtractionJob.lease_token == claim[2],
                        ).with_for_update())
                        if job:
                            _finish(job, str(error) if isinstance(error, (ValueError, ExecutionAccessError)) else "抽取过程发生错误，可以重试")
                except Exception:
                    logger.exception("graph_extraction_claim_cleanup_failed")
        await asyncio.sleep(2)


def start_workers(factory):
    tasks = [asyncio.create_task(_worker(factory)) for _ in range(2)]

    async def shutdown():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return shutdown

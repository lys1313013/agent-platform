"""Exercise pg_trgm threshold configuration against PostgreSQL."""

import pytest
from sqlalchemy import func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.core.text_search import trigram_match


@pytest.mark.postgres
async def test_trigram_threshold_is_bound_and_transaction_local(engine):
    async with engine.connect() as connection, AsyncSession(bind=connection) as db:
        # Load pg_trgm in this connection before reading its default setting.
        await db.scalar(select(func.similarity("tenant", "tenant")))
        setting = text("SHOW pg_trgm.similarity_threshold")
        original = await db.scalar(setting)
        await db.rollback()

        match = await trigram_match(db, literal("tenant"), "tenant", 0.9)
        assert await db.scalar(select(match)) is True
        assert float(await db.scalar(setting)) == 0.9

        await db.rollback()
        assert await db.scalar(setting) == original

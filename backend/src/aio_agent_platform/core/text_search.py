"""pg_trgm query helpers that let PostgreSQL use a GIN index."""

from __future__ import annotations

from sqlalchemy import func, text


async def trigram_match(db, column, query: str, threshold: float):
    """Return a WHERE expression; ``%`` uses the trigram index, similarity() does not."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SET LOCAL pg_trgm.similarity_threshold = :threshold"),
            {"threshold": float(threshold)},
        )
        return column.op("%")(query)
    return func.similarity(column, query) > threshold

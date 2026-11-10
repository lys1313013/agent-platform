"""Trigram GIN indexes for similarity search.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_memories_search_vec_trgm", "memories", "search_vec"),
    ("ix_daily_memories_search_vec_trgm", "daily_memories", "search_vec"),
    ("ix_skills_search_vec_trgm", "skills", "search_vec"),
    ("ix_graph_entities_name_norm_trgm", "graph_entities", "name_norm"),
)


def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    for name, table, column in _INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" '
            f"USING gin ({column} gin_trgm_ops)"
        )


def downgrade():
    for name, _table, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

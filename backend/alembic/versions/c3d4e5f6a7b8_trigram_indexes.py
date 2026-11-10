"""Trigram GIN indexes for similarity search.

Revision ID: c3d4e5f6a7b8
Revises: 8c57d2e6a940
"""
import sqlalchemy as sa

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "8c57d2e6a940"
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
    connection = op.get_bind()
    extension_schema = connection.execute(sa.text(
        "SELECT n.nspname FROM pg_catalog.pg_extension e "
        "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'pg_trgm'"
    )).scalar_one()
    # The extension may already live outside the application's search_path.
    extension_schema = connection.dialect.identifier_preparer.quote_schema(extension_schema)
    for name, table, column in _INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" '
            f"USING gin ({column} {extension_schema}.gin_trgm_ops)"
        )


def downgrade():
    for name, _table, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

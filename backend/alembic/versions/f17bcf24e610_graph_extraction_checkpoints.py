"""Persist graph extraction claims and per-chunk checkpoints."""
import sqlalchemy as sa

from alembic import op

revision = "f17bcf24e610"
down_revision = "e615cda314bf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_graph_rels_traversal", "graph_relationships",
                    ["knowledge_base_id", "status", "source_entity_id", "id"], if_not_exists=True)
    op.add_column("graph_extraction_jobs", sa.Column("lease_token", sa.Uuid()), if_not_exists=True)
    op.add_column("graph_extraction_jobs", sa.Column("lease_until", sa.DateTime(timezone=True)), if_not_exists=True)
    op.create_index("idx_graph_jobs_claim", "graph_extraction_jobs", ["status", "lease_until", "created_at"], if_not_exists=True)
    op.create_table(
        "graph_extraction_chunks",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("chunk_id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        if_not_exists=True,
    )
    op.create_index("idx_graph_extraction_chunks_pending", "graph_extraction_chunks", ["job_id", "status", "chunk_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_graph_rels_traversal", table_name="graph_relationships")
    op.drop_table("graph_extraction_chunks")
    op.drop_index("idx_graph_jobs_claim", table_name="graph_extraction_jobs")
    op.drop_column("graph_extraction_jobs", "lease_until")
    op.drop_column("graph_extraction_jobs", "lease_token")

"""Add the tenant dimension to audit logs.

Revision ID: 8c57d2e6a940
Revises: 4f82a6b9c013
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8c57d2e6a940"
down_revision = "4f82a6b9c013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "audit_logs",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True, comment="操作租户ID"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_audit_tenant", "audit_logs", ["tenant_id", "created_at"], if_not_exists=True,
    )


def downgrade():
    op.drop_index("idx_audit_tenant", table_name="audit_logs", if_exists=True)
    op.drop_column("audit_logs", "tenant_id", if_exists=True)

"""Add the tenant dimension to audit logs.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
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

"""Scope remote HTTP tools to their owning tenant.

Revision ID: a1b2c3d4e5f6
Revises: e7a8192b4c5d
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "e7a8192b4c5d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "remote_tools",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True, comment="所属租户ID"),
        if_not_exists=True,
    )
    # Legacy rows had no owner. Assign the oldest tenant so existing deployments
    # keep a single, explicit owner instead of remaining globally callable.
    op.execute(
        "UPDATE remote_tools SET tenant_id = "
        "(SELECT id FROM tenants ORDER BY created_at ASC LIMIT 1) "
        "WHERE tenant_id IS NULL"
    )
    op.create_index(
        "idx_remote_tools_tenant", "remote_tools", ["tenant_id"], if_not_exists=True,
    )


def downgrade():
    op.drop_index("idx_remote_tools_tenant", table_name="remote_tools", if_exists=True)
    op.drop_column("remote_tools", "tenant_id", if_exists=True)

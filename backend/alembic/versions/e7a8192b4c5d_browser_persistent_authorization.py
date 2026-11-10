"""Explicit, revocable browser authorization; existing devices remain opt-in.

Revision ID: e7a8192b4c5d
Revises: d6f708192a3b
"""
import sqlalchemy as sa

from alembic import op

revision = "e7a8192b4c5d"
down_revision = "d6f708192a3b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("browser_devices", sa.Column("persistent_authorized_at", sa.DateTime(timezone=True)), if_not_exists=True)
    op.add_column("browser_devices", sa.Column("authorization_version", sa.Integer(), nullable=False, server_default="0"), if_not_exists=True)
    op.add_column("browser_run_grants", sa.Column("device_authorization_version", sa.Integer()), if_not_exists=True)
    op.alter_column("browser_run_grants", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade():
    # Revocation on downgrade prevents former permanent grants becoming usable.
    op.execute("UPDATE browser_run_grants SET revoked_at = CURRENT_TIMESTAMP, expires_at = CURRENT_TIMESTAMP WHERE expires_at IS NULL")
    op.alter_column("browser_run_grants", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_column("browser_run_grants", "device_authorization_version")
    op.drop_column("browser_devices", "authorization_version")
    op.drop_column("browser_devices", "persistent_authorized_at")

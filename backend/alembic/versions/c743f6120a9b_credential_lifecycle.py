"""Version user credentials and isolate refresh rotation per login."""

import sqlalchemy as sa

from alembic import op

revision = "c743f6120a9b"
down_revision = "ab8c62e194d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("credential_version", sa.Integer(), nullable=False,
                                     server_default=sa.text("0")), if_not_exists=True)
    op.add_column("refresh_tokens", sa.Column("family_id", sa.Uuid()), if_not_exists=True)
    # Legacy JWTs carry no family claim. The same deterministic key is derived
    # from their stored SHA-256 hash when clients present them after upgrade.
    op.execute("UPDATE refresh_tokens SET family_id = left(token_hash, 32)::uuid WHERE family_id IS NULL")
    op.alter_column("refresh_tokens", "family_id", nullable=False)
    op.create_index("uq_refresh_tokens_family", "refresh_tokens", ["family_id"], unique=True, if_not_exists=True)


def downgrade() -> None:
    op.drop_index("uq_refresh_tokens_family", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
    op.drop_column("users", "credential_version")

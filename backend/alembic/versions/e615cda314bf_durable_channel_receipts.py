"""Persist channel receipts and version distributed transport/schedule configuration."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e615cda314bf"
down_revision = "c743f6120a9b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column in (("cron_jobs", "schedule_version"), ("channel_configs", "transport_version")):
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")), if_not_exists=True)
    op.create_table(
        "channel_inbox_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text()),
        sa.Column("owner", sa.Uuid()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("channel_id", "event_id", name="uq_channel_inbox_delivery"),
        if_not_exists=True,
    )
    op.create_index("ix_channel_inbox_recall", "channel_inbox_events", ["channel_id", "message_id"],
                    if_not_exists=True, postgresql_where=sa.text("kind = 'recall'"))
    op.create_index("ix_channel_inbox_retention", "channel_inbox_events", ["channel_id", "finished_at"], if_not_exists=True)
    op.create_index("ix_channel_inbox_work", "channel_inbox_events", ["channel_id", "status", "id"],
                    if_not_exists=True, postgresql_where=sa.text("status IN ('queued', 'claimed', 'running')"))
    op.create_table(
        "channel_inbox_leases",
        sa.Column("channel_id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("channel_inbox_leases")
    op.drop_table("channel_inbox_events")
    op.drop_column("channel_configs", "transport_version")
    op.drop_column("cron_jobs", "schedule_version")

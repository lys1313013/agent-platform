"""Record explicit relationships for platform execution traces."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "ab8c62e194d0"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy bootstrap could create current columns before stamping an older revision.
    for table in ("llm_call_logs", "tool_call_logs"):
        op.add_column(
            table, sa.Column("iteration", sa.Integer(), comment="Agent 循环轮次"),
            if_not_exists=True,
        )
    op.add_column(
        "agent_trace_logs",
        sa.Column("root_trace_id", UUID(as_uuid=True), comment="委派链的根执行实例ID"),
        if_not_exists=True,
    )
    op.add_column(
        "agent_trace_logs",
        sa.Column("parent_tool_span_id", UUID(as_uuid=True), comment="发起委派的工具调用明细ID"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_agent_trace_logs_root", "agent_trace_logs", ["root_trace_id"], if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_agent_trace_logs_root", table_name="agent_trace_logs")
    op.drop_column("agent_trace_logs", "parent_tool_span_id")
    op.drop_column("agent_trace_logs", "root_trace_id")
    op.drop_column("tool_call_logs", "iteration")
    op.drop_column("llm_call_logs", "iteration")

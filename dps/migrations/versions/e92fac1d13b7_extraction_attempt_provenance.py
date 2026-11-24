"""Record which worker attempt and DPS baseline produced a result."""

import sqlalchemy as sa
from alembic import op

revision = "e92fac1d13b7"
down_revision = "c1a7e5d3f9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dps_run", sa.Column("attempt_no", sa.Integer(), nullable=True))
    op.add_column("dps_run", sa.Column("base_run_id", sa.String(36), nullable=True))
    op.add_column("dps_ai_call", sa.Column("attempt_no", sa.Integer(), nullable=True))
    op.create_table(
        "dps_tool_call",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("attempt_no", sa.Integer()),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_sha256", sa.String(64)),
        sa.Column("error_type", sa.String(96)),
        sa.Column("spring_version", sa.String(128)),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("read_span_id", sa.String(16)),
    )
    op.create_index("ix_dps_tool_call_job_id", "dps_tool_call", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_dps_tool_call_job_id", table_name="dps_tool_call")
    op.drop_table("dps_tool_call")
    op.drop_column("dps_ai_call", "attempt_no")
    op.drop_column("dps_run", "base_run_id")
    op.drop_column("dps_run", "attempt_no")

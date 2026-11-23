"""Trace ids on the AI call log and the document history (observability, 23 Nov 2026).

``dps_ai_call.trace_id`` / ``span_id`` and ``dps_trace_event.trace_id`` hold the OpenTelemetry ids of the model call
and of the job, so a row opens its trace in Langfuse and a trace finds its rows. All nullable: rows written before
this, or with tracing off, keep working.

Revision ID: b9d3e6f1a4c2
Revises: d4e7f1a2b3c9
Create Date: 2026-11-23 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "b9d3e6f1a4c2"
down_revision = "d4e7f1a2b3c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dps_ai_call") as batch:
        batch.add_column(sa.Column("trace_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("span_id", sa.String(16), nullable=True))
    op.create_index("ix_dps_ai_call_trace", "dps_ai_call", ["trace_id"])
    with op.batch_alter_table("dps_trace_event") as batch:
        batch.add_column(sa.Column("trace_id", sa.String(32), nullable=True))
    op.create_index("ix_dps_trace_event_trace", "dps_trace_event", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_dps_trace_event_trace", table_name="dps_trace_event")
    with op.batch_alter_table("dps_trace_event") as batch:
        batch.drop_column("trace_id")
    op.drop_index("ix_dps_ai_call_trace", table_name="dps_ai_call")
    with op.batch_alter_table("dps_ai_call") as batch:
        batch.drop_column("span_id")
        batch.drop_column("trace_id")

"""Drop the old Covenexa tables nothing writes any more (full review WP7, audit of 20 Nov 2026).

dps_extracted_term (the old agreement flow's terms), dps_event_outbox (data events to Covenexa Core),
dps_learned_wording (the old learning admin) and dps_flow_state (the old flows' saved state), with
dps_job.flow_state_id that pointed into it. Every job now runs in
the skills flow and keeps its result in dps_run; Spring learns of results from dps_event_feed.

Revision ID: d4e7f1a2b3c9
Revises: a5a4b983270e
Create Date: 2026-11-20 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "d4e7f1a2b3c9"
down_revision = "a5a4b983270e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_dps_extracted_term_document_id", table_name="dps_extracted_term")
    op.drop_table("dps_extracted_term")
    op.drop_table("dps_event_outbox")
    op.drop_index("ux_dps_learned_wording", table_name="dps_learned_wording")
    op.drop_table("dps_learned_wording")
    op.drop_index("ix_dps_flow_state_uuid", table_name="dps_flow_state")
    op.drop_table("dps_flow_state")
    with op.batch_alter_table("dps_job") as batch:  # pointed into dps_flow_state
        batch.drop_column("flow_state_id")


def downgrade() -> None:
    with op.batch_alter_table("dps_job") as batch:
        batch.add_column(sa.Column("flow_state_id", sa.String(64), nullable=True))
    op.create_table(
        "dps_flow_state",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("flow_uuid", sa.String(64), nullable=False),
        sa.Column("method_name", sa.String(64), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("pending_feedback_json", sa.Text(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dps_flow_state_uuid", "dps_flow_state", ["flow_uuid", "saved_at"])
    op.create_table(
        "dps_learned_wording",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("phrase", sa.Unicode(300), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("evidence", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_by", sa.String(255), nullable=True),
    )
    op.create_index("ux_dps_learned_wording", "dps_learned_wording", ["kind", "phrase"], unique=True)
    op.create_table(
        "dps_event_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_table(
        "dps_extracted_term",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("dps_document.id"), nullable=False),
        sa.Column("metric_raw", sa.Unicode(128), nullable=False),
        sa.Column("name", sa.Unicode(200), nullable=True),
        sa.Column("covenant_master_id", sa.String(64), nullable=True),
        sa.Column("expression", sa.Unicode(2000), nullable=True),
        sa.Column("operator", sa.String(4), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("frequency", sa.String(16), nullable=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("issues_json", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Unicode(500), nullable=True),
        sa.Column("schedule_json", sa.UnicodeText(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("review_note", sa.Unicode(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dps_extracted_term_document_id", "dps_extracted_term", ["document_id"])

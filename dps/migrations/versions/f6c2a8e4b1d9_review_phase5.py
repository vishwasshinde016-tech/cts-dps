"""Review and learning (CTS DPS Phase 5): review decisions, golden-set candidates, superseded runs

Revision ID: f6c2a8e4b1d9
Revises: e4b7c1d8a3f2
Create Date: 2026-11-17 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6c2a8e4b1d9'
down_revision: Union[str, Sequence[str], None] = 'e4b7c1d8a3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_DT = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table('dps_run') as batch:
        batch.add_column(sa.Column('superseded_by', sa.String(64), nullable=True))
    op.create_table(
        'dps_review_decision',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('run_no', sa.Integer(), nullable=False),
        sa.Column('field_id', sa.String(64), nullable=False),
        sa.Column('reviewer', sa.String(255), nullable=False),
        sa.Column('action', sa.String(16), nullable=False),
        sa.Column('old_value_json', sa.UnicodeText(), nullable=True),
        sa.Column('new_value_json', sa.UnicodeText(), nullable=True),
        sa.Column('reason_code', sa.String(32), nullable=True),
        sa.Column('comment', sa.Unicode(2000), nullable=True),
        sa.Column('sampled', sa.Boolean(), nullable=False),
        sa.Column('at', UTC_DT, nullable=False),
    )
    op.create_index('ix_dps_review_decision_job_id', 'dps_review_decision', ['job_id'])
    op.create_table(
        'dps_golden_candidate',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('field_id', sa.String(64), nullable=False),
        sa.Column('job_type', sa.String(32), nullable=False),
        sa.Column('payload_json', sa.UnicodeText(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('decided_by', sa.String(255), nullable=True),
        sa.Column('created_at', UTC_DT, nullable=False),
        sa.Column('decided_at', UTC_DT, nullable=True),
    )


def downgrade() -> None:
    op.drop_table('dps_golden_candidate')
    op.drop_index('ix_dps_review_decision_job_id', table_name='dps_review_decision')
    op.drop_table('dps_review_decision')
    with op.batch_alter_table('dps_run') as batch:
        batch.drop_column('superseded_by')

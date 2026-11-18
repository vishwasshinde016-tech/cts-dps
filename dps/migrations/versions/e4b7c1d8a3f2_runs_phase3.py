"""Runs (CTS DPS Phase 3): dps_run, one dps.result/1.0 per run of a job

Revision ID: e4b7c1d8a3f2
Revises: d2a6b9c4e1f7
Create Date: 2026-11-17 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4b7c1d8a3f2'
down_revision: Union[str, Sequence[str], None] = 'd2a6b9c4e1f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_run',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('run_no', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('trigger', sa.String(24), nullable=False),
        sa.Column('result_json', sa.UnicodeText(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ux_dps_run_job_no', 'dps_run', ['job_id', 'run_no'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_dps_run_job_no', table_name='dps_run')
    op.drop_table('dps_run')

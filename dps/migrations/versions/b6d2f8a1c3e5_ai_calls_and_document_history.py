"""AI traceability and document history: every model call (dps_ai_call) and each document's steps (dps_trace_event)

Revision ID: b6d2f8a1c3e5
Revises: a9c3e6f1b4d8
Create Date: 2026-11-09 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6d2f8a1c3e5'
down_revision: Union[str, Sequence[str], None] = 'a9c3e6f1b4d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_ai_call',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('purpose', sa.String(length=24), nullable=False),
        sa.Column('user', sa.String(length=255), nullable=False),
        sa.Column('cp_id', sa.String(length=64), nullable=True),
        sa.Column('document_id', sa.String(length=36), nullable=True),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('error', sa.Unicode(length=500), nullable=True),
        sa.Column('seconds', sa.Float(), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('request_text', sa.UnicodeText(), nullable=True),
        sa.Column('response_text', sa.UnicodeText(), nullable=True),
    )
    op.create_index('ix_dps_ai_call_at', 'dps_ai_call', ['at'])
    op.create_index('ix_dps_ai_call_document', 'dps_ai_call', ['document_id'])
    op.create_table(
        'dps_trace_event',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('document_id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('step', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=255), nullable=False),
        sa.Column('summary', sa.Unicode(length=1000), nullable=False),
        sa.Column('detail_json', sa.UnicodeText(), nullable=False),
    )
    op.create_index('ix_dps_trace_document', 'dps_trace_event', ['document_id', 'at'])


def downgrade() -> None:
    op.drop_index('ix_dps_trace_document', table_name='dps_trace_event')
    op.drop_table('dps_trace_event')
    op.drop_index('ix_dps_ai_call_document', table_name='dps_ai_call')
    op.drop_index('ix_dps_ai_call_at', table_name='dps_ai_call')
    op.drop_table('dps_ai_call')

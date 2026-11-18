"""Operations (CTS DPS Phase 6): scheduler leader lock and daily audit seals

Revision ID: a7d3e9f5c2b8
Revises: f6c2a8e4b1d9
Create Date: 2026-11-18 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d3e9f5c2b8'
down_revision: Union[str, Sequence[str], None] = 'f6c2a8e4b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_DT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'dps_leader',
        sa.Column('name', sa.String(32), primary_key=True),
        sa.Column('holder', sa.String(64), nullable=False),
        sa.Column('until', UTC_DT, nullable=False),
    )
    op.create_table(
        'dps_audit_seal',
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('rows', sa.Integer(), nullable=False),
        sa.Column('prev_hash', sa.String(64), nullable=False),
        sa.Column('head_hash', sa.String(64), nullable=False),
        sa.Column('file_sha256', sa.String(64), nullable=True),
        sa.Column('created_at', UTC_DT, nullable=False),
    )


def downgrade() -> None:
    op.drop_table('dps_audit_seal')
    op.drop_table('dps_leader')

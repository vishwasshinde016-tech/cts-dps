"""Event push to Spring (CTS DPS, architect gap G6): dps_push_cursor

Revision ID: c1d5e8f2a9b4
Revises: b9e4f1a6d3c7
Create Date: 2026-11-18 02:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d5e8f2a9b4'
down_revision: Union[str, Sequence[str], None] = 'b9e4f1a6d3c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_push_cursor',
        sa.Column('name', sa.String(32), primary_key=True),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('dps_push_cursor')

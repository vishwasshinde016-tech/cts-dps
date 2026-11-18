"""AI answer cache (Phase 3a)

Revision ID: a1f4c7e2d9b5
Revises: c3e9a7b5d1f4
Create Date: 2026-11-11 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1f4c7e2d9b5'
down_revision: Union[str, Sequence[str], None] = 'c3e9a7b5d1f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_ai_cache',
        sa.Column('key', sa.String(length=64), primary_key=True),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('answer', sa.UnicodeText(), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('dps_ai_cache')

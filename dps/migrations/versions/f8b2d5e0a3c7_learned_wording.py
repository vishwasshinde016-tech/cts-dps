"""Learning loop: wordings an administrator added to DPS's catalog from reviewers' decisions

Revision ID: f8b2d5e0a3c7
Revises: e7a1c4d9f2b6
Create Date: 2026-11-07 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8b2d5e0a3c7'
down_revision: Union[str, Sequence[str], None] = 'e7a1c4d9f2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_learned_wording',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('phrase', sa.String(length=300), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('evidence', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('added_by', sa.String(length=255), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('retired_by', sa.String(length=255), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ux_dps_learned_wording', 'dps_learned_wording', ['kind', 'phrase'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_dps_learned_wording', table_name='dps_learned_wording')
    op.drop_table('dps_learned_wording')

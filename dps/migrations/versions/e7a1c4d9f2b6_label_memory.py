"""Borrower memory: statement wordings a person confirmed for a CP's standard lines

Revision ID: e7a1c4d9f2b6
Revises: d5e9f3a2b8c4
Create Date: 2026-11-07 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7a1c4d9f2b6'
down_revision: Union[str, Sequence[str], None] = 'd5e9f3a2b8c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_label_memory',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('cp_id', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=300), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('source_label', sa.String(length=300), nullable=False),
        sa.Column('document_id', sa.String(length=36), nullable=True),
        sa.Column('confirmed_by', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ux_dps_label_memory', 'dps_label_memory', ['cp_id', 'label'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_dps_label_memory', table_name='dps_label_memory')
    op.drop_table('dps_label_memory')

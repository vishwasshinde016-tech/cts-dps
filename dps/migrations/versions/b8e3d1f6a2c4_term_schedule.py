"""Covenant schedule on extracted terms (Phase 3b-1): step-down levels, test months, cure right

Revision ID: b8e3d1f6a2c4
Revises: a1f4c7e2d9b5
Create Date: 2026-11-11 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e3d1f6a2c4'
down_revision: Union[str, Sequence[str], None] = 'a1f4c7e2d9b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('dps_extracted_term', sa.Column('schedule_json', sa.UnicodeText(), nullable=True))


def downgrade() -> None:
    op.drop_column('dps_extracted_term', 'schedule_json')

"""12-month covenants: the period each statement figure covers (dps_statement_value.period_months)

A quarter's profit is not a year's. Each profit and cash-flow figure now records how many months it covers (3, 6,
9 or 12), and the trailing-twelve-month figures built from them are stored as their own keys (EBITDA_TTM, ...).

Revision ID: c3e9a7b5d1f4
Revises: b6d2f8a1c3e5
Create Date: 2026-11-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e9a7b5d1f4'
down_revision: Union[str, Sequence[str], None] = 'b6d2f8a1c3e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('dps_statement_value', sa.Column('period_months', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('dps_statement_value', 'period_months')

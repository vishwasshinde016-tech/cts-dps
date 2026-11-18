"""Rollout mode per job (CTS DPS Phase 7 pilot): SHADOW, ASSISTED or NORMAL

Revision ID: b9e4f1a6d3c7
Revises: a7d3e9f5c2b8
Create Date: 2026-11-18 01:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b9e4f1a6d3c7'
down_revision: Union[str, Sequence[str], None] = 'a7d3e9f5c2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('dps_job') as batch:
        batch.add_column(sa.Column('rollout', sa.String(16), nullable=False, server_default='NORMAL'))


def downgrade() -> None:
    with op.batch_alter_table('dps_job') as batch:
        batch.drop_column('rollout', mssql_drop_default=True)

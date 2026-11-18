"""Derived artefacts (CTS DPS Phase 2): dps_artifact, write-once with hashes

Revision ID: d2a6b9c4e1f7
Revises: c9f1a3d5b7e2
Create Date: 2026-11-17 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2a6b9c4e1f7'
down_revision: Union[str, Sequence[str], None] = 'c9f1a3d5b7e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dps_artifact',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('version_id', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('path', sa.String(400), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=True),
        sa.Column('pipeline_version', sa.String(32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ux_dps_artifact_path', 'dps_artifact', ['path'], unique=True)
    op.create_index('ix_dps_artifact_source', 'dps_artifact', ['source_sha256', 'kind', 'pipeline_version'])


def downgrade() -> None:
    op.drop_index('ix_dps_artifact_source', table_name='dps_artifact')
    op.drop_index('ux_dps_artifact_path', table_name='dps_artifact')
    op.drop_table('dps_artifact')

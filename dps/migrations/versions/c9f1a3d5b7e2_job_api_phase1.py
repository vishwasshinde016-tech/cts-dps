"""Job API and queue upgrades (CTS DPS Phase 1): job columns, batches, attempts, tenant limits, event feed

Every change is additive: existing rows get job_type from flow, priority 1 and due_at = created_at, so the
older upload route and the Covenexa screens keep working.

Revision ID: c9f1a3d5b7e2
Revises: b8e3d1f6a2c4
Create Date: 2026-11-17 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9f1a3d5b7e2'
down_revision: Union[str, Sequence[str], None] = 'b8e3d1f6a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_DT = sa.DateTime(timezone=True)
JOB_COLUMNS = [
    sa.Column('job_type', sa.String(32), nullable=False, server_default='EXTRACT_AGREEMENT'),
    sa.Column('tenant_id', sa.String(64), nullable=False, server_default='default'),
    sa.Column('priority', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('due_at', UTC_DT, nullable=True),
    sa.Column('idempotency_key', sa.String(128), nullable=True),
    sa.Column('request_hash', sa.String(64), nullable=True),
    sa.Column('request_json', sa.UnicodeText(), nullable=True),
    sa.Column('batch_id', sa.String(36), nullable=True),
    sa.Column('client_file_reference', sa.Unicode(128), nullable=True),
    sa.Column('correlation_id', sa.String(128), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('started_at', UTC_DT, nullable=True),
    sa.Column('finished_at', UTC_DT, nullable=True),
    sa.Column('error_code', sa.String(48), nullable=True),
    sa.Column('pipeline_version', sa.String(32), nullable=False, server_default='1'),
    sa.Column('document_version_id', sa.String(64), nullable=True),
    sa.Column('sha256', sa.String(64), nullable=True),
    sa.Column('crash_count', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('requested_by', sa.String(255), nullable=True),
]


def upgrade() -> None:
    for column in JOB_COLUMNS:
        op.add_column('dps_job', column)
    op.execute("UPDATE dps_job SET job_type = 'EXTRACT_STATEMENT' WHERE flow = 'STATEMENT_PROCESSING'")
    op.execute("UPDATE dps_job SET due_at = created_at WHERE due_at IS NULL")
    op.drop_index('ix_dps_job_claim', table_name='dps_job')
    op.create_index('ix_dps_job_claim', 'dps_job', ['pool', 'status', 'priority', 'due_at'])
    op.create_index('ix_dps_job_batch_id', 'dps_job', ['batch_id'])
    op.create_index('ux_dps_job_idempotency', 'dps_job', ['tenant_id', 'idempotency_key'], unique=True,
                    mssql_where=sa.text('idempotency_key IS NOT NULL'),
                    sqlite_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_index('ux_dps_job_input', 'dps_job',
                    ['tenant_id', 'document_version_id', 'job_type', 'pipeline_version'], unique=True,
                    mssql_where=sa.text("document_version_id IS NOT NULL AND job_type <> 'REPROCESS'"),
                    sqlite_where=sa.text("document_version_id IS NOT NULL AND job_type <> 'REPROCESS'"))

    op.add_column('dps_document', sa.Column('tenant_id', sa.String(64), nullable=False, server_default='default'))
    op.add_column('dps_document', sa.Column('external_version_id', sa.String(64), nullable=True))
    op.add_column('dps_document', sa.Column('object_key', sa.String(500), nullable=True))
    op.create_index('ix_dps_document_external_version_id', 'dps_document', ['external_version_id'])

    op.create_table(
        'dps_batch',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('batch_ref', sa.Unicode(128), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('item_count', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('created_at', UTC_DT, nullable=False),
        sa.Column('finished_at', UTC_DT, nullable=True),
    )
    op.create_index('ux_dps_batch_idempotency', 'dps_batch', ['tenant_id', 'idempotency_key'], unique=True)
    op.create_table(
        'dps_job_attempt',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.String(64), nullable=False),
        sa.Column('started_at', UTC_DT, nullable=False),
        sa.Column('ended_at', UTC_DT, nullable=True),
        sa.Column('outcome', sa.String(24), nullable=True),
        sa.Column('error_code', sa.String(48), nullable=True),
    )
    op.create_index('ix_dps_job_attempt_job_id', 'dps_job_attempt', ['job_id'])
    op.create_table(
        'dps_tenant_limit',
        sa.Column('tenant_id', sa.String(64), primary_key=True),
        sa.Column('max_queued', sa.Integer(), nullable=False),
        sa.Column('max_running', sa.Integer(), nullable=False),
        sa.Column('max_running_per_batch', sa.Integer(), nullable=False),
    )
    op.create_table(
        'dps_event_feed',
        sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.String(36), nullable=False, unique=True),
        sa.Column('type', sa.String(48), nullable=False),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('job_id', sa.String(36), nullable=True),
        sa.Column('document_version_id', sa.String(64), nullable=True),
        sa.Column('correlation_id', sa.String(128), nullable=True),
        sa.Column('occurred_at', UTC_DT, nullable=False),
        sa.Column('payload_json', sa.UnicodeText(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('dps_event_feed')
    op.drop_table('dps_tenant_limit')
    op.drop_index('ix_dps_job_attempt_job_id', table_name='dps_job_attempt')
    op.drop_table('dps_job_attempt')
    op.drop_index('ux_dps_batch_idempotency', table_name='dps_batch')
    op.drop_table('dps_batch')
    op.drop_index('ix_dps_document_external_version_id', table_name='dps_document')
    with op.batch_alter_table('dps_document') as batch:
        batch.drop_column('object_key', mssql_drop_default=True)
        batch.drop_column('external_version_id', mssql_drop_default=True)
        batch.drop_column('tenant_id', mssql_drop_default=True)
    op.drop_index('ux_dps_job_input', table_name='dps_job')
    op.drop_index('ux_dps_job_idempotency', table_name='dps_job')
    op.drop_index('ix_dps_job_batch_id', table_name='dps_job')
    op.drop_index('ix_dps_job_claim', table_name='dps_job')
    with op.batch_alter_table('dps_job') as batch:
        for column in reversed(JOB_COLUMNS):
            batch.drop_column(column.name, mssql_drop_default=True)
    op.create_index('ix_dps_job_claim', 'dps_job', ['status', 'pool'])

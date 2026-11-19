"""Deduplicate jobs by business context and document identity."""

import sqlalchemy as sa
from alembic import op

revision = "a5a4b983270e"
down_revision = "c1d5e8f2a9b4"


def upgrade() -> None:
    op.add_column("dps_job", sa.Column("input_key", sa.String(64), nullable=True))
    op.create_index("ux_dps_job_scoped_input", "dps_job", ["tenant_id", "input_key"], unique=True,
                    mssql_where=sa.text("input_key IS NOT NULL"), sqlite_where=sa.text("input_key IS NOT NULL"))
    op.drop_index("ux_dps_job_input", table_name="dps_job")


def downgrade() -> None:
    # The former unique index cannot represent multiple business contexts for one version.
    op.create_index("ux_dps_job_input", "dps_job",
                    ["tenant_id", "document_version_id", "job_type", "pipeline_version"], unique=True,
                    mssql_where=sa.text("document_version_id IS NOT NULL AND job_type <> 'REPROCESS'"),
                    sqlite_where=sa.text("document_version_id IS NOT NULL AND job_type <> 'REPROCESS'"))
    op.drop_index("ux_dps_job_scoped_input", table_name="dps_job")
    op.drop_column("dps_job", "input_key")

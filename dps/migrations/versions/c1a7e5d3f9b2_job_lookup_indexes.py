"""Lookup indexes on dps_job (improvement plan S5, 24 Nov 2026).

Amendments, document sets and the bank app's covenant-status read jobs by ``document_version_id`` and
``document_id``; support reads them by ``correlation_id``. None of the three had an index, so each lookup scanned the
whole job table, which grows with every upload.

Revision ID: c1a7e5d3f9b2
Revises: b9d3e6f1a4c2
Create Date: 2026-11-24 10:00:00
"""

from alembic import op

revision = "c1a7e5d3f9b2"
down_revision = "b9d3e6f1a4c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_dps_job_document", "dps_job", ["document_id"])
    op.create_index("ix_dps_job_version", "dps_job", ["tenant_id", "document_version_id"])
    op.create_index("ix_dps_job_correlation", "dps_job", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_dps_job_correlation", table_name="dps_job")
    op.drop_index("ix_dps_job_version", table_name="dps_job")
    op.drop_index("ix_dps_job_document", table_name="dps_job")

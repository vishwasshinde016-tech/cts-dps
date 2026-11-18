"""Text from documents and people as NVARCHAR: VARCHAR stored the rupee sign as "?" and ">=" signs as "="

Revision ID: a9c3e6f1b4d8
Revises: f8b2d5e0a3c7
Create Date: 2026-11-09 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9c3e6f1b4d8'
down_revision: Union[str, Sequence[str], None] = 'f8b2d5e0a3c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, length, nullable)
COLUMNS = [
    ('dps_document', 'file_name', 255, False),
    ('dps_extracted_term', 'metric_raw', 128, False),
    ('dps_extracted_term', 'name', 200, True),
    ('dps_extracted_term', 'expression', 2000, True),
    ('dps_extracted_term', 'quote', 500, True),
    ('dps_extracted_term', 'review_note', 2000, True),
    ('dps_label_memory', 'label', 300, False),
    ('dps_label_memory', 'source_label', 300, False),
    ('dps_learned_wording', 'phrase', 300, False),
    ('dps_statement_value', 'note', 2000, True),
]
# SQL Server will not alter a column an index covers: drop these first and build them again after.
INDEXES = [
    ('ux_dps_label_memory', 'dps_label_memory', ['cp_id', 'label']),
    ('ux_dps_learned_wording', 'dps_learned_wording', ['kind', 'phrase']),
]


def _change(to_unicode: bool) -> None:
    # SQLite stores any text as given and has no ALTER COLUMN; only SQL Server needs the change.
    if op.get_bind().dialect.name != 'mssql':
        return
    for name, table, _ in INDEXES:
        op.drop_index(name, table_name=table)
    for table, column, length, nullable in COLUMNS:
        new, old = (sa.Unicode(length), sa.String(length)) if to_unicode else (sa.String(length), sa.Unicode(length))
        op.alter_column(table, column, type_=new, existing_type=old, existing_nullable=nullable)
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, unique=True)


def upgrade() -> None:
    _change(to_unicode=True)


def downgrade() -> None:
    _change(to_unicode=False)

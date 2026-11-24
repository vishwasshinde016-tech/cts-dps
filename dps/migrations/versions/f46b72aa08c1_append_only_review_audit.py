"""Prevent updates and deletes of business audit and review history."""

from alembic import op

revision = "f46b72aa08c1"
down_revision = "ae43796b724a"
branch_labels = None
depends_on = None

TABLES = ("dps_audit_event", "dps_review_decision")


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    for table in TABLES:
        if dialect == "mssql":
            op.execute(f"CREATE TRIGGER tr_{table}_append_only ON {table} AFTER UPDATE, DELETE AS "
                       "BEGIN SET NOCOUNT ON; IF EXISTS (SELECT 1 FROM deleted) "
                       "BEGIN ;THROW 50001, 'DPS audit history is append-only.', 1; END END")
        elif dialect == "sqlite":
            for operation in ("update", "delete"):
                op.execute(f"CREATE TRIGGER tr_{table}_no_{operation} BEFORE {operation.upper()} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'DPS audit history is append-only.'); END")


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    for table in TABLES:
        if dialect == "mssql":
            op.execute(f"DROP TRIGGER tr_{table}_append_only")
        elif dialect == "sqlite":
            for operation in ("update", "delete"):
                op.execute(f"DROP TRIGGER tr_{table}_no_{operation}")

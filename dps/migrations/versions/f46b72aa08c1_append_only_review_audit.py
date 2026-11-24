"""Prevent updates and deletes of business audit and review history."""

from alembic import op

revision = "f46b72aa08c1"
down_revision = "ae43796b724a"
branch_labels = None
depends_on = None

def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "mssql":
        op.execute("CREATE TRIGGER tr_dps_audit_event_append_only ON dps_audit_event AFTER UPDATE, DELETE AS "
                   "BEGIN SET NOCOUNT ON; IF EXISTS (SELECT 1 FROM deleted) "
                   "BEGIN ;THROW 50001, 'DPS audit history is append-only.', 1; END END")
        op.execute("CREATE TRIGGER tr_dps_review_decision_append_only ON dps_review_decision AFTER UPDATE, DELETE AS "
                   "BEGIN SET NOCOUNT ON; IF EXISTS (SELECT 1 FROM deleted) "
                   "BEGIN ;THROW 50001, 'DPS audit history is append-only.', 1; END END")
    elif dialect == "sqlite":
        op.execute("CREATE TRIGGER tr_dps_audit_event_no_update BEFORE UPDATE ON dps_audit_event "
                   "BEGIN SELECT RAISE(ABORT, 'DPS audit history is append-only.'); END")
        op.execute("CREATE TRIGGER tr_dps_audit_event_no_delete BEFORE DELETE ON dps_audit_event "
                   "BEGIN SELECT RAISE(ABORT, 'DPS audit history is append-only.'); END")
        op.execute("CREATE TRIGGER tr_dps_review_decision_no_update BEFORE UPDATE ON dps_review_decision "
                   "BEGIN SELECT RAISE(ABORT, 'DPS audit history is append-only.'); END")
        op.execute("CREATE TRIGGER tr_dps_review_decision_no_delete BEFORE DELETE ON dps_review_decision "
                   "BEGIN SELECT RAISE(ABORT, 'DPS audit history is append-only.'); END")


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "mssql":
        op.execute("DROP TRIGGER tr_dps_audit_event_append_only")
        op.execute("DROP TRIGGER tr_dps_review_decision_append_only")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER tr_dps_audit_event_no_update")
        op.execute("DROP TRIGGER tr_dps_audit_event_no_delete")
        op.execute("DROP TRIGGER tr_dps_review_decision_no_update")
        op.execute("DROP TRIGGER tr_dps_review_decision_no_delete")

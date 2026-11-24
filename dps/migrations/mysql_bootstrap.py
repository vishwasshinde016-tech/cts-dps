"""Generate the one-time DPS schema for the existing CTS cov_monitoring database."""

from sqlalchemy import MetaData
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

import dps.models  # noqa: F401 - registers the model tables
from dps.db import Base

BASELINE = "f46b72aa08c1"
HEADER = """\
-- DPS MySQL 8.4 bootstrap. Connect with --database=cov_monitoring.
-- Run with the mysql client in batch mode, with its default stop-on-error behavior.
-- Never use --force. MySQL commits each DDL statement individually.

SET @dps_target_ok = CAST(DATABASE() AS BINARY) = CAST('cov_monitoring' AS BINARY);
SET @dps_lock = IF(@dps_target_ok, GET_LOCK('cov_monitoring.dps_schema_install', 0), 0);

SET @dps_preflight = CASE
    WHEN COALESCE(@dps_target_ok, 0) <> 1
        THEN 'wrong_database'
    WHEN SUBSTRING_INDEX(VERSION(), '.', 2) <> '8.4' THEN 'unexpected_mysql_version'
    WHEN COALESCE(@dps_lock, 0) <> 1
        THEN 'bootstrap_locked'
    WHEN EXISTS (
        SELECT 1 FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND LEFT(LOWER(TABLE_NAME), 4) = 'dps_'
    ) OR EXISTS (
        SELECT 1 FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = DATABASE() AND LEFT(LOWER(TRIGGER_NAME), 4) = 'dps_'
    ) THEN 'existing_or_partial_dps_install'
    ELSE 'ready'
END;
SELECT @dps_preflight AS dps_bootstrap_preflight;
-- PREPARE from NULL fails and stops the batch before any DDL on an invalid target.
SET @dps_guard_sql = IF(@dps_preflight = 'ready', 'DO 0', NULL);
PREPARE dps_guard FROM @dps_guard_sql;
EXECUTE dps_guard;
DEALLOCATE PREPARE dps_guard;
"""


def bootstrap_sql() -> str:
    metadata = MetaData()
    for model in Base.metadata.sorted_tables:
        table = model.to_metadata(metadata)
        table.dialect_options["mysql"].update(engine="InnoDB", charset="utf8mb4", collate="utf8mb4_0900_bin")
    mysql = dialect()
    statements = [HEADER.rstrip()]
    for table in metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=mysql)).strip() + ";")
        statements.extend(str(CreateIndex(index).compile(dialect=mysql)) + ";"
                          for index in sorted(table.indexes, key=lambda index: index.name))
    for table in ("dps_audit_event", "dps_review_decision"):
        for operation in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table}\n"
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DPS audit history is append-only.';"
            )
    statements.append(
        "CREATE TABLE dps_alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)\n"
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;\n"
        f"INSERT INTO dps_alembic_version (version_num) VALUES ('{BASELINE}');\n"
        "SELECT RELEASE_LOCK('cov_monitoring.dps_schema_install') AS dps_bootstrap_released;"
    )
    return "\n".join(line.rstrip() for line in "\n\n".join(statements).splitlines()) + "\n"


if __name__ == "__main__":
    print(bootstrap_sql(), end="")

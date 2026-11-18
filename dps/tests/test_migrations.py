"""Alembic migrations: a fresh database upgraded to head matches the models, and the newest step reverses.

Phase 1 (CTS DPS) promise: the job API migration is additive and reversible, and existing rows keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from dps.db import Base

ROOT = Path(__file__).resolve().parents[1]


def _config(url: str) -> Config:
    # No alembic.ini: its logging section would replace the app's log handlers for every later test.
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@pytest.fixture
def db_url(tmp_path, monkeypatch) -> str:
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    monkeypatch.setenv("DPS_DATABASE_URL", url)
    from dps.settings import get_settings
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


def test_upgrade_to_head_matches_the_models(db_url):
    command.upgrade(_config(db_url), "head")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    # Type and server-default spelling differ between SQLite reflection and the models; tables, columns and
    # indexes must not.
    structural = [d for d in diff if d[0] in ("add_table", "remove_table", "add_column", "remove_column",
                                                "add_index", "remove_index")]
    assert structural == [], structural


def test_phase1_step_keeps_old_jobs_and_reverses(db_url):
    cfg = _config(db_url)
    command.upgrade(cfg, "b8e3d1f6a2c4")
    engine = create_engine(db_url)
    created = datetime(2026, 11, 1, tzinfo=UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dps_document (id, type, file_name, storage_path, sha256, size_bytes, status, "
                          "uploaded_by, uploaded_at) VALUES ('d1', 'FINANCIAL_STATEMENT', 'f.xlsx', 'p', 'h', 1, "
                          "'QUEUED', 'u', :t)"), {"t": created})
        conn.execute(text("INSERT INTO dps_job (id, document_id, flow, status, pool, attempts, steps_json, "
                          "created_at) VALUES ('j1', 'd1', 'STATEMENT_PROCESSING', 'QUEUED', 'STANDARD', 0, '[]', :t)"),
                     {"t": created})
    command.upgrade(cfg, "c9f1a3d5b7e2")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT job_type, priority, due_at, tenant_id FROM dps_job WHERE id = 'j1'")).one()
    assert row.job_type == "EXTRACT_STATEMENT"
    assert row.priority == 1 and row.due_at is not None and row.tenant_id == "default"
    command.downgrade(cfg, "b8e3d1f6a2c4")
    tables = set(inspect(create_engine(db_url)).get_table_names())
    assert "dps_event_feed" not in tables and "dps_batch" not in tables
    columns = {c["name"] for c in inspect(create_engine(db_url)).get_columns("dps_job")}
    assert "priority" not in columns and "flow" in columns
    with create_engine(db_url).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM dps_job")).scalar() == 1

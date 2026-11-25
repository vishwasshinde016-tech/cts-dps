"""Live run of 25 Nov 2026 on 12 real files: workers died on "database is locked", a laptop read one document at a
time, and the AI read one batch at a time. These tests keep the fixes."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from dps.db import Database
from dps.settings import Settings


def test_sqlite_waits_for_a_writer_instead_of_failing(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'w.db').as_posix()}")
    with db.engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() >= 30000
    with db.engine.begin() as c:
        c.execute(text("create table t (n integer)"))
    errors = []

    def write(n):
        try:
            for i in range(30):
                with db.engine.begin() as c:
                    c.execute(text("insert into t values (:n)"), {"n": n * 100 + i})
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.engine.connect() as c:
        assert c.execute(text("select count(*) from t")).scalar() == 120


def test_a_worker_outlives_a_failed_loop():
    from dps.worker import run_loop

    stop = {"flag": False}
    calls = []

    class Flaky:
        def run_once(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("(sqlite3.OperationalError) database is locked")
            if len(calls) == 3:
                stop["flag"] = True
            return True

    pauses = []
    run_loop(Flaky(), stop, sleep=pauses.append)
    assert len(calls) == 3 and pauses == [5]  # paused once after the error, then went on


def test_defaults_read_several_documents_and_batches_at_once():
    s = Settings()
    assert s.worker_threads == 3 and s.ai_parallel_reads == 3  # 9 calls at once stays within the Azure rate


@pytest.mark.parametrize("name,media", [("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.pdf", "application/pdf")])
def test_the_submit_tool_sends_every_upload_type_as_auto(name, media):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "submit_job.py"
    spec = importlib.util.spec_from_file_location("submit_job", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.MEDIA[Path(name).suffix] == media and mod.JOBS[0] == "AUTO"


def test_a_model_answer_is_not_cut_off_at_3000_tokens():
    """24 of 66 live calls stopped at exactly 3000 tokens; their covenant JSON was cut off and lost."""
    from dps.agent.gateway import DEFAULT_MAX_TOKENS, Route, Routes

    assert Route("strong", "azure", "azure/x").max_tokens == DEFAULT_MAX_TOKENS >= 16000
    assert Routes.load(None, "azure/x").routes["strong"].max_tokens == DEFAULT_MAX_TOKENS


def test_the_timeout_setting_reaches_every_route():
    """DPS_AI_TIMEOUT_SECONDS was read but never used: every call waited 60 s and 8 of 21 timed out under load."""
    from dps.agent.gateway import DEFAULT_TIMEOUT_SECONDS, Routes

    assert all(r.timeout_s == 120 for r in Routes.load(None, "azure/x", timeout_s=120).routes.values())
    assert all(r.timeout_s == DEFAULT_TIMEOUT_SECONDS >= 180 for r in Routes.load(None, "azure/x").routes.values())
    assert Settings().ai_timeout_seconds >= 180


def test_a_statement_with_no_period_date_still_follows_the_contract():
    import json
    from pathlib import Path

    import jsonschema

    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "result.json")
                        .read_text(encoding="utf-8"))
    line = schema["$defs"]["statementLineValue"] if "$defs" in schema else schema["definitions"]["statementLineValue"]
    jsonschema.validate({"amount": 61.2, "unit": "INR_CR", "asOfDate": None}, line)


def test_an_unknown_page_count_gets_a_long_documents_parse_time():
    """A 26-page report with hidden page objects got the 10-page limit and failed with PARSE_TIMEOUT."""
    from dps.parse.sandbox import Limits

    assert Limits().wall_clock(None) == 240 and Limits.for_pool("HEAVY").wall_clock(None) == 2400
    assert Limits().wall_clock(5) == 120  # a known short document keeps the short limit


def test_a_short_scan_gets_time_for_ocr_and_the_docling_model_load():
    """A 6-page scanned waiver took 379 s on an idle laptop (OCR 204 s, Docling 165 s) and failed the 300 s limit."""
    from dps.parse.sandbox import Limits

    assert Limits.for_pool("HEAVY").wall_clock(6) >= 600

"""The service's shared HTTP behaviour: health, sign-in, the data-source contract and job operations.

The old Covenexa routes (upload, documents, terms, spreads, manual values, recommendations, ask) were deleted in the
full review (G12, WP7); their tests went with them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from conftest import h

from dps.models import Document, DocumentCpLink, Job, StatementValue

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def session(app):
    return app.state.db.session_factory()


# ---------- health / auth ----------

def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "UP" and r.json()["version"]
    assert "X-Correlation-Id" in r.headers


def test_correlation_id_echoed(client):
    r = client.get("/health", headers={"X-Correlation-Id": "abc"})
    assert r.headers["X-Correlation-Id"] == "abc"


def test_requires_identity(client):
    r = client.get("/dps/v1/jobs")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_the_old_covenexa_routes_are_gone(client):
    """G12: in CTS the DMS uploads and Spring approves; DPS has no upload, publish or spread of its own."""
    for method, path in (("POST", "/dps/v1/documents"), ("GET", "/dps/v1/documents?cpId=CP-1"),
                         ("POST", "/dps/v1/values"), ("POST", "/dps/v1/ask"), ("GET", "/dps/v1/catalog"),
                         ("PATCH", "/dps/v1/terms/t1"), ("POST", "/dps/v1/recommendations")):
        assert client.request(method, path, headers=h()).status_code in (404, 405), path


class _Deadlock(Exception):
    def __str__(self) -> str:
        return "(1205, b'Transaction was deadlocked on lock resources with another process')"


def test_lock_conflicts_are_recognised():
    from dps.errors import is_lock_conflict

    assert is_lock_conflict(_Deadlock())
    assert is_lock_conflict(Exception("database is locked"))
    assert is_lock_conflict(Exception("(1222, b'Lock request time out period exceeded')"))
    assert not is_lock_conflict(Exception("(208, b'Invalid object name')"))


def test_string_parameters_are_sent_as_varchar_on_sql_server():
    from sqlalchemy import select
    from sqlalchemy.dialects import mssql

    sql = str(select(Document.id).where(Document.sha256 == "abc").compile(dialect=mssql.dialect()))
    assert "CAST(" in sql and "AS VARCHAR(64))" in sql


# ---------- the bank app's data-source contract (/api/data/values) ----------

def add_value(app, cp, key, value, as_of, superseded=False, doc_id=None, unit="AED mn"):
    with session(app) as s:
        s.add(StatementValue(cp_id=cp, key=key, value=value, unit=unit, as_of_date=as_of,
                             source_type="DOCUMENT", superseded=superseded, document_id=doc_id, page=3))
        s.commit()


def test_values_point_in_time_and_window(client, app):
    add_value(app, "CP-1", "EBITDA", 200.0, date(2025, 12, 31))
    add_value(app, "CP-1", "EBITDA", 210.0, date(2026, 3, 31))
    add_value(app, "CP-1", "EBITDA", 999.0, date(2026, 3, 31), superseded=True)
    add_value(app, "CP-1", "INTEREST_PAID", 41.0, date(2026, 3, 31))
    add_value(app, "CP-1", "EBITDA", 230.0, date(2026, 6, 30))

    pit = client.post("/api/data/values", json={"cpId": "CP-1", "to": "2026-03-31",
                                                "keys": ["EBITDA", "INTEREST_PAID", "CASH"]}, headers=h()).json()
    assert pit["points"] == [{"asOfDate": "2026-03-31", "values": {"EBITDA": 210.0, "INTEREST_PAID": 41.0}}]
    assert pit["missingKeys"] == ["CASH"]
    # Core reads this as java.time.Instant, which needs an explicit offset (SQLite returns naive values).
    assert datetime.fromisoformat(pit["sourceChangedAt"]).utcoffset() == timedelta(0)

    win = client.post("/api/data/values", json={"cpId": "CP-1", "from": "2025-10-01", "to": "2026-03-31",
                                                "keys": ["EBITDA"]}, headers=h()).json()
    assert [p["asOfDate"] for p in win["points"]] == ["2025-12-31", "2026-03-31"]

    empty = client.post("/api/data/values", json={"cpId": "CP-2", "to": "2026-03-31", "keys": ["EBITDA"]},
                        headers=h()).json()
    assert empty == {"cpId": "CP-2", "points": [], "missingKeys": ["EBITDA"], "sourceChangedAt": None}


def test_values_service_role_and_scope(client, app):
    add_value(app, "CP-1", "EBITDA", 1.0, date(2026, 3, 31))
    assert client.post("/api/data/values", json={"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]},
                       headers=h("core-svc", "SERVICE")).status_code == 200
    assert client.post("/api/data/values", json={"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]},
                       headers=h("rm-other")).status_code == 404


# ---------- job operations ----------

def test_job_operations_for_support(client, app):
    with session(app) as s:
        doc = Document(type="LOAN_AGREEMENT", file_name="agreement.pdf", storage_path="x", sha256="a" * 64,
                       size_bytes=1, uploaded_by="rm-anil")
        s.add(doc)
        s.flush()
        s.add(DocumentCpLink(document_id=doc.id, cp_id="CP-1", linked_by="rm-anil"))
        job = Job(document_id=doc.id, flow="SKILL_PACK", job_type="EXTRACT_AGREEMENT")
        s.add(job)
        s.commit()
        job_id = job.id
    support = h("support-1", "SUPPORT")
    assert client.get("/dps/v1/jobs", headers=h()).status_code == 403
    jobs = client.get("/dps/v1/jobs", headers=support).json()
    assert len(jobs) == 1 and jobs[0]["status"] == "QUEUED"
    assert jobs[0]["fileName"] == "agreement.pdf" and jobs[0]["cpIds"] == ["CP-1"]  # which upload the job reads
    assert client.post(f"/dps/v1/jobs/{job_id}/retry", headers=support).status_code == 409
    with app.state.db.session_factory() as s:
        j = s.get(Job, job_id)
        j.status, j.attempts, j.error = "FAILED", 3, "boom"
        s.commit()
    assert [j["id"] for j in client.get("/dps/v1/jobs?status=failed", headers=support).json()] == [job_id]
    assert client.get("/dps/v1/jobs?status=DONE", headers=support).json() == []
    retried = client.post(f"/dps/v1/jobs/{job_id}/retry", headers=support).json()
    assert (retried["status"], retried["attempts"], retried["error"]) == ("QUEUED", 0, None)
    assert client.post("/dps/v1/jobs/nope/retry", headers=support).status_code == 404
    assert client.post(f"/dps/v1/jobs/{job_id}/retry", headers=h()).status_code == 403

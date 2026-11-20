from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import select
from statement_shim import StatementDeps, StatementProcessingFlow, reviewed_if_clean

from dps import jobs
from dps.db import Base, Database
from dps.integrations.core import CoreClient, CoreMaster, CoreUnavailable
from dps.models import Document, DocumentCpLink, EventOutbox, Job, Run, StatementValue, utcnow
from dps.parse.engine import Page
from dps.settings import Settings
from dps.skills import load
from dps.skills import runner as skills
from dps.worker import Worker, build_pack_runner, statement_deps

GOLDEN = Path(__file__).resolve().parents[2] / "golden-set" / "documents"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'flows.db'}")
    Base.metadata.create_all(db.engine)
    return db


def add_document(database: Database, type_: str = "LOAN_AGREEMENT", name: str = "a.pdf",
                 as_of: date | None = None, cps: tuple[str, ...] = ("CP-1",)) -> str:
    with database.session_factory() as s:
        d = Document(type=type_, file_name=name, storage_path="x", sha256=name, size_bytes=1, uploaded_by="u",
                     as_of_date=as_of)
        s.add(d)
        s.flush()
        for cp in cps:
            s.add(DocumentCpLink(document_id=d.id, cp_id=cp, linked_by="u"))
        s.commit()
        return d.id


class FakeCore:
    def __init__(self, masters=None, down: bool = False) -> None:
        self.masters = masters if masters is not None else [CoreMaster("m1", "Minimum DSCR", "FINANCIAL", "AUTO",
                                                                         ("DSCR",))]
        self.down = down
        self.events: list[dict] = []

    def active_masters(self):
        if self.down:
            raise CoreUnavailable("GET /api/v1/covenants: ConnectError")
        return self.masters

    def validate_formula(self, expression: str) -> list[str]:
        return []

    def send_data_event(self, payload: dict) -> dict:
        self.events.append(payload)
        return {"assessmentsQueued": 1, "duplicate": False}


class FakeParser:
    def __init__(self, pages: list[Page]) -> None:
        self.pages = pages

    def parse(self, data: bytes) -> list[Page]:
        return self.pages


AGREEMENT_PAGES = [
    Page(1, "4.1 Debt Service Coverage Ratio\nThe Borrower shall maintain a DSCR of not less than 1.25 times.\n"
            "4.2 Interest Coverage Ratio\nThe Borrower shall maintain an ICR of not less than 2.00 times.\n",
         "TEXT", 0.9),
]


def pages_model(pages: list[Page]) -> dict:
    return {"format": "PDF", "pages": [{"no": p.no, "text": p.text, "quality": p.quality, "method": "NATIVE",
                                        "sourceMethod": p.method} for p in pages]}


def agreement_result(pages: list[Page], job_type: str = "EXTRACT_AGREEMENT", ctx: dict | None = None) -> dict:
    """EXTRACT_AGREEMENT in the one pipeline (full review WP7): the covenants pack in the skills flow."""
    job = Job(id="j1", document_id="d1", job_type=job_type, tenant_id="default",
              request_json=json.dumps({"context": ctx or {}}))
    return skills.run(load("covenants"), job, pages_model(pages), None, None)


def doc_status(database: Database, doc_id: str) -> str:
    with database.session_factory() as s:
        return s.get(Document, doc_id).status


def test_an_agreement_job_holds_a_clean_agreement_for_a_person():
    """Full review F5: covenant rules are never applied without a person, however clean the read."""
    result = agreement_result(AGREEMENT_PAGES)
    covenants = {f["value"]["metric"]: f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT"}
    assert set(covenants) == {"DSCR", "ICR"}
    assert covenants["DSCR"]["value"]["threshold"] == 1.25 and covenants["DSCR"]["source"]["page"] == 1
    assert all(f["status"] == "NEEDS_REVIEW" for f in covenants.values())
    assert result["status"] == "NEEDS_REVIEW" and result["pipeline"]["skillPack"].startswith("covenants/")
    assert covenants["DSCR"]["value"]["scope"]["obligor"] == "Borrower"  # WP6 on the agreement job too


def test_an_agreement_job_with_no_text_goes_to_a_person():
    result = agreement_result([Page(1, "", "NONE", 0.0)])
    assert result["status"] == "NEEDS_REVIEW" and "NOTHING_FOUND" in {w["code"] for w in result["warnings"]}


# ---------- Flow B ----------

def xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "BS"
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def statement(database: Database, data: bytes, name: str, heavy: bool = True, pages=None, cps=("CP-1",),
              as_of=date(2026, 3, 31), reviewed: bool = True, derived: bool = False):
    doc_id = add_document(database, "FINANCIAL_STATEMENT", name, as_of, cps)
    deps = StatementDeps(database.session_factory, lambda d: data, FakeParser(pages or []), "ds-dps", heavy,
                         report_derived_values=derived)  # G22: the figures DPS works out only with the switch on
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    return (reviewed_if_clean(flow) if reviewed else flow), doc_id


def values(database: Database, cp: str = "CP-1", current: bool = True) -> dict[str, StatementValue]:
    with database.session_factory() as s:
        q = select(StatementValue).where(StatementValue.cp_id == cp)
        if current:
            q = q.where(StatementValue.superseded.is_(False))
        return {v.key: v for v in s.scalars(q)}


GOOD_ROWS = [("Amounts in INR lakhs", None), ("EBITDA", 500), ("Short-term borrowings", 300),
             ("Long-term borrowings", 700), ("Total equity", 2000), ("Cash and cash equivalents", 100)]


def test_a_statement_stores_the_approved_printed_figures_only(database):
    """G21: nothing is stored before a person approves. G22: the figures as printed; Spring works out the rest.
    G9: no event goes to Core from here (the job's dps.extraction.completed tells Spring)."""
    flow, doc_id = statement(database, xlsx_bytes(GOOD_ROWS), "fs.xlsx", cps=("CP-1", "CP-2"))
    assert flow.state.outcome == "REPORTED" and flow.state.unit_scale == "lakh"
    v = values(database)
    assert v["EBITDA"].value == 5.0 and v["EBITDA"].cell == "BS!B2" and values(database, "CP-2")["EBITDA"]
    assert "TOTAL_DEBT" not in v and "NET_DEBT" not in v  # worked out by Spring's formula engine
    with database.session_factory() as s:
        assert s.scalars(select(EventOutbox)).first() is None


def test_the_derived_values_switch_keeps_the_old_figures(database):
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fs.xlsx", date(2026, 3, 31))
    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(GOOD_ROWS), FakeParser([]), "ds-dps", True,
                         report_derived_values=True)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    reviewed_if_clean(flow)
    v = values(database)
    assert v["TOTAL_DEBT"].derived and v["TOTAL_DEBT"].value == 10.0 and v["NET_DEBT"].value == 9.0


def test_flow_b_new_upload_supersedes_and_rerun_is_idempotent(database):
    statement(database, xlsx_bytes(GOOD_ROWS), "q1.xlsx")
    newer = [r if r[0] != "EBITDA" else ("EBITDA", 600) for r in GOOD_ROWS]
    flow, doc_id = statement(database, xlsx_bytes(newer), "q1-restated.xlsx")
    assert values(database)["EBITDA"].value == 6.0
    assert len([v for v in values(database, current=False).values()]) >= 1
    # Re-running the same document replaces its own rows instead of superseding them again.
    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(newer), FakeParser([]), "ds-dps", True)
    StatementProcessingFlow(deps).kickoff(inputs={"job_id": "j2", "document_id": doc_id})
    with database.session_factory() as s:
        rows = s.scalars(select(StatementValue).where(StatementValue.key == "EBITDA")).all()
    assert sorted((r.value, r.superseded) for r in rows) == [(5.0, True), (6.0, False)]


def test_flow_b_csv_needs_review_then_reports_on_resume(database):
    csv_data = b"Line item,FY26,FY25\nEBITDA,120,100\nNet worth,900,800\n"
    flow, doc_id = statement(database, csv_data, "fs.csv")
    assert flow.state.outcome == "NEEDS_REVIEW"
    # No unit is stated, so the crore assumption is flagged for the reviewer too.
    assert flow.state.issues == ["MISSING_LINE_ITEMS: TOTAL_DEBT", "UNIT_NOT_STATED: amounts read as INR crore"]
    assert doc_status(database, doc_id) == "NEEDS_REVIEW" and values(database) == {}

    deps = StatementDeps(database.session_factory, lambda d: csv_data, FakeParser([]), "ds-dps", True)
    resumed = StatementProcessingFlow(deps)
    resumed.kickoff(inputs={"id": flow.state.id, "job_id": "j1", "document_id": doc_id})
    assert resumed.state.outcome == "REPORTED" and values(database)["EBITDA"].value == 120


def test_the_approved_figures_keep_the_reviewers_corrections(database):
    """Bug hunt (8 November 2026): approving re-stored the figures as read, deleting the reviewer's correction."""
    from statement_shim import as_result, publish

    csv_data = b"Line item,FY26,FY25\nEBITDA,120,100\nNet worth,900,800\n"
    flow, doc_id = statement(database, csv_data, "fs.csv")
    assert flow.state.outcome == "NEEDS_REVIEW"
    fields, warnings, extra = as_result(flow.state, date(2026, 3, 31))
    for f in fields:
        f["status"] = "CONFIRMED"
        if f["name"] == "EBITDA":
            f["value"]["amount"], f["status"] = 125.0, "CORRECTED"
    with database.session_factory() as s:
        n = publish(s, Job(id="j1", document_id=doc_id, job_type="EXTRACT_STATEMENT"),
                    {"fields": fields, "warnings": warnings, **extra}, actor="checker-1")
        s.commit()
    now = values(database)
    assert n == 2 and (now["EBITDA"].value, now["EBITDA"].source_type) == (125.0, "USER")
    assert now["EBITDA"].entered_by == "checker-1" and now["NET_WORTH"].value == 900
    assert doc_status(database, doc_id) == "PUBLISHED"


def test_flow_b_net_interest_waits_once_per_borrower(database):
    """Held-out set (HU01): "Interest expense, net" was published with no flag where the covenant may use gross."""
    csv_data = (b"Amounts in INR crore\nLine item,FY26,FY25\nEBITDA,120,100\nTotal debt,400,350\n"
                b"Net worth,900,800\n\"Interest expense, net\",19,17\n")
    flow, doc_id = statement(database, csv_data, "fs.csv")
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert flow.state.issues == ["INTEREST_IS_NET: 'Interest expense, net'"]

    deps = StatementDeps(database.session_factory, lambda d: csv_data, FakeParser([]), "ds-dps", True)
    resumed = StatementProcessingFlow(deps)
    resumed.kickoff(inputs={"id": flow.state.id, "job_id": "j1", "document_id": doc_id})
    assert resumed.state.outcome == "REPORTED" and values(database)["INTEREST_PAID"].value == 19

    following, _ = statement(database, csv_data, "fs-2027.csv", as_of=date(2027, 3, 31))
    assert following.state.outcome == "REPORTED" and following.state.issues == []


def test_flow_b_pdf_and_unsupported_types(database):
    pages = [Page(1, "Balance sheet (Rs. in crores)\nEBITDA ....... 50.5\nTotal debt 120\nNet worth 300\n",
                  "TEXT", 0.9)]
    flow, _ = statement(database, b"pdf", "fs.pdf", pages=pages)
    assert flow.state.outcome == "REPORTED" and values(database)["TOTAL_DEBT"].page == 1

    scan, _ = statement(database, b"pdf", "scan.pdf", heavy=False, pages=[Page(1, "", "NONE", 0.0)])
    assert scan.state.outcome == "HEAVY"

    xls, doc_id = statement(database, b"x", "old.xls")
    assert xls.state.outcome == "FAILED" and xls.state.error.startswith("Unreadable .xls statement")
    bad, _ = statement(database, b"not a workbook", "bad.xlsx")
    assert bad.state.outcome == "FAILED" and bad.state.error == "Unreadable statement: BadZipFile"
    empty, _ = statement(database, b"a,b\n", "empty.csv")
    assert empty.state.outcome == "FAILED" and empty.state.error == "No line items found in the statement"


# ---------- worker and queue ----------

class Ingestor:
    """A worker always has an ingest step in CTS; these documents have no DMS key, so it reads nothing here."""


def settings(tmp_path: Path, **kw) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'flows.db'}", storage_dir=tmp_path / "store",
                    core_data_source_id="ds-dps", worker_max_attempts=2, worker_retry_base_seconds=0, **kw)


def add_job(database: Database, doc_id: str, flow: str = "SKILL_PACK", pool: str = "STANDARD",
            job_type: str = "EXTRACT_AGREEMENT") -> str:
    with database.session_factory() as s:
        j = Job(document_id=doc_id, flow=flow, pool=pool, job_type=job_type)
        s.add(j)
        s.commit()
        return j.id


def job(database: Database, job_id: str) -> Job:
    with database.session_factory() as s:
        return s.get(Job, job_id)


def worker(tmp_path: Path, database: Database, runner, **kw) -> Worker:
    return Worker(settings(tmp_path, **kw), database.session_factory, FakeCore(), {}, Ingestor(),
                  pack_runner=runner)


def agreement_runner(db, j, model, raw):
    skills.finish(db, j, skills.run(load("covenants"), j, pages_model(AGREEMENT_PAGES), None, None, db))


def test_the_worker_runs_an_agreement_job_in_the_skills_flow(database, tmp_path):
    doc_id = add_document(database)
    job_id = add_job(database, doc_id)
    w = worker(tmp_path, database, agreement_runner)
    assert w.run_once() is True
    j = job(database, job_id)
    assert j.status == jobs.NEEDS_REVIEW and j.locked_by is None  # a person decides (F5)
    with database.session_factory() as s:
        run = s.scalars(select(Run).where(Run.job_id == job_id)).one()
    ledger = json.loads(run.result_json)["document"]["coverage"]["ledger"]  # every duty sentence accounted for
    assert ledger["scope"] == "WHOLE_DOCUMENT" and ledger["accounted"] == ledger["candidates"]
    assert w.run_once() is False


def test_jobs_queued_under_the_old_flow_names_run_in_the_skills_flow(database, tmp_path):
    """Jobs queued before the one pipeline (WP7) still run: AGREEMENT_INTAKE and STATEMENT_PROCESSING."""
    job_id = add_job(database, add_document(database), flow="AGREEMENT_INTAKE")
    assert worker(tmp_path, database, agreement_runner).run_once()
    assert job(database, job_id).status == jobs.NEEDS_REVIEW


def test_worker_retries_transient_errors_then_fails(database, tmp_path):
    job_id = add_job(database, add_document(database))

    def down(db, j, model, raw):
        raise CoreUnavailable("GET /api/v1/covenants: ConnectError")

    w = worker(tmp_path, database, down)
    w.run_once()
    assert job(database, job_id).status == jobs.RETRY_WAIT
    w.run_once()
    j = job(database, job_id)
    assert j.status == jobs.DEAD and "ConnectError" in j.error and j.error_code == "SPRING_UNAVAILABLE"


def test_worker_fails_unknown_flow_and_crashing_runner(database, tmp_path):
    a = add_job(database, add_document(database), flow="NOPE")
    b = add_job(database, add_document(database, name="b.pdf"))

    def boom(db, j, model, raw):
        raise ValueError("bug")

    w = worker(tmp_path, database, boom)
    for _ in range(3):  # a, then b crashes, then b is retried once and crashes again
        w.run_once()
    assert job(database, a).error == "No flow registered for NOPE"
    assert job(database, b).status == jobs.DEAD and job(database, b).error == "ValueError: bug"
    assert job(database, b).attempts == 2 and job(database, b).error_code == "INTERNAL_ERROR"


def test_worker_moves_statements_to_the_heavy_pool_and_fails_empty_ones(database, tmp_path):
    from statement_shim import StatementDeps as Deps

    heavy = add_job(database, add_document(database, "FINANCIAL_STATEMENT", "scan.pdf", date(2026, 3, 31)),
                    job_type="EXTRACT_STATEMENT")
    empty = add_job(database, add_document(database, "FINANCIAL_STATEMENT", "empty.csv", date(2026, 3, 31)),
                    job_type="EXTRACT_STATEMENT")
    files = {"scan.pdf": b"pdf", "empty.csv": b"a,b\n"}
    deps = Deps(database.session_factory, lambda d: files[d.file_name], FakeParser([Page(1, "", "NONE", 0.0)]),
                "ds-dps", False)

    def runner(db, j, model, raw):
        skills.finish(db, j, skills.run_job(j, model, None, None, db, "1", raw, statement_deps=deps))

    w = worker(tmp_path, database, runner)
    w.run_once()
    w.run_once()
    assert (job(database, heavy).pool, job(database, heavy).status, job(database, heavy).attempts) == (
        "HEAVY", jobs.QUEUED, 0)
    assert job(database, empty).status == jobs.FAILED and job(database, empty).error_code == "CONTENT_FAILED"
    assert w.run_once() is False  # a STANDARD worker does not take HEAVY jobs


def test_claim_takes_expired_leases_and_respects_attempts(database):
    doc_id = add_document(database)
    job_id = add_job(database, doc_id)
    with database.session_factory() as s:
        first = jobs.claim(s, "w1", "STANDARD", 60, 3)
        assert first.id == job_id and first.locked_by == "w1" and not jobs.lease_expired(first)
        assert jobs.claim(s, "w2", "STANDARD", 60, 3) is None
        first.locked_until = utcnow() - timedelta(seconds=1)
        s.commit()
        assert jobs.lease_expired(first)
        second = jobs.claim(s, "w2", "STANDARD", 60, 3)
        assert second.locked_by == "w2" and second.attempts == 2
        assert jobs.claim(s, "w3", "STANDARD", 60, 2) is None
    assert jobs.lease_expired(Job(document_id="x", flow="f"))


@pytest.mark.skipif(not GOLDEN.exists(), reason="golden set not present")
def test_the_pack_runner_wires_the_statement_reader(database, tmp_path):
    from dps.extraction.statement_pdf import PdfStatement  # noqa: F401 - the reader the deps point at

    core = CoreClient("http://core", 1.0, "")
    deps = statement_deps(settings(tmp_path, ocr_enabled=False), database, core)
    assert deps.pdf_reader is not None and deps.heavy_pool is False and deps.report_derived_values is False
    assert deps.loan_currency == core.cp_currency and deps.fx_rate == core.fx_rate
    assert callable(build_pack_runner(settings(tmp_path, ocr_enabled=False), database, core))


@pytest.mark.skipif(not GOLDEN.exists(), reason="golden set not present")
def test_an_agreement_job_holds_a_covenant_the_rules_cannot_read(tmp_path):
    from dps.parse.engine import ParserEngine

    # AGR-001 has an end-use covenant ("2.1 The proceeds of the Facility shall be used exclusively for ...") the rules
    # do not read: the whole-document ledger holds it for a person instead of leaving it out.
    pages = ParserEngine(None).parse((GOLDEN / "AGR-001.pdf").read_bytes())
    result = agreement_result(pages)
    held = [f for f in result["fields"] if any(i["code"] == "UNRESOLVED_CANDIDATE" for i in f["issues"])]
    assert [f["source"]["page"] for f in held] == [2] and "proceeds of the Facility" in held[0]["source"]["quote"]


# ---------- Core client ----------

def core_client(handler) -> CoreClient:
    return CoreClient("http://core", 1.0, "", client=httpx.Client(base_url="http://core",
                                                                   transport=httpx.MockTransport(handler)))


def test_core_client_pages_masters_and_uses_service_identity():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        content = [{"id": f"m{page}", "name": f"N{page}", "category": "FINANCIAL", "assessmentType": "AUTO",
                    "referencedMeasures": ["DSCR"], "custom": False},
                   {"id": "c", "name": "custom", "custom": True}]
        return httpx.Response(200, json={"content": content, "last": page == 1})

    masters = core_client(handler).active_masters()
    assert [m.id for m in masters] == ["m0", "m1"] and masters[0].referenced_measures == ("DSCR",)
    assert seen[0].headers["X-Roles"] == "SERVICE"
    bearer = CoreClient("http://core", 1.0, "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bearer._headers == {"Authorization": "Bearer tok"}


def test_core_client_validate_and_events_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/formulas/validate":
            body = json.loads(request.content)
            if body["expression"] == "OK":
                return httpx.Response(200, json={"valid": True, "errors": []})
            if body["expression"] == "EMPTY":
                return httpx.Response(200, json={"valid": False, "errors": []})
            return httpx.Response(200, json={"valid": False, "errors": [{"code": "UNKNOWN_MEASURE"}]})
        if request.url.path == "/api/v1/data-events":
            return httpx.Response(202, json={"assessmentsQueued": 2, "duplicate": False})
        return httpx.Response(503)

    c = core_client(handler)
    assert c.validate_formula("OK") == []
    assert c.validate_formula("X") == ["UNKNOWN_MEASURE"]
    assert c.validate_formula("EMPTY") == ["invalid"]
    assert c.send_data_event({"a": 1}) == {"assessmentsQueued": 2, "duplicate": False}
    with pytest.raises(CoreUnavailable):
        c.active_masters()

    def offline(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(CoreUnavailable):
        core_client(offline).validate_formula("OK")


def test_renew_lease_extends_only_the_holders_lock(database):
    job_id = add_job(database, add_document(database))
    with database.session_factory() as s:
        claimed = jobs.claim(s, "w1", "STANDARD", 5, 3)
        first_until = jobs._aware(claimed.locked_until)
        assert jobs.renew_lease(s, job_id, "w1", 600)
        s.refresh(claimed)
        assert jobs._aware(claimed.locked_until) > first_until
        assert not jobs.renew_lease(s, job_id, "w2", 600)


def test_worker_heartbeat_keeps_a_long_job_from_being_taken_over(database, tmp_path):
    import time as _time

    job_id = add_job(database, add_document(database))
    seen = {}

    def slow(db, j, model, raw):
        _time.sleep(2.4)
        with database.session_factory() as s:
            seen["until"] = jobs._aware(s.get(Job, j.id).locked_until)
            seen["other"] = jobs.claim(s, "w-other", "STANDARD", 3, 3)
        jobs.finish(db, j, jobs.DONE)

    w = worker(tmp_path, database, slow, worker_lease_seconds=3)
    started = utcnow()
    assert w.run_once() is True
    assert seen["other"] is None, "a second worker must not claim a job whose lease is being renewed"
    assert seen["until"] > started + timedelta(seconds=3)
    assert job(database, job_id).status == jobs.DONE

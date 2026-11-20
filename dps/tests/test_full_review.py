"""Full review, 19 Nov 2026 (docs/cts-dps/full-review): one test per confirmed defect, so each fix stays fixed.

The IDs (F1 ... G34) are the finding IDs in FIX-PLAN.md. Every test states the behaviour the review asked for.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from test_flows import add_document, agreement_result, database  # noqa: F401 - fixture
from test_phase5_review import SERVICE, _run_certificate

from dps.models import Run
from dps.parse.engine import Page

SPRINGING = [Page(1, "7.1 Fixed Charge Coverage Ratio\nIf Excess Availability is less than USD 10,000,000 on any day, "
                     "the Borrower shall maintain a Fixed Charge Coverage Ratio of at least 1.10:1.\n", "TEXT", 0.95)]
CLEAN = [Page(1, "4.1 Debt Service Coverage Ratio\nThe Borrower shall maintain a DSCR of not less than 1.25 times.\n"
                 "4.2 Interest Coverage Ratio\nThe Borrower shall maintain an ICR of not less than 2.00 times.\n",
              "TEXT", 0.95)]


# ---------- WP1: nothing is applied without a person ----------

def test_f5_a_clean_agreement_is_never_published_without_a_person():
    """F5 full fix (WP7): EXTRACT_AGREEMENT runs in the skills flow; there is no publishing path left at all."""
    result = agreement_result(CLEAN)
    covenants = [f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT"]
    assert covenants and all(f["status"] == "NEEDS_REVIEW" for f in covenants)
    assert result["status"] == "NEEDS_REVIEW"


def test_f12_rules_only_says_the_agent_did_not_read():
    from dps.skills import load
    from dps.skills.runner import _extract

    class Job:
        id, correlation_id, job_type, document_version_id, tenant_id = "j", None, "ONBOARD", "v", "default"

    pages = [{"no": 1, "text": CLEAN[0].text, "quality": 0.95, "sourceMethod": "TEXT"}]
    _, warnings, _ = _extract(load("covenants"), Job(), {"pages": pages}, {}, None, None, None, "1")
    assert "AI_NOT_USED" in {w["code"] for w in warnings}


def test_g29_a_blocking_warning_needs_an_acknowledgement(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path, version="cv-g29", key="key-g29-cert")
    with app.state.db.session_factory() as db:
        run = db.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.run_no.desc())).first()
        result = json.loads(run.result_json)
        result.setdefault("warnings", []).append({"code": "ENTITY_MISMATCH", "message": "another borrower"})
        run.result_json = json.dumps(result)
        db.commit()
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    confirm = {"decisions": [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]}
    r = client.post(f"/dps/v1/jobs/{job_id}/review", json=confirm,
                    headers={**SERVICE, "If-Match": str(review["revision"]), "X-User": "checker"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "NEEDS_REVIEW"  # every field confirmed, the mismatch still open
    bad = client.post(f"/dps/v1/jobs/{job_id}/review",
                      json={"decisions": [{"fieldId": "warning:ENTITY_MISMATCH", "action": "ACKNOWLEDGE"}]},
                      headers={**SERVICE, "If-Match": str(r.json()["revision"])})
    assert bad.status_code == 400  # a reason is required
    ok = client.post(f"/dps/v1/jobs/{job_id}/review",
                     json={"decisions": [{"fieldId": "warning:ENTITY_MISMATCH", "action": "ACKNOWLEDGE",
                                          "comment": "group company, same obligor"}]},
                     headers={**SERVICE, "If-Match": str(r.json()["revision"]), "X-User": "checker"})
    assert ok.status_code == 202 and ok.json()["status"] == "DONE"
    final = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert final["document"]["route"] == "REVIEWED"
    warning = next(w for w in final["warnings"] if w["code"] == "ENTITY_MISMATCH")
    assert warning["acknowledged"] == {"by": "checker", "comment": "group company, same obligor"}


def test_g21_a_clean_statement_waits_for_review_before_core_sees_it(database):  # noqa: F811
    from conftest import review_done
    from test_flows import GOOD_ROWS, statement, values, xlsx_bytes

    flow, doc_id = statement(database, xlsx_bytes(GOOD_ROWS), "clean.xlsx", reviewed=False)
    assert flow.state.outcome == "NEEDS_REVIEW" and not flow.state.issues
    assert values(database) == {}  # nothing reached Core
    assert review_done(flow).state.outcome == "REPORTED"
    assert values(database)["EBITDA"].value == 5.0


# ---------- WP2: jobs, queue and events ----------

def _queued_job(database, **kw):  # noqa: F811
    from dps.models import Job, utcnow

    doc = add_document(database)
    with database.session_factory() as s:
        j = Job(document_id=doc, flow="AGREEMENT_INTAKE", job_type="EXTRACT_AGREEMENT", tenant_id="default",
                priority=1, due_at=utcnow(), status="QUEUED", pool="STANDARD", attempts=0, crash_count=0,
                cancel_requested=False, **kw)
        s.add(j)
        s.commit()
        return j.id


def test_g6_a_job_whose_worker_died_on_its_last_attempt_ends_dead(database):  # noqa: F811
    from datetime import timedelta

    from dps import jobs
    from dps.models import EventFeed, Job, utcnow

    job_id = _queued_job(database)
    with database.session_factory() as s:
        for n in range(3):
            j = jobs.claim(s, f"w{n}", "STANDARD", 120, 3, poison_crashes=99)
            assert j is not None
            j.locked_until = utcnow() - timedelta(seconds=1)  # the worker died: its lease ran out
            s.commit()
        assert jobs.claim(s, "w9", "STANDARD", 120, 3, poison_crashes=99) is None
        assert s.get(Job, job_id).status == jobs.RUNNING  # before the fix it stayed here for ever
        assert jobs.reap(s, 3) == 1
        j = s.get(Job, job_id)
        assert (j.status, j.error_code) == (jobs.DEAD, "LEASE_EXPIRED_EXHAUSTED")
        events = [e.type for e in s.scalars(select(EventFeed).where(EventFeed.job_id == job_id))]
        assert events == ["dps.job.dead"]
        assert jobs.reap(s, 3) == 0


def test_g7_a_job_cancelled_while_running_is_not_published(database):  # noqa: F811
    from dps import jobs
    from dps.models import Job

    job_id = _queued_job(database)
    with database.session_factory() as s:
        j = jobs.claim(s, "w1", "STANDARD", 120, 3)
        jobs.request_cancel(s, j, "rm-anil")
        token = jobs.worker_attempt.set((job_id, "w1", j.attempts))
        try:
            jobs.finish(s, j, jobs.DONE)  # the flow ended normally after the cancel
        finally:
            jobs.worker_attempt.reset(token)
        assert (s.get(Job, job_id).status, s.get(Job, job_id).error_code) == (jobs.CANCELLED, "JOB_CANCELLED")


def test_g8_review_and_mapping_are_different_batch_outcomes():
    from dps import jobs
    from dps.models import Job

    assert jobs.item_outcome(Job(status=jobs.NEEDS_REVIEW, error_code=None)) == "PENDING_REVIEW"
    assert jobs.item_outcome(Job(status=jobs.NEEDS_REVIEW, error_code="MAPPING_REVIEW")) == "PENDING_MAPPING"


def test_g24_the_feed_never_passes_a_recent_gap(database):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from dps import feed
    from dps.models import EventFeed

    now = datetime.now(UTC)
    with database.session_factory() as s:
        for seq in (1, 2, 4):  # 3 is still being written by another transaction
            s.add(EventFeed(seq=seq, type="dps.job.accepted", tenant_id="default", payload_json="{}",
                            occurred_at=now))
        s.commit()
        events, cursor = feed.read(s, 0, 100, now=now)
        assert [e["seq"] for e in events] == [1, 2] and cursor == 2
        later = now + timedelta(seconds=feed.GAP_SETTLE_SECONDS + 1)  # an old gap is a rollback: read past it
        events, cursor = feed.read(s, 2, 100, now=later)
        assert [e["seq"] for e in events] == [4] and cursor == 4


def test_g16_each_worker_takes_its_share_of_the_deployment_rate():
    from dps.agent.gateway import Routes

    assert Routes.load(None, "gpt", share=4).routes["strong"].rpm == 75
    assert Routes.load(None, "gpt").routes["strong"].rpm == 300


def test_g5_readiness_reports_the_workers(client):
    body = client.get("/health/ready").json()
    assert body["workers"] in ("UP", "DOWN", "UNKNOWN")


# ---------- WP3: versions and custody ----------

def test_g13_the_pipeline_version_changes_with_what_decides_the_result(tmp_path):
    from dps.settings import Settings
    from dps.versioning import compute

    auto = Settings().pipeline_version
    assert auto.startswith("0.") and "+" in auto  # computed, not the old fixed "1"
    assert Settings(pipeline_version="pinned-7").pipeline_version == "pinned-7"
    routes = tmp_path / "routes.yaml"
    routes.write_text("routes: {strong: {deployment: gpt-a}}", encoding="utf-8")
    first = compute(str(routes), "")
    routes.write_text("routes: {strong: {deployment: gpt-b}}", encoding="utf-8")
    compute.cache_clear()
    assert compute(str(routes), "") != first  # a model-route change is a new version


def test_g17_the_model_answer_cache_is_off_and_expires(tmp_path):
    from datetime import timedelta

    from dps.agent.guard import guarded, purge_cache
    from dps.db import Base, Database
    from dps.models import AiCache, utcnow
    from dps.settings import Settings

    assert Settings().ai_cache is False
    db = Database(f"sqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(db.engine)
    s = Settings(ai_cache=True, ai_cache_days=30)
    calls = []
    call = guarded(lambda m: calls.append(1) or "answer", "gpt", db.session_factory, s)
    msgs = [{"role": "user", "content": "x"}]
    assert call(msgs) == "answer" and call(msgs) == "answer" and len(calls) == 1  # cached
    with db.session_factory() as session:
        for row in session.query(AiCache):
            row.created_at = utcnow() - timedelta(days=31)
        session.commit()
    assert call(msgs) == "answer" and len(calls) == 2  # expired: asked again
    with db.session_factory() as session:
        for row in session.query(AiCache):
            row.created_at = utcnow() - timedelta(days=31)
        session.commit()
        assert purge_cache(session, 30) == 1


def test_g15_g20_file_flags_and_pack_versions_reach_the_result():
    from dps.skills import load
    from dps.skills.runner import run

    class Job:
        id, correlation_id, job_type, document_version_id, tenant_id, sha256 = "j", None, "ONBOARD", "v", "d", "s"
        document_id = "doc"
        request_json, rollout = "{}", "NORMAL"

    pages = [{"no": 1, "text": CLEAN[0].text, "quality": 0.95, "sourceMethod": "TEXT"}]
    result = run(load("covenants"), Job(), {"pages": pages, "flags": ["TRACKED_CHANGES", "IRRELEVANT"]})
    flags = next(w for w in result["warnings"] if w["code"] == "FILE_FLAGS")
    assert "TRACKED_CHANGES" in flags["message"] and "IRRELEVANT" not in flags["message"]
    packs = result["pipeline"]["packs"]
    assert packs["covenants"]["version"] and len(packs["covenants"]["promptSha"]) == 16
    assert "schedule_terms" in packs


# ---------- WP4: finding every covenant (probes P1 and P2 of the review, now tests) ----------

import pytest  # noqa: E402

OBLIGATIONS = [
    "The Borrower shall ensure that its Chief Financial Officer is at all times a person acceptable to the Lender.",
    "The Borrower shall cause the Vessel to remain classed with a member of the International Association of "
    "Classification Societies.",
    "The Borrower shall not change its accounting reference date or its auditors without the consent of the Agent.",
    "The Borrower will procure that the Project achieves Commercial Operation on or before 31 March 2028.",
    "The Borrower shall hedge not less than 50 per cent. of its interest rate exposure under the Facility.",
    "No member of the Group may declare, make or pay any dividend, charge, fee or other distribution.",
    "The Parent shall ensure that no Subsidiary creates or permits to subsist any Security over any of its assets.",
    "Each Restricted Subsidiary shall deliver to the Agent its annual audited accounts within 120 days.",
    "Capital Expenditure in any Financial Year is not to exceed USD 5,000,000.",
    "It is a condition of this Agreement that the Sponsor holds at least 51 per cent. of the voting share capital "
    "of the Borrower.",
    "Leverage: as at each Quarter Date, Total Net Debt to EBITDA of 3.50:1 or lower.",
    "Any Financial Indebtedness of any member of the Group is not paid when due nor within any originally "
    "applicable grace period.",
    "If Excess Availability is less than USD 10,000,000 on any day, the Borrower shall maintain a Fixed Charge "
    "Coverage Ratio of at least 1.10:1.",
    "The aggregate Revolving Loans shall not at any time exceed the lesser of the Commitments and the Borrowing Base.",
    "Eligible Receivables shall exclude any receivable outstanding for more than 90 days after its invoice date.",
]
NOT_OBLIGATIONS = [
    "For example, if the Leverage Ratio were 4.00:1, the Applicable Margin would be 3.25 per cent. per annum.",
    "The Borrower may pay dividends out of Excess Cash Flow so long as no Default is continuing.",
    "The Lender may at any time inspect the books of the Borrower.",
    "\"Leverage Ratio\" means the ratio of Total Net Debt to EBITDA in respect of any Relevant Period.",
]


@pytest.mark.parametrize("sentence", OBLIGATIONS)
def test_f1_every_obligation_shape_is_found_by_the_rules_or_the_ledger(sentence):
    from dps.skills.covenants import ledger
    from dps.skills.covenants.rules import rules

    page = [{"no": 1, "text": sentence}]
    assert ledger.candidates(page) or rules(page, {}), sentence


@pytest.mark.parametrize("sentence", NOT_OBLIGATIONS)
def test_f1_illustrations_permissions_and_definitions_are_not_candidates(sentence):
    from dps.skills.covenants import ledger
    from dps.skills.covenants.rules import rules

    page = [{"no": 1, "text": sentence}]
    assert not ledger.candidates(page) and not rules(page, {})


def _onboard_fields(text):
    from dps.skills.covenants import ledger
    from dps.skills.covenants.rules import rules

    pages = [{"no": 1, "text": text}]
    fields = rules(pages, {})
    entries = ledger.settle(ledger.candidates(pages), fields)
    ledger.attach(entries, fields)
    return fields + ledger.review_items(entries, pages, "covenants"), entries


def test_f2_a_table_of_levels_is_one_covenant_with_its_schedule():
    fields, _ = _onboard_fields(
        "The Borrower shall ensure that Leverage in respect of any Relevant Period ending on a date set out below "
        "does not exceed the ratio set out opposite that date:\n\n31 March 2027 4.00:1\n\n31 March 2028 3.50:1\n\n"
        "31 March 2029 and thereafter 3.00:1")
    [table] = [f for f in fields if any(i["code"] == "STEP_TABLE" for i in f["issues"])]
    assert table["value"]["operator"] == "<="
    assert [s["threshold"] for s in table["value"]["schedule"]] == [4.0, 3.5, 3.0]
    assert table["status"] == "NEEDS_REVIEW"


def test_f3_a_springing_condition_stays_with_its_covenant():
    fields, _ = _onboard_fields(
        "If Excess Availability is less than USD 10,000,000 on any day, the Borrower shall maintain a Fixed Charge "
        "Coverage Ratio of at least 1.10:1.")
    fccr = next(f for f in fields if f["group"] == "FINANCIAL_COVENANT")
    assert fccr["status"] == "NEEDS_REVIEW"
    assert any("Excess Availability" in q for q in fccr["value"].get("qualifiers", []))
    assert "QUALIFIED" in {i["code"] for i in fccr["issues"]}


def test_f7_two_reporting_duties_are_never_one():
    _, entries = _onboard_fields(
        "The Borrower shall deliver its audited annual financial statements within 120 days. The Borrower shall "
        "deliver a copy of each environmental permit renewal within 10 days of receipt.")
    second = next(e for e in entries if "permit" in e["text"])
    assert second["state"] != "FINDING"  # covered and listed on the field, or for review: never silently explained


def test_f8_a_sample_of_dismissed_sentences_goes_to_a_person():
    from dps.skills.covenants import ledger

    pages = [{"no": 1, "text": " ".join(f"The Borrower shall deliver report number {n} to the Lender within {n} days."
                                        for n in range(1, 41))}]
    entries = ledger.candidates(pages)
    for e in entries:
        e["state"], e["reason"] = ledger.NOT_A_COVENANT, "test"
    sampled = [f for f in ledger.review_items(entries, pages, "covenants")
               if any(i["code"] == "DISMISSED_SAMPLE" for i in f["issues"])]
    assert 1 <= len(sampled) < len(entries)


def test_f11_a_long_definition_is_read_to_its_end():
    from dps.agent.document_agent import FindDefinitionTool, ToolContext

    body = "\"EBITDA\" means operating profit " + "plus an add-back, " * 120 + "less taxes.\n\"Next Term\" means x."
    tool = FindDefinitionTool(ToolContext([{"no": 1, "text": body}], None, None, None, 5))
    out = tool._run("EBITDA")
    assert "less taxes" in out and "Next Term" not in out


def test_f14_a_chief_financial_officer_is_not_a_compliance_certificate():
    from dps.skills.covenants.rules import rules

    fields = rules([{"no": 1, "text": OBLIGATIONS[0]}], {})
    assert "COMPLIANCE_CERTIFICATE" not in {f["name"] for f in fields}


def test_g26_g32_prompt_has_pages_first_and_one_item_per_duty():
    from dps.skills import load
    from dps.skills.common import prompt_for

    pack = load("covenants")
    assert "One item per duty" in pack.prompt
    content = prompt_for(pack, [{"no": 3, "text": "page text"}])[0]["content"]
    assert content.index("<<<page 3>>>") < content.index("Task:")


# ---------- WP5: reading Word, Excel and mixed PDFs ----------

def _docx_bytes():
    import io

    import docx
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    d = docx.Document()
    d.add_heading("1. Definitions", level=1)
    for i in range(60):
        d.add_paragraph(f"{i}.1 Notices under this Agreement shall be in writing and in English. " * 3)
    d.add_heading("21. Financial Covenants", level=1)
    d.add_paragraph("21.1 The Borrower shall ensure that the Leverage Ratio does not exceed 3.25:1 on each Test Date.")
    d.element.body.append(parse_xml(
        f'<w:sdt {w}><w:sdtContent><w:p><w:r><w:t>21.2 The Borrower shall maintain Minimum Liquidity of not less '
        f'than USD 25,000,000 at all times.</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
    d.element.body.append(parse_xml(
        f'<w:p {w}><w:del w:id="1" w:author="x" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>21.3 The Borrower '
        f'shall maintain a DSCR of not less than 1.30:1.</w:delText></w:r></w:del></w:p>'))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_f6_a_long_word_file_is_read_in_logical_pages_with_form_fields_and_revisions():
    from dps.agent.document_agent import PAGE_LIMIT
    from dps.parse.page_model import DOCX_PAGE_CHARS, build_docx
    from dps.skills.common import fit_pages

    model = build_docx(_docx_bytes(), "s", None, [])
    assert model["pageCount"] > 1
    assert all(len(p["text"]) <= DOCX_PAGE_CHARS + 600 for p in model["pages"])  # every page fits the agent
    assert all(len(p["text"]) < PAGE_LIMIT for p in model["pages"])
    text = "\n".join(p["text"] for p in model["pages"])
    assert "3.25:1" in text and "25,000,000" in text  # the content-control covenant is read
    assert "1.30:1" not in text  # a deleted covenant is not read as text ...
    deleted = [b["deleted"] for p in model["pages"] for b in p["blocks"] if b.get("deleted")]
    assert deleted and "1.30:1" in deleted[0]  # ... but kept for the reviewer
    assert {"TRACKED_CHANGES", "CONTENT_CONTROLS"} <= set(model["flags"])
    covenant_page = next(p for p in model["pages"] if "3.25:1" in p["text"])
    assert covenant_page["blocks"][0]["kind"] == "HEADING"  # a heading starts a page
    assert any(b["ref"].startswith("docx:para:") for b in covenant_page["blocks"])
    _, left_out = fit_pages([{"no": covenant_page["no"], "text": covenant_page["text"]}])
    assert left_out == []


def test_f13_excel_formulas_without_values_and_hidden_rows_are_flagged():
    import io

    from openpyxl import Workbook

    from dps.parse.page_model import build_xlsx

    wb = Workbook()
    ws = wb.active
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "EBITDA", 100, "Total", "=B1*2"
    ws.row_dimensions[3].hidden = True
    ws["A3"] = "hidden line"
    buf = io.BytesIO()
    wb.save(buf)  # openpyxl saves formulas without cached values
    model = build_xlsx(buf.getvalue(), "s", None, [])
    assert {"NO_CACHED_VALUE", "HIDDEN_ROWS_OR_COLUMNS"} <= set(model["flags"])


def test_f9_a_text_page_mostly_covered_by_a_picture_is_also_ocr_read():
    from dps.parse.engine import Page
    from dps.parse.page_model import _read_picture_text

    class Ocr:
        def ocr_page(self, data, index):
            return "Header line\nThe Borrower shall maintain a DSCR of not less than 1.40x.", 0.9

    class Engine:
        _ocr = Ocr()

    model = {"flags": []}
    pages = [Page(1, "Header line\nSchedule 4 (as scanned)", "TEXT", 0.9), Page(2, "plain text page", "TEXT", 0.9)]
    out = _read_picture_text(b"", pages, {1: {"imageShare": 0.7}, 2: {"imageShare": 0.05}}, Engine(), model)
    assert "1.40x" in out[0].text and out[0].text.count("Header line") == 1
    assert out[1].text == "plain text page" and model["flags"] == ["IMAGE_REGIONS"]


# ---------- WP7 / WP8: one product; sign-in and tenant safety ----------

def test_g12_the_old_upload_routes_do_not_exist(settings):
    from fastapi.testclient import TestClient

    from dps.main import create_app

    off = TestClient(create_app(settings, create_schema=True))
    assert off.post("/dps/v1/documents", headers={"X-User": "rm", "X-Roles": "RM"}).status_code == 404
    assert off.get("/health").status_code == 200 and off.get("/health/ready").status_code == 200


def test_g11_service_token_sign_in(settings):
    import hashlib

    from fastapi.testclient import TestClient

    from dps.main import create_app
    from dps.settings import AuthMode

    token = "s3cret-from-the-vault"
    cfg = settings.model_copy(update={"auth_mode": AuthMode.SERVICE_TOKEN,
                                      "service_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                      "service_allowed_networks": "not-a-network,127.0.0.1/32"})
    client = TestClient(create_app(cfg, create_schema=True))
    assert client.get("/dps/v1/capabilities").status_code == 401  # no token
    bad = client.get("/dps/v1/capabilities", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    # TestClient's peer is "testclient", not an address: outside every network, so refused even with the token
    assert client.get("/dps/v1/capabilities", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    from dps.api.auth import Authenticator

    class Req:
        headers = {"Authorization": f"Bearer {token}", "X-User": "rm-anil"}

        class client:  # noqa: N801
            host = "127.0.0.1"

    principal = Authenticator(cfg).authenticate(Req())
    assert principal.user == "rm-anil" and principal.roles == {"SERVICE"}


def test_g2_another_tenants_job_does_not_exist_here(client, app, settings, tmp_path):
    from test_phase5_review import SERVICE, _run_certificate

    from dps.models import Job

    job_id = _run_certificate(client, app, settings, tmp_path, version="cv-g2", key="key-g2-tenant")
    assert client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).status_code == 200
    with app.state.db.session_factory() as db:
        db.get(Job, job_id).tenant_id = "other-bank"
        db.commit()
    assert client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).status_code == 404
    assert client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).status_code == 404
    assert client.get("/dps/v1/versions/cv-g2/runs", headers=SERVICE).status_code == 404


def test_cts_without_legacy_routes_still_has_job_status_and_retry(settings, tmp_path):
    """Found in the local end-to-end run: status and retry lived only in the old routes."""
    from fastapi.testclient import TestClient
    from test_phase2_ingest import text_pdf
    from test_phase4_skills import CERTIFICATE, make_worker, submit

    from dps.main import create_app

    app = create_app(settings.model_copy(update={"legacy_api": False}), create_schema=True)
    client = TestClient(app)
    r = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", "cv-st",
               "key-status-1")
    job_id = r.json()["jobId"]
    service = {"X-User": "cts-spring", "X-Roles": "SERVICE"}
    assert client.get(f"/dps/v1/jobs/{job_id}", headers=service).json()["status"] == "QUEUED"
    assert make_worker(app, settings, tmp_path).run_once()
    assert client.get(f"/dps/v1/jobs/{job_id}", headers=service).json()["status"] in ("DONE", "NEEDS_REVIEW")
    support = {"X-User": "ops-1", "X-Roles": "SUPPORT"}
    assert client.post(f"/dps/v1/jobs/{job_id}/retry", headers=support).status_code == 409  # not failed

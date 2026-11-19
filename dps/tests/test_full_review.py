"""Full review, 19 Nov 2026 (docs/cts-dps/full-review): one test per confirmed defect, so each fix stays fixed.

The IDs (F1 ... G34) are the finding IDs in FIX-PLAN.md. Every test states the behaviour the review asked for.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from test_flows import add_document, database, doc_status, intake, terms  # noqa: F401 - fixture
from test_phase5_review import SERVICE, _run_certificate

from dps.models import Run
from dps.parse.engine import Page

SPRINGING = [Page(1, "7.1 Fixed Charge Coverage Ratio\nIf Excess Availability is less than USD 10,000,000 on any day, "
                     "the Borrower shall maintain a Fixed Charge Coverage Ratio of at least 1.10:1.\n", "TEXT", 0.95)]
CLEAN = [Page(1, "4.1 Debt Service Coverage Ratio\nThe Borrower shall maintain a DSCR of not less than 1.25 times.\n"
                 "4.2 Interest Coverage Ratio\nThe Borrower shall maintain an ICR of not less than 2.00 times.\n",
              "TEXT", 0.95)]


# ---------- WP1: nothing is applied without a person ----------

def test_f5_a_clean_agreement_is_never_published_without_a_person(database):  # noqa: F811
    doc_id = add_document(database)
    flow = intake(database, CLEAN)
    flow.kickoff(inputs={"job_id": "j-f5", "document_id": doc_id})
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert doc_status(database, doc_id) == "NEEDS_REVIEW"
    stored = terms(database, doc_id)
    assert stored and all(t.status != "PUBLISHED" for t in stored.values())


def test_f5_a_dps_formula_is_marked_as_a_template(database):  # noqa: F811
    doc_id = add_document(database)
    intake(database, SPRINGING).kickoff(inputs={"job_id": "j-f5b", "document_id": doc_id})
    [term] = terms(database, doc_id).values()
    assert "FORMULA_TEMPLATE" in term.issues_json


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

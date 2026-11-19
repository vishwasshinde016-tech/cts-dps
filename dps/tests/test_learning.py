"""Phase 4 of "never miss silently": the learning loop (weekly picture, suggestions, learned wordings, test cases)."""

from __future__ import annotations

import json

import pytest
from conftest import h
from sqlalchemy import select
from test_api import session

from dps import learning
from dps.extraction.agreement import extract_terms, set_learned_covenants
from dps.extraction.statement import match_key, set_learned_lines
from dps.models import AuditEvent, Document, ExtractedTerm, LearnedWording
from dps.parse.engine import Page
from dps.services import remember_label

ADMIN = h("ops-admin", "ADMIN")
SUPPORT = h("ops-sue", "SUPPORT")
WORDING = "Borrowings repayable after one year"


@pytest.fixture(autouse=True)
def _forget():
    yield
    set_learned_lines({})
    set_learned_covenants({})


def seed(app) -> None:
    with session(app) as s:
        agreement = Document(type="LOAN_AGREEMENT", file_name="a.pdf", storage_path="x", sha256="a", size_bytes=1,
                             uploaded_by="u", status="PUBLISHED")
        st = Document(type="FINANCIAL_STATEMENT", file_name="s.pdf", storage_path="y", sha256="b", size_bytes=1,
                      uploaded_by="u", status="NEEDS_REVIEW",
                      review_json=json.dumps({"issues": ["SECOND_READER_DIFFERS: EBITDA", "SCALE_JUMP: x",
                                                         "BALANCE_SHEET_DOES_NOT_BALANCE: y",
                                                         "MISSING_LINE_ITEMS: CFO"],
                                              "likely": [{"key": "CFO"}], "confirmed": ["EBITDA", "PAT"],
                                              "remembered": ["LT_DEBT"]}))
        s.add_all([agreement, st])
        s.flush()
        s.add_all([
            ExtractedTerm(document_id=agreement.id, metric_raw="POSSIBLE_COVENANT", name="Debt Yield",
                          category="CANDIDATE", status="PUBLISHED", reviewed_by="ap-raj", page=7,
                          quote="Debt Yield shall not be less than 9.00 per cent."),
            ExtractedTerm(document_id=agreement.id, metric_raw="POSSIBLE_COVENANT", category="CANDIDATE",
                          status="REJECTED", reviewed_by="ap-raj", page=8, quote="Yes: ____ No: ____"),
            ExtractedTerm(document_id=agreement.id, metric_raw="POSSIBLE_COVENANT", category="CANDIDATE",
                          status="PROPOSED", page=9, quote="open one"),
            ExtractedTerm(document_id=agreement.id, metric_raw="DSCR", category="FINANCIAL", status="PUBLISHED",
                          reviewed_by="ap-raj", method="REGEX+AI", operator=">=", threshold=1.25, page=3, quote="q"),
            ExtractedTerm(document_id=agreement.id, metric_raw="LEVERAGE", category="FINANCIAL", status="REJECTED",
                          reviewed_by="ap-raj", issues_json=json.dumps(["AI_READ_DIFFERENT: <= 3.5 on page 4"]),
                          page=4, quote="q2"),
            ExtractedTerm(document_id=agreement.id, metric_raw="NET_WORTH", category="FINANCIAL", method="AI",
                          issues_json=json.dumps(["AI_ONLY"]), page=5, quote="q3"),
        ])
        remember_label(s, "CP-1", f"(ii) {WORDING} (refer note 12) 300.0", "LT_DEBT", st.id, "rm-anil")
        remember_label(s, "CP-2", f"{WORDING} 120.0", "LT_DEBT", None, "rm-bea")
        remember_label(s, "CP-1", "Long-term borrowings 50.0", "LT_DEBT", None, "rm-anil")  # already known
        s.commit()


def test_the_weekly_picture_counts_what_dps_did_not_understand(client, app):
    seed(app)
    assert client.get("/dps/v1/learning", headers=h()).status_code == 403  # an RM does not see it
    body = client.get("/dps/v1/learning", headers=SUPPORT).json()
    [week] = body["weeks"]
    assert {k: week[k] for k in ("agreements", "statements", "possibleCovenants", "possibleApproved",
                                 "possibleRejected", "possibleOpen", "aiReadDifferent", "aiOnly",
                                 "covenantsConfirmedByBoth", "secondReaderDiffers", "accountingChecks", "unitChecks",
                                 "missingLines", "likelyLines", "confirmedByBoth", "remembered", "wordingsLearned",
                                 "notUnderstood")} == {
        "agreements": 1, "statements": 1, "possibleCovenants": 3, "possibleApproved": 1, "possibleRejected": 1,
        "possibleOpen": 1, "aiReadDifferent": 1, "aiOnly": 1, "covenantsConfirmedByBoth": 1,
        "secondReaderDiffers": 1, "accountingChecks": 1, "unitChecks": 1, "missingLines": 1, "likelyLines": 1,
        "confirmedByBoth": 2, "remembered": 1, "wordingsLearned": 3, "notUnderstood": 5}

    sug = body["suggestions"]
    [line] = sug["statementLines"]  # "Long-term borrowings" is already in the catalog
    assert (line["phrase"], line["key"], line["cps"], line["confirmedBy"]) == (WORDING, "LT_DEBT", 2,
                                                                                ["rm-anil", "rm-bea"])
    [cov] = sug["covenants"]
    assert (cov["name"], cov["page"], cov["approvedBy"]) == ("Debt Yield", 7, "ap-raj")
    assert [(r["metric"], r["precision"]) for r in sug["rules"]] == [("LEVERAGE", 0.0), ("DSCR", 1.0)]
    assert body["learned"] == []


def test_an_admin_promotes_a_wording_and_new_statements_use_it(client, app):
    seed(app)
    req = {"kind": "STATEMENT_LINE", "phrase": WORDING, "key": "LT_DEBT", "evidence": 2}
    assert client.post("/dps/v1/learning/wordings", headers=SUPPORT, json=req).status_code == 403
    r = client.post("/dps/v1/learning/wordings", headers=ADMIN, json=req)
    assert r.status_code == 201 and r.json()["active"] and r.json()["addedBy"] == "ops-admin"
    assert client.post("/dps/v1/learning/wordings", headers=ADMIN, json=req).json()["code"] == "ALREADY_LEARNED"

    assert match_key(WORDING) is None
    with session(app) as s:
        learning.apply(s)
    assert match_key(f"{WORDING} 280.0") == "LT_DEBT"
    body = client.get("/dps/v1/learning", headers=SUPPORT).json()
    assert body["suggestions"]["statementLines"] == []  # learned, so no longer suggested
    assert [w["phrase"] for w in body["learned"]] == [WORDING]

    # Retire: kept on record, no longer used.
    r = client.delete(f"/dps/v1/learning/wordings/{body['learned'][0]['id']}", headers=ADMIN)
    assert r.status_code == 200 and not r.json()["active"] and r.json()["retiredBy"] == "ops-admin"
    with session(app) as s:
        learning.apply(s)
        assert sorted(a.action for a in s.scalars(select(AuditEvent).where(
            AuditEvent.target_type == "learned_wording"))) == ["WORDING_LEARNED", "WORDING_RETIRED"]
    assert match_key(WORDING) is None
    assert client.delete("/dps/v1/learning/wordings/nope", headers=ADMIN).status_code == 404

    # Promoting it again brings the same row back.
    assert client.post("/dps/v1/learning/wordings", headers=ADMIN, json=req).status_code == 201
    with session(app) as s:
        assert len(s.scalars(select(LearnedWording)).all()) == 1


def test_a_wording_dps_already_reads_as_another_line_is_refused(client, app):
    def post(**body):
        return client.post("/dps/v1/learning/wordings", headers=ADMIN, json=body)

    r = post(kind="STATEMENT_LINE", phrase="Short-term borrowings", key="LT_DEBT")
    assert (r.status_code, r.json()["code"]) == (409, "CONFLICT")
    assert "ST_DEBT" in r.json()["message"]
    assert post(kind="STATEMENT_LINE", phrase="Some line", key="NOPE").json()["code"] == "UNKNOWN_KEY"
    assert post(kind="COVENANT", phrase="Interest Cover Ratio", key="DSCR").json()["code"] == "CONFLICT"
    assert post(kind="COVENANT", phrase="Debt Yield", key="NOPE").json()["code"] == "UNKNOWN_KEY"
    assert post(kind="OTHER", phrase="Debt Yield", key="DSCR").status_code == 400
    with pytest.raises(learning.LearningError) as ex:
        learning._check("OTHER", "x y z", "DSCR")
    assert ex.value.code == "UNKNOWN_KIND"
    with pytest.raises(learning.LearningError) as ex:
        learning._check("COVENANT", "  ", "DSCR")
    assert ex.value.code == "TOO_SHORT"


def test_a_learned_covenant_name_is_read_in_new_agreements(client, app):
    page = [Page(4, "7.2 Financial Covenants. The Borrower shall ensure that the Debt Yield is not less than 9.00 at "
                    "all times during the term of this Agreement.", "TEXT", 1.0)]
    assert [t for t in extract_terms(page) if t.category == "FINANCIAL"] == []
    assert client.post("/dps/v1/learning/wordings", headers=ADMIN,
                       json={"kind": "COVENANT", "phrase": "Debt Yield", "key": "DSCR"}).status_code == 201
    with session(app) as s:
        learning.apply(s)
    [term] = extract_terms(page)
    assert (term.metric, term.operator, term.threshold) == ("DSCR", ">=", 9.0)


def test_decisions_export_as_test_cases(client, app):
    seed(app)
    client.post("/dps/v1/learning/wordings", headers=ADMIN,
                json={"kind": "STATEMENT_LINE", "phrase": WORDING, "key": "LT_DEBT"})
    r = client.get("/dps/v1/learning/test-cases", headers=SUPPORT)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["statementLines"] == [{"wording": WORDING, "key": "LT_DEBT"},
                                      {"wording": "Long-term borrowings", "key": "LT_DEBT"}]
    assert sorted(c["metric"] for c in body["covenantsApproved"]) == ["DSCR", "POSSIBLE_COVENANT"]
    assert sorted(c["metric"] for c in body["covenantsRejected"]) == ["LEVERAGE", "POSSIBLE_COVENANT"]
    assert [w["phrase"] for w in body["learnedWordings"]] == [WORDING]


def test_phrase_of_drops_amounts_numbering_and_notes():
    assert learning.phrase_of("(ii) Borrowings (refer note 12) 1,234.50") == "Borrowings"
    assert learning.phrase_of("3. Trade payables - 40") == "Trade payables"
    assert learning.phrase_of("Sundry creditors") == "Sundry creditors"


def test_a_wording_that_would_undo_reviewers_decisions_is_refused(client, app):
    """Phase 4 gate: the bank's own decisions are the benchmark a learned wording must not make worse."""
    from dps.services import remember_label

    with session(app) as s:
        doc = Document(type="LOAN_AGREEMENT", file_name="g.pdf", storage_path="g", sha256="g", size_bytes=1,
                       uploaded_by="u", status="PUBLISHED")
        s.add(doc)
        s.flush()
        s.add(ExtractedTerm(document_id=doc.id, metric_raw="DSCR", category="FINANCIAL", status="REJECTED",
                            reviewed_by="ap-raj", page=2, quote="the Cash Cover Ratio of the guarantor shall be 1.1"))
        remember_label(s, "CP-9", "Deferred payment liabilities 40.0", "ST_DEBT", None, "rm-anil")
        s.commit()

    def post(**body):
        return client.post("/dps/v1/learning/wordings", headers=ADMIN, json=body)

    r = post(kind="COVENANT", phrase="Cash Cover Ratio", key="DSCR")
    assert (r.status_code, r.json()["code"]) == (409, "WORSE_ON_DECISIONS")
    assert "rejected 1 proposal" in r.json()["message"]
    r = post(kind="STATEMENT_LINE", phrase="Deferred payment liabilities", key="LT_DEBT")
    assert (r.status_code, r.json()["code"]) == (409, "WORSE_ON_DECISIONS")
    assert post(kind="STATEMENT_LINE", phrase="Deferred payment liabilities", key="ST_DEBT").status_code == 201

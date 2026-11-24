"""Phase 3: model gateway, classification (rules + grounded model), identifiers, ANALYZE end to end."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from conftest import h
from sqlalchemy import select
from test_phase2_ingest import text_pdf

from dps import jobs
from dps.agent.gateway import (
    AiBudgetExceeded,
    AiUnavailable,
    Gateway,
    ModelOutputInvalid,
    Route,
    Routes,
    TokenBucket,
    TransientModelError,
    parse_json,
)
from dps.analyze import analysis
from dps.analyze.classify import classify, grounded
from dps.analyze.identify import (
    cin_valid,
    find_identifiers,
    gstin_valid,
    legibility,
    lei_valid,
    match,
    name_similarity,
    pan_valid,
    udin_valid,
)
from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.integrations.spring_tools import LookupMatch
from dps.models import EventFeed, Job, Run
from dps.parse import sandbox
from dps.parse.engine import ParserEngine
from dps.parse.page_model import build
from dps.worker import Worker

GOLDEN = Path(__file__).resolve().parents[2] / "golden-set"
SERVICE = h("cts-spring", "SERVICE")


def model_of(*pages: str, fmt: str = "PDF") -> dict:
    return {"format": fmt, "pageCount": len(pages), "sheets": [], "flags": [], "segments": [],
            "pages": [{"no": i + 1, "text": t, "quality": 0.9, "method": "NATIVE",
                       "blocks": [{"id": f"p{i + 1}b{j + 1}", "kind": "PARAGRAPH", "text": line}
                                  for j, line in enumerate(t.splitlines()) if line.strip()]}
                      for i, t in enumerate(pages)]}


# ---------- identifiers ----------

@pytest.mark.parametrize(("fn", "good", "bad"), [
    (pan_valid, "AAPFU0939F", "AAPXU0939F"),
    (gstin_valid, "27AAPFU0939F1ZV", "27AAPFU0939F1ZW"),
    (lei_valid, "5493001KJTIIGC8Y1R12", "5493001KJTIIGC8Y1R13"),
    (cin_valid, "L17110MH1973PLC019786", "X17110MH1973PLC019786"),
    (udin_valid, "26123456ABCDEF1234", "2612345ABCDEF1234"),
])
def test_identifier_validators(fn, good, bad):
    assert fn(good) and not fn(bad)


def test_find_identifiers_with_evidence():
    page1 = ("SANCTION LETTER\nM/s Shree Polymers Private Limited\nCIN: L17110MH1973PLC019786  PAN AAPFU0939F\n"
             "GSTIN 27AAPFU0939F1ZV, LEI 5493001KJTIIGC8Y1R12\nLoan Account No: 1234-5678-9012\n"
             "Amounts in ₹ lakh. Financial statements for the year ended 31st March, 2026")
    page2 = "For and on behalf of the Bank\nAuthorised Signatory\nUDIN: 26123456ABCDEF1234"
    found = {f.kind: f for f in find_identifiers(model_of(page1, page2))}
    assert found["PAN"].value == "AAPFU0939F" and found["PAN"].page == 1 and found["PAN"].block_id == "p1b3"
    assert found["GSTIN"].value == "27AAPFU0939F1ZV" and found["LEI"].value == "5493001KJTIIGC8Y1R12"
    assert found["LOAN_ACCOUNT"].value == "123456789012"
    assert found["PERIOD_END"].value == "2026-03-31" and found["UNIT"].value == "LAKH"
    assert found["CURRENCY"].value == "INR" and found["SIGNATURE"].value == "PRESENT"
    assert found["UDIN"].value == "26123456ABCDEF1234"
    assert "Shree Polymers" in found["BORROWER_NAME"].value
    assert not any(f.kind == "PERIOD_END" for f in find_identifiers(model_of("year ended 31 February 2026")))


def test_custom_patterns_and_name_similarity():
    found = find_identifiers(model_of("Facility FAC-00042 under account 000123456789"),
                             loan_account_pattern=r"account\s+(\d{12})", facility_pattern=r"(FAC-\d{5})")
    kinds = {f.kind: f.value for f in found}
    assert kinds["FACILITY_ID"] == "FAC-00042" and kinds["LOAN_ACCOUNT"] == "000123456789"
    assert name_similarity("M/s Shree Polymers Pvt. Ltd.", "Shree Polymers Private Limited") == 1.0
    assert legibility(model_of("a", "b")) == 0.9 and legibility({"pages": []}) == 0.0


def test_matching_reports_status_and_degrades():
    found = find_identifiers(model_of("PAN AAPFU0939F and CIN L17110MH1973PLC019786"))

    class Tools:
        def __init__(self, up):
            self.up = up

        def lookup_identifiers(self, items, context, cid):
            if not self.up:
                return {i: LookupMatch(i, False, code="SPRING_UNAVAILABLE") for i, _, _ in items}
            return {i: LookupMatch(i, True, "STRONG" if k == "PAN" else "CONFLICT",
                                   ({"entityType": "BORROWER", "entityId": "B-7"},)) for i, k, _ in items}

    out, warnings = match(found, Tools(True), {"cpIds": ["CP-1"]})
    status = {i["kind"]: i["match"] for i in out}
    assert status["PAN"] == {"status": "STRONG", "candidateIds": ["B-7"]} and status["CIN"]["status"] == "CONFLICT"
    assert warnings == []
    out, warnings = match(found, Tools(False), None)
    assert {i["match"]["status"] for i in out} == {"NONE"} and warnings[0]["code"] == "SPRING_UNAVAILABLE"


# ---------- classification ----------

SAMPLES = {
    "SANCTION_LETTER": "Ref No.: HO/CR/2026/11\nDear Sir,\nSANCTION LETTER\nWe are pleased to inform you that the "
                       "Bank has sanctioned the following facilities.\nTerms and conditions of sanction are below.",
    "LOAN_AGREEMENT": "RUPEE TERM LOAN AGREEMENT\nThis Agreement is made on 1 April 2026 BETWEEN Shree Polymers Ltd "
                      "(the Borrower) AND State Bank (the Lender).\nEvent of Default. Interest Period.",
    "AMENDMENT": "FIRST AMENDMENT AGREEMENT\nThis agreement amends the Facility Agreement dated 1 April 2024.\n"
                 "Clause 21.2 shall be deleted and replaced with the following.",
    "FINANCIAL_STATEMENT": "Balance Sheet as at 31 March 2026\nStatement of Profit and Loss\nRevenue from operations "
                           "1,200\nTotal assets 5,000\nIndependent Auditor's Report",
    "COMPLIANCE_CERTIFICATE": "COMPLIANCE CERTIFICATE\nWe hereby certify that the Borrower has complied with the "
                              "financial covenants.\nUDIN: 26123456ABCDEF1234\nChartered Accountants",
    "SECURITY_DOCUMENT": "DEED OF HYPOTHECATION\nThe Borrower hereby hypothecates the movable assets.\nSchedule of "
                         "assets hypothecated",
    "OTHER_EVIDENCE": "STOCK STATEMENT as on 30 September 2026\nRaw material 120\nFinished goods 300",
}


@pytest.mark.parametrize("cls", list(SAMPLES))
def test_rules_classify_each_class(cls):
    c = classify(model_of(SAMPLES[cls]))
    assert c.type == cls and c.method == "RULES", (c.type, c.scores)
    for other in SAMPLES:
        if other != cls:
            assert classify(model_of(SAMPLES[cls]), expected=other).mismatch  # swapped type is caught


def test_an_lma_facilities_agreement_cover_page_is_a_loan_agreement():
    # UK LMA agreements are titled "Facilities Agreement" (plural); the cover page names no borrower clause yet.
    cover = ("EXECUTION VERSION\nFACILITIES AGREEMENT\nDated July 2023\nfor\nAQUILA MIDCO 2 LIMITED\nas Parent\n"
             "arranged by\nICG ALTERNATIVE INVESTMENT LIMITED\nas Mandated Lead Arranger\n")
    body = "The Borrower shall ensure that Leverage does not exceed 5.00:1. " * 40
    c = classify({"pages": [{"no": 1, "text": cover}, {"no": 2, "text": "TABLE OF CONTENTS"}, {"no": 3, "text": body}]})
    assert c.type == "LOAN_AGREEMENT", c.scores


@pytest.mark.parametrize("cover", [
    "FIRST AMENDMENT\nTO SECOND AMENDED AND RESTATED CREDIT AGREEMENT\nTHIS FIRST AMENDMENT TO SECOND AMENDED AND "
    "RESTATED CREDIT AGREEMENT (this \"Amendment\") is dated as of May 6, 2010 among the Borrower and the Lenders.",
    "SIXTH AMENDMENT TO\nTHIRD AMENDED AND RESTATED REVOLVING CREDIT AGREEMENT\nThis Sixth Amendment is entered "
    "into among the Borrower, the Administrative Agent and the Lenders.",
])
def test_a_us_amendment_to_a_credit_agreement_is_an_amendment(cover):
    body = "The Credit Agreement is amended by deleting Section 7.11 and replacing it. The Borrower shall comply. " * 20
    c = classify({"pages": [{"no": 1, "text": cover}, {"no": 2, "text": body}]})
    assert c.type == "AMENDMENT", c.scores


def test_sub_attributes():
    c = classify(model_of("Consolidated Balance Sheet as at 31 March 2026\nStatement of Profit and Loss for the "
                          "quarter ended 31 March 2026\nRevenue from operations 10"))
    assert (c.basis, c.period_type) == ("CONSOLIDATED", "QUARTERLY")
    assert classify(model_of(SAMPLES["OTHER_EVIDENCE"])).evidence_kind == "STOCK_STATEMENT"


class FakeGateway:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    def call_json(self, route, messages, schema, check):
        self.calls += 1
        if self.error:
            raise self.error
        problem = check(self.answer)
        if problem:
            raise ModelOutputInvalid(problem)
        return self.answer

    def begin_job(self):
        from dps.agent.gateway import JobBudget

        return JobBudget(1000)

    def end_job(self):
        return None


def test_model_answer_only_when_rules_unsure_and_must_be_grounded():
    vague = model_of("Note on the arrangement\nThe parties record the terms below for the working capital line.")
    good = {"type": "SANCTION_LETTER", "basis": None, "periodType": None,
            "evidence": [{"page": 1, "quote": "The parties record the terms below for the working capital line."}]}
    gw = FakeGateway(good)
    c = classify(vague, "LOAN_AGREEMENT", gw)
    assert (c.type, c.method, gw.calls) == ("SANCTION_LETTER", "MODEL", 1) and c.confidence < 0.8
    assert not c.mismatch and c.uncertain is False
    bad = {**good, "evidence": [{"page": 1, "quote": "We are pleased to sanction"}]}
    assert grounded(bad, vague) is not None
    assert classify(vague, None, FakeGateway(bad)).type == "UNKNOWN"
    assert classify(vague, None, FakeGateway(error=AiUnavailable("down"))).type == "UNKNOWN"
    # Miro AI function 1: the Document Agent gives a second opinion even when the rules are sure. It reads
    # SANCTION_LETTER where the rules read LOAN_AGREEMENT: the type is disputed and a person confirms it.
    agreement = model_of(SAMPLES["LOAN_AGREEMENT"])
    line = next(t.strip() for t in SAMPLES["LOAN_AGREEMENT"].splitlines() if len(t.strip()) >= 12)
    other = {"type": "SANCTION_LETTER", "basis": None, "periodType": None, "evidence": [{"page": 1, "quote": line}]}
    rules_gw = FakeGateway(other)
    disputed = classify(agreement, None, rules_gw)
    assert rules_gw.calls == 1 and disputed.type == "LOAN_AGREEMENT" and disputed.disputed == "SANCTION_LETTER"
    assert disputed.uncertain is True and disputed.as_result()["aiSecondOpinion"] == "SANCTION_LETTER"
    same = classify(agreement, None, FakeGateway({**other, "type": "LOAN_AGREEMENT"}))
    assert same.method == "RULES+AI_CONFIRMED" and same.confidence >= 0.95 and same.disputed is None


def _golden():
    types = {r["documentId"]: r["type"] for r in csv.DictReader(open(GOLDEN / "manifest.csv", encoding="utf-8"))}
    out = []
    for path in sorted((GOLDEN / "documents").glob("*.pdf")):
        data = path.read_bytes()
        model = build(data, "PDF", hashlib.sha256(data).hexdigest(), engine=ParserEngine())
        if any((p.get("text") or "").strip() for p in model["pages"]):  # scans need OCR (heavy pool)
            out.append((path.stem, types[path.stem], model))
    return out


def test_golden_set_accuracy_and_swapped_types():
    docs = _golden()
    correct = sum(classify(m).type == want for _, want, m in docs)
    assert correct / len(docs) >= 0.98, f"{correct}/{len(docs)}"
    swapped = [(m, want) for _, want, m in docs]
    caught = sum(classify(m, expected="COMPLIANCE_CERTIFICATE" if want != "COMPLIANCE_CERTIFICATE"
                          else "LOAN_AGREEMENT").mismatch for m, want in swapped)
    assert caught / len(swapped) >= 0.95
    false_flags = sum(classify(m, expected=want).mismatch for m, want in swapped)
    assert false_flags / len(swapped) <= 0.01


# ---------- gateway ----------

def gateway(provider_answers: list, rpm: int = 600, **kw) -> tuple[Gateway, list]:
    sent: list = []

    def provider(route: Route):
        def call(messages):
            sent.append(messages)
            item = provider_answers.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return call

    routes = Routes({"small": Route("small", "fake", "small-model", retries=kw.get("retries", 3), rpm=rpm)},
                    per_job_tokens=kw.get("budget", 1000))
    return Gateway(routes, provider, sleep=lambda s: None), sent


def test_gateway_retries_transient_errors():
    gw, sent = gateway([TransientModelError("429"), TransientModelError("timeout"), '{"ok": 1}'])
    assert gw.call_json("small", [{"role": "user", "content": "x"}]) == {"ok": 1} and len(sent) == 3


def test_gateway_breaker_opens_then_unavailable():
    gw, _ = gateway([TransientModelError("503")] * 10, retries=0)
    for _ in range(5):
        with pytest.raises(AiUnavailable):
            gw.call("small", [])
    assert not gw.available("small")
    with pytest.raises(AiUnavailable, match="circuit open"):
        gw.call("small", [])


def test_a_spent_month_and_a_refusal_are_not_outages_and_keep_the_breaker_closed():
    """Phase 3 fix 3 (23 Nov 2026): before, both raised AiUnavailable and opened the breaker for the next jobs."""
    from dps.agent.gateway import AiMonthlyBudgetSpent, AiRefused, ModelRefused
    from dps.agent.guard import AiBudgetExhausted

    gw, sent = gateway([AiBudgetExhausted("100 of 100 tokens used in 2026-11")] * 8, retries=3)
    for _ in range(8):
        with pytest.raises(AiMonthlyBudgetSpent) as spent:
            gw.call("small", [])
        assert spent.value.code == "AI_MONTHLY_BUDGET_SPENT" and isinstance(spent.value, AiBudgetExceeded)
    assert gw.available("small") and len(sent) == 8  # no retries, breaker still closed
    gw, sent = gateway([ModelRefused("content_filter")] * 8 + ['{"ok": 1}'], retries=3)
    for _ in range(8):
        with pytest.raises(AiRefused):
            gw.call("small", [])
    assert gw.available("small") and len(sent) == 8
    assert gw.call_json("small", []) == {"ok": 1}  # the next document still gets the model


def test_the_live_provider_maps_a_content_filter_error_to_a_refusal(monkeypatch):
    from dps.advisory import advisor
    from dps.agent import gateway as gw_module
    from dps.agent.gateway import ModelRefused

    errors = iter([RuntimeError("litellm.ContentPolicyViolationError: AzureException - content_filter (500)"),
                   RuntimeError("503 Service Unavailable")])

    def fake_completion(model, temperature=None, timeout=None, max_tokens=None):
        def call(messages):
            raise next(errors)
        return call

    monkeypatch.setattr(advisor, "azure_completion", fake_completion)
    settings = type("S", (), {"ai_routes_file": "", "ai_model": "azure/x", "ai_rpm_share": 1, "ai_trace_text": False,
                              "ai_temperature": None})()
    live = gw_module._live_gateway(settings)
    provider = live._provider(live.routes.routes["strong"])
    with pytest.raises(ModelRefused):  # even though its text holds "500"
        provider([])
    with pytest.raises(TransientModelError):
        provider([])


def test_gateway_repairs_once_then_invalid():
    gw, sent = gateway(["not json", '{"type": "X"}'])
    schema = {"type": "object", "required": ["type"]}
    assert gw.call_json("small", [{"role": "user", "content": "q"}], schema) == {"type": "X"}
    assert "not valid" in sent[1][-1]["content"]
    gw, _ = gateway(["nope", "still nope"])
    with pytest.raises(ModelOutputInvalid):
        gw.call_json("small", [], schema)


def test_gateway_budget_and_unknown_route(monkeypatch):
    from dps.agent import usage

    gw, _ = gateway(['{"a":1}', '{"a":2}'], budget=10)
    monkeypatch.setattr(usage, "last_call_tokens", lambda: 25)
    budget = gw.begin_job()
    gw.call("small", [])
    assert budget.used == 25 and budget.models == {"small": "small-model"}
    with pytest.raises(AiBudgetExceeded):
        gw.call("small", [])
    gw.end_job()
    with pytest.raises(AiUnavailable):
        gw.call("strong", [])
    assert gw.completion("small")([]) == '{"a":2}'


def test_gateway_provider_bug_is_an_outage():
    gw, _ = gateway([ValueError("bad key")])
    with pytest.raises(AiUnavailable, match="ValueError"):
        gw.call("small", [])


def test_routes_file_and_defaults(tmp_path):
    f = tmp_path / "routes.yaml"
    f.write_text("defaults: {retries: 1, rpm: 60}\nroutes:\n  small: {provider: azure_openai, deployment: s1, "
                 "timeout_s: 30}\nbudgets: {per_job_tokens: 99}\n", encoding="utf-8")
    r = Routes.load(f)
    assert r.routes["small"].deployment == "s1" and r.routes["small"].retries == 1 and r.per_job_tokens == 99
    d = Routes.load(None, "azure/gpt")
    assert set(d.routes) == {"small", "strong", "vision"} and d.routes["vision"].deployment == "azure/gpt"


def test_token_bucket_waits():
    now = {"t": 0.0}
    waits: list = []
    bucket = TokenBucket(2, clock=lambda: now["t"], sleep=lambda s: (waits.append(s), now.update(t=now["t"] + s)))
    for _ in range(3):
        bucket.take()
    assert waits and waits[0] == pytest.approx(30.0)


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here: {"a": 2} done') == {"a": 2}


# ---------- ANALYZE end to end ----------

@pytest.mark.parametrize("cancel_before_finish", [False, True])
def test_analyze_job_from_staging_to_event(client, app, settings, tmp_path, cancel_before_finish):
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    data = text_pdf([SAMPLES["LOAN_AGREEMENT"] + "\nPAN AAPFU0939F"])
    digest = hashlib.sha256(data).hexdigest()
    (settings.staging_dir / "stg-1.pdf").write_bytes(data)
    body = {"jobType": "ANALYZE", "priority": "INTERACTIVE",
            "context": {"tenantId": "default", "cpIds": ["CP-1"], "requestedBy": "rm-anil",
                        "expectedType": "FINANCIAL_STATEMENT"},
            "document": {"stagingRef": "stg-1.pdf", "sha256": digest, "fileName": "a.pdf",
                         "mediaType": "application/pdf", "sizeBytes": len(data)}}
    r = client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": "key-analyze-1"})
    assert r.status_code == 202, r.text
    job_id = r.json()["jobId"]
    again = client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": "key-analyze-2"})
    assert again.json()["jobId"] == job_id  # same bytes: same ANALYZE job

    class Tools:
        def lookup_identifiers(self, items, context, cid):
            return {i: LookupMatch(i, True, "STRONG", ({"entityType": "BORROWER", "entityId": "B-1"},))
                    for i, _, _ in items}

    store = LocalStore(settings.storage_dir, settings.staging_dir, tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def analyzer(db, job, model):
        result = analysis.analyze(model, job, None, Tools(), "1")
        if cancel_before_finish:
            job.cancel_requested = True
            db.commit()
        analysis.finish(db, job, result)

    w = Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, analyzer)
    assert w.run_once()
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        if cancel_before_finish:
            assert (job.status, job.error_code) == (jobs.CANCELLED, "JOB_CANCELLED")
            events = s.scalars(select(EventFeed.type).where(EventFeed.job_id == job_id).order_by(EventFeed.seq)).all()
            assert events == ["dps.job.accepted", "dps.job.cancelled"]
            assert s.scalars(select(Run).where(Run.job_id == job_id)).first() is None
            return
        assert (job.status, job.error_code) == (jobs.NEEDS_REVIEW, "TYPE_MISMATCH")
        event = s.scalars(select(EventFeed).where(EventFeed.job_id == job_id,
                                                  EventFeed.type == "dps.analysis.completed")).one()
        payload = json.loads(event.payload_json)
        assert payload["type"] == "LOAN_AGREEMENT" and payload["mismatch"] is True
        assert payload["schemaVersion"] == "1.1"
        assert {"kind": "PAN", "match": "STRONG", "page": 1} in payload["identifiers"]
        assert "AAPFU0939F" not in event.payload_json
        assert not s.scalars(select(EventFeed).where(EventFeed.job_id == job_id,
                                                     EventFeed.type.like("dps.extraction%"))).first()
        assert s.scalars(select(Run).where(Run.job_id == job_id)).one().run_no == 1
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    from jsonschema import ValidationError
    from test_jobapi import check

    from dps import feed

    check("result", result)
    current_event = feed.view(event)
    check("event", current_event)
    current_event["payload"]["identifiers"][0]["value"] = "historical"
    with pytest.raises(ValidationError):
        check("event", current_event)
    current_event["payload"]["schemaVersion"] = "1.0"
    check("event", current_event)
    assert any(i["value"] == "AAPFU0939F" for i in result["identifiers"])
    # B3: a wrong type is a mapping problem: job NEEDS_REVIEW, route MAPPING_REVIEW (the spec's name)
    assert result["classification"]["type"] == "LOAN_AGREEMENT" and result["document"]["route"] == "MAPPING_REVIEW"


def test_analyze_rejects_bad_staging(client, app, settings):
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    (settings.staging_dir / "s.pdf").write_bytes(b"%PDF-1.4 x")
    base = {"jobType": "ANALYZE", "context": {"requestedBy": "rm-anil"},
            "document": {"sha256": "0" * 64, "fileName": "a.pdf", "mediaType": "application/pdf", "sizeBytes": 9}}
    r = client.post("/dps/v1/jobs", json=base, headers={**SERVICE, "Idempotency-Key": "key-analyze-3"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_INPUT"
    base["document"]["stagingRef"] = "s.pdf"
    r = client.post("/dps/v1/jobs", json=base, headers={**SERVICE, "Idempotency-Key": "key-analyze-4"})
    assert r.json()["code"] == "INTEGRITY_MISMATCH"
    base["document"]["stagingRef"] = "../x"
    r = client.post("/dps/v1/jobs", json=base, headers={**SERVICE, "Idempotency-Key": "key-analyze-5"})
    assert r.json()["code"] == "INVALID_INPUT"


def test_worker_without_analyzer_fails_analyze(client, app, settings, tmp_path):
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    data = text_pdf(["hello"])
    (settings.staging_dir / "z.pdf").write_bytes(data)
    body = {"jobType": "ANALYZE", "context": {"requestedBy": "rm-anil"},
            "document": {"stagingRef": "z.pdf", "sha256": hashlib.sha256(data).hexdigest(), "fileName": "z.pdf",
                         "mediaType": "application/pdf", "sizeBytes": len(data)}}
    r = client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": "key-analyze-6"})
    job_id = r.json()["jobId"]
    Worker(settings, app.state.db.session_factory, object(), {}).run_once()
    with app.state.db.session_factory() as s:
        assert s.get(Job, job_id).error_code == "UNSUPPORTED_JOB_TYPE"

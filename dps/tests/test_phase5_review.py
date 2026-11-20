"""Phase 5: scorer and calibration, routing and QA sampling, review API, reclassify, learning, AI status contract."""

from __future__ import annotations

import json
import random

import pytest
from conftest import h
from sqlalchemy import select
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase4_skills import CERTIFICATE, SANCTION, make_worker, submit

from dps import jobs
from dps.models import EventFeed, GoldenCandidate, Job, ReviewDecision, Run
from dps.scoring import (
    RuleTableScorer,
    Scorer,
    brier,
    calibrate,
    expected_calibration_error,
    isotonic,
    route,
    sampled,
    signals,
    train,
)

SERVICE = h("cts-spring", "SERVICE")
OPS = h("ops-1", "SUPPORT")


def field(**kw):
    base = {"id": "f-1", "group": "AGREEMENT_TERM", "name": "X", "value": {"key": "X"}, "method": "RULES",
            "confidence": None, "signals": None, "issues": [], "status": "AUTO_ACCEPTED",
            "source": {"page": 1, "quote": "a quote", "bbox": None, "blockId": None, "cellRef": None}}
    base.update(kw)
    return base


# ---------- scorer ----------

def test_signals_and_rule_table():
    good = signals(field(method="RULES+AI_CONFIRMED"), {1: (0.95, "NATIVE")}, "LOW")
    s = RuleTableScorer().score(good)
    assert s.confidence >= 0.95 and s.reasons == []
    bad = signals(field(source={"page": None, "quote": None}, method="AI",
                        issues=[{"code": "MODELS_DISAGREE"}, {"code": "TOTALS_DO_NOT_ADD"}]),
                  {1: (0.4, "OCR")}, "HIGH")
    s = RuleTableScorer().score(bad)
    assert s.confidence == 0.05 and s.reasons[0] == "NOT_GROUNDED" and len(s.reasons) == 3
    assert signals(field(), None)["ocr_conf_min"] == 1.0


def test_routing_thresholds_and_sample():
    f = field(confidence=0.97)
    route(f, "MEDIUM", sample_rate=0.0)
    assert f["status"] == "AUTO_ACCEPTED"
    f = field(confidence=0.93)
    route(f, "MEDIUM", sample_rate=0.0)
    assert f["status"] == "NEEDS_REVIEW"
    f = field(confidence=0.93)
    route(f, "LOW", sample_rate=0.0)
    assert f["status"] == "AUTO_ACCEPTED"
    f = field(confidence=0.99)
    route(f, "HIGH", sample_rate=0.0)
    assert f["status"] == "NEEDS_REVIEW"  # high risk: always reviewed in the first release
    f = field(confidence=0.99)
    route(f, "LOW", sample_rate=1.0)
    assert f["status"] == "NEEDS_REVIEW" and f["signals"]["sampled"] is True
    confirmed = field(status="CONFIRMED", confidence=0.1)
    route(confirmed, "HIGH")
    assert confirmed["status"] == "CONFIRMED"
    rate = sum(sampled(f"field-{i}") for i in range(20000)) / 20000
    assert 0.025 < rate < 0.035 and sampled("abc") == sampled("abc")


def test_logistic_training_calibration_and_metrics(tmp_path):
    rnd = random.Random(3)  # noqa: S311 - synthetic training data
    rows = []
    for _ in range(600):
        grounded = rnd.random() < 0.8
        agree = rnd.random() < 0.7
        s = signals(field(source={"page": 1, "quote": "q"} if grounded else {"page": None, "quote": None},
                          issues=[] if agree else [{"code": "MODELS_DISAGREE"}]), None, "MEDIUM")
        p_right = 0.95 if grounded and agree else (0.6 if grounded else 0.2)
        rows.append((s, rnd.random() < p_right))
    model = train(rows[:400])
    raw = [(model.raw(s), ok) for s, ok in rows[400:500]]
    model.calibration = isotonic(raw)
    held = [(model.score(s).confidence, ok) for s, ok in rows[500:]]
    assert expected_calibration_error(held) <= 0.12  # small held-out set; the target 0.05 is for real data
    assert brier(held) < 0.25
    assert model.weights["grounded"] > 0 and model.weights["agree"] > 0
    path = tmp_path / "sanction.json"
    path.write_text(model.to_json(), encoding="utf-8")
    scorer = Scorer(tmp_path)
    assert scorer.for_type("SANCTION").__class__.__name__ == "LogisticScorer"
    assert scorer.for_type("certificate").__class__.__name__ == "RuleTableScorer"
    assert calibrate(0.5, []) == 0.5 and calibrate(2.0, [(0.3, 0.1), (0.9, 0.8)]) == 0.8
    assert expected_calibration_error([]) == 0.0 and brier([]) == 0.0
    weak = model.score(signals(field(source={"page": None, "quote": None}), None, "HIGH"))
    assert "NOT_GROUNDED" in weak.reasons


def test_isotonic_is_monotone():
    table = isotonic([(0.1, False), (0.2, True), (0.3, False), (0.8, True), (0.9, True)])
    ys = [y for _, y in table]
    assert ys == sorted(ys)


# ---------- review API ----------

def _run_certificate(client, app, settings, tmp_path, version="cv-1", key="key-cert-1"):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", version, key)
    job_id = r.json()["jobId"]
    assert w.run_once()
    return job_id


def test_review_flow_confirm_correct_add_and_conflict(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path)
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    check("review", review)
    assert review["revision"] == 1 and review["fields"]
    first = review["fields"][0]
    assert first["evidence"]["pageImage"] == "/dps/v1/versions/cv-1/pages/1/image"
    assert "HIGH_RISK_FIELD" in first["reasons"]
    decisions = {"decisions": [
        {"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"][1:]] + [
        {"fieldId": first["fieldId"], "action": "CORRECT", "value": {"figure": 1.5}, "reasonCode": "WRONG_VALUE",
         "comment": "certificate page 1"},
        {"fieldId": "added-1", "action": "ADD", "group": "CERTIFIED_FIGURE", "name": "Current Ratio",
         "value": {"covenant": "CURRENT_RATIO", "figure": 1.3}, "page": 1, "reasonCode": "MISSED"}]}
    check("review-decisions", decisions)
    stale = client.post(f"/dps/v1/jobs/{job_id}/review", json=decisions, headers={**SERVICE, "If-Match": "7"})
    assert stale.status_code == 409 and stale.json()["code"] == "REVISION_CHANGED"
    r = client.post(f"/dps/v1/jobs/{job_id}/review", json=decisions,
                    headers={**SERVICE, "If-Match": "1", "X-User": "analyst-1"})
    assert r.status_code == 202, r.text
    assert r.json()["revision"] == 2 and r.json()["remaining"] == 0 and r.json()["status"] == jobs.DONE
    again = client.post(f"/dps/v1/jobs/{job_id}/review", json=decisions, headers={**SERVICE, "If-Match": "1"})
    assert again.status_code == 409  # two reviewers at once: the second one reloads
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    check("result", result)
    by = {f["id"]: f for f in result["fields"]}
    assert by[first["fieldId"]]["status"] == "CORRECTED" and by[first["fieldId"]]["value"]["figure"] == 1.5
    assert by[first["fieldId"]]["method"] == "MANUAL" and by["added-1"]["status"] == "CONFIRMED"
    with app.state.db.session_factory() as s:
        runs = s.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.run_no)).all()
        assert [r.run_no for r in runs] == [1, 2] and runs[0].superseded_by == f"{job_id}:2"
        decisions_saved = s.scalars(select(ReviewDecision).where(ReviewDecision.job_id == job_id)).all()
        assert {d.reviewer for d in decisions_saved} == {"analyst-1"}
        assert {c.field_id for c in s.scalars(select(GoldenCandidate))} == {first["fieldId"], "added-1"}
        assert s.scalars(select(EventFeed).where(EventFeed.job_id == job_id,
                                                 EventFeed.type == "dps.extraction.completed")).first()
    runs = client.get("/dps/v1/versions/cv-1/runs", headers=SERVICE).json()["runs"]
    assert runs[0]["trigger"] == "REVIEW" and runs[1]["supersededBy"] == f"{job_id}:2"


def test_review_validation_errors(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path, "cv-2", "key-cert-2")
    bad = [{"fieldId": "nope", "action": "CONFIRM"},
           {"fieldId": "x", "action": "CORRECT"},
           {"fieldId": "y", "action": "ADD", "value": 1}]
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    for d in bad:
        if d["action"] == "CORRECT":
            d["fieldId"] = review["fields"][0]["fieldId"]
        r = client.post(f"/dps/v1/jobs/{job_id}/review", json={"decisions": [d]}, headers={**SERVICE, "If-Match": "1"})
        assert r.status_code == 400, d
    queued = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf(["x"]), ".pdf", "application/pdf", "cv-3",
                    "key-cert-3").json()["jobId"]
    assert client.get(f"/dps/v1/jobs/{queued}/review", headers=SERVICE).status_code == 404
    assert client.get("/dps/v1/jobs/none/review", headers=SERVICE).status_code == 404


def test_reclassify_starts_a_new_job_and_supersedes(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path, "cv-4", "key-cert-4")
    r = client.post(f"/dps/v1/jobs/{job_id}/reclassify", json={"type": "SANCTION_LETTER", "reason": "it is a letter"},
                    headers={**SERVICE, "X-User": "analyst-3"})
    assert r.status_code == 202, r.text
    new_id = r.json()["jobId"]
    with app.state.db.session_factory() as s:
        new = s.get(Job, new_id)
        assert new.job_type == "EXTRACT_SANCTION" and new.document_version_id == "cv-4"
        assert s.scalars(select(Run).where(Run.job_id == job_id)).first().superseded_by == new_id
    bad = client.post(f"/dps/v1/jobs/{job_id}/reclassify", json={"type": "OTHER_EVIDENCE", "reason": "x"},
                      headers=SERVICE)
    assert bad.status_code == 422


def test_quality_and_golden_candidates(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path, "cv-5", "key-cert-5")
    with app.state.db.session_factory() as s:
        s.add(ReviewDecision(job_id=job_id, run_no=2, field_id="f", reviewer="a", action="CORRECT", sampled=True))
        s.add(ReviewDecision(job_id=job_id, run_no=2, field_id="g", reviewer="a", action="CONFIRM", sampled=True))
        s.add(GoldenCandidate(job_id=job_id, field_id="f", job_type="EXTRACT_CERTIFICATE", payload_json="{}"))
        s.commit()
    q = client.get("/dps/v1/ops/quality", headers=OPS).json()
    assert q["byJobType"] == [{"jobType": "EXTRACT_CERTIFICATE", "sampled": 2, "wrong": 1, "errorRate": 0.5}]
    assert q["goldenCandidatesPending"] == 1
    items = client.get("/dps/v1/ops/golden-candidates", headers=OPS).json()["items"]
    cid = items[0]["id"]
    assert client.post(f"/dps/v1/ops/golden-candidates/{cid}/accept", headers=OPS).json()["status"] == "ACCEPTED"
    assert client.post(f"/dps/v1/ops/golden-candidates/{cid}/reject", headers=OPS).status_code == 409
    assert client.post("/dps/v1/ops/golden-candidates/none/accept", headers=OPS).status_code == 404
    assert client.get("/dps/v1/ops/quality", headers=SERVICE).status_code == 403


def test_ai_covenant_status_contract(client, app, settings, tmp_path):
    from test_jobapi import _def  # noqa: F401 - shared helper

    _run_certificate(client, app, settings, tmp_path, "cv-6", "key-cert-6")
    body = {"cpId": "CP-1", "covenants": [
        {"appliedCovenantId": "AC-1", "covenantName": "Debt Service Coverage Ratio", "latestResult": "COMPLIANT"},
        {"appliedCovenantId": "AC-2", "covenantName": "ICR", "conditionExpression": "ICR >= 2"},
        {"appliedCovenantId": "AC-3", "covenantName": "Net worth"}]}
    from test_jobapi import check_def

    check_def("compat-covenant-status", "request", body)
    r = client.post("/api/v1/ai/covenant-status", json=body, headers=SERVICE).json()
    check_def("compat-covenant-status", "response", r)
    recs = {x["appliedCovenantId"]: x for x in r["recommendations"]}
    assert recs["AC-1"]["recommendedStatus"] == "COMPLIED" and "page 1" in recs["AC-1"]["rationale"]
    assert recs["AC-2"]["recommendedStatus"] == "BREACHED" and "AC-3" not in recs
    empty = client.post("/api/v1/ai/covenant-status", json={**body, "cpId": "CP-404"}, headers=SERVICE).json()
    assert empty == {"recommendations": []}


@pytest.mark.parametrize("pack_name", ["sanction", "certificate"])
def test_pack_results_carry_scores_and_reasons(pack_name):
    from test_phase3_analyze import model_of
    from test_phase4_skills import fake_job

    from dps.skills import load
    from dps.skills import runner as skills

    text = SANCTION if pack_name == "sanction" else CERTIFICATE
    result = skills.run(load(pack_name), fake_job("X"), model_of(text), None, None)
    for f in result["fields"]:
        assert f["signals"]["scorer"] == "RULE_TABLE" and isinstance(f["confidence"], float)
        assert "reasons" in f["signals"]
    assert json.dumps(result)


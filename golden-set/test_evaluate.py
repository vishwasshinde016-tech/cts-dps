import json

from evaluate import evaluate, main, score_document


def label(covs, status="VERIFIED", doc="AGR-T"):
    return {"documentId": doc, "status": status, "covenants": covs}


DSCR = {"metric": "DSCR", "category": "FINANCIAL", "operator": ">=", "threshold": 1.25, "page": 5}
ICR = {"metric": "ICR", "category": "FINANCIAL", "operator": ">=", "threshold": 2.0, "page": 6}
PLEDGE = {"metric": "NEGATIVE_PLEDGE", "category": "NON_FINANCIAL", "operator": None, "threshold": None, "page": 4}


def test_perfect_match():
    s = score_document(label([DSCR, ICR, PLEDGE]), {"covenants": [DSCR, ICR, PLEDGE]})
    assert s.metrics() == {"recall": 1.0, "precision": 1.0, "threshold_accuracy": 1.0, "page_accuracy": 1.0,
                           "frequency_accuracy": None, "unit_accuracy": None, "date_accuracy": None,
                           "review_items": 0, "abstentions": 0, "unsafe_auto_accepts": 0,
                           "material_accuracy": {"scope": None, "conditions": None, "change": None, "quote": None}}


def test_evaluation_counts_abstention_as_a_miss_and_reports_deal_family(tmp_path):
    labels, preds = tmp_path / "labels", tmp_path / "preds"
    labels.mkdir()
    preds.mkdir()
    expected = {**DSCR, "scope": {"obligor": "Borrower"}, "quote": "A ratio of at least 1.25 times"}
    (labels / "AGR-T.json").write_text(json.dumps({**label([expected, ICR]), "dealFamily": "DEAL-1"}))
    (preds / "AGR-T.json").write_text(json.dumps({"covenants": [
        {**expected, "quote": "a ratio of\nat least 1.25 times", "status": "NEEDS_REVIEW"},
        {**ICR, "status": "UNRESOLVED"},
    ]}))
    result = evaluate(labels, preds)
    metrics = result["by_deal_family"]["DEAL-1"]
    assert metrics["review_items"] == 2 and metrics["abstentions"] == 1 and metrics["recall"] == 0.5
    assert metrics["material_accuracy"]["scope"] == metrics["material_accuracy"]["quote"] == 1.0


def test_two_covenants_of_one_metric_are_matched_one_to_one():
    """V2: a step-down or two DSCR tests (historic, projected) were scored as one; the last one won."""
    low, high = {**DSCR, "threshold": 1.1}, {**DSCR, "threshold": 1.4}
    s = score_document(label([low, high]), {"covenants": [high, low]})
    assert s.matched == 2 and s.threshold_correct == 2
    s = score_document(label([low, high]), {"covenants": [high]})
    assert s.matched == 1 and s.misses == ["DSCR"] and s.threshold_correct == 1


def test_frequency_and_unit_are_scored_where_labelled():
    gold = {**DSCR, "frequency": "QUARTERLY", "unit": "times"}
    s = score_document(label([gold, ICR]), {"covenants": [{**gold, "frequency": "YEARLY", "unit": "x"}, ICR]})
    m = s.metrics()
    assert m["frequency_accuracy"] == 0.0 and m["unit_accuracy"] == 1.0  # x and times are one unit
    assert s.with_frequency == 1  # ICR has no labelled frequency: not counted


def test_first_test_date_is_scored_where_labelled():
    gold = {**DSCR, "firstTestDate": "2026-03-31"}
    assert score_document(label([gold, ICR]), {"covenants": [gold, ICR]}).metrics()["date_accuracy"] == 1
    assert score_document(label([gold, ICR]), {"covenants": [DSCR, ICR]}).metrics()["date_accuracy"] == 0
    later = {**gold, "firstTestDate": "2026-06-30"}
    assert score_document(label([gold, later]), {"covenants": [later, gold]}).metrics()["date_accuracy"] == 1


def test_miss_extra_wrong_threshold_and_page():
    pred = [{**DSCR, "threshold": 1.5, "page": 9, "status": "AUTO_ACCEPTED"},
            {"metric": "CURRENT_RATIO", "category": "FINANCIAL", "status": "NEEDS_REVIEW"}]
    s = score_document(label([DSCR, ICR]), {"covenants": pred})
    m = s.metrics()
    assert m["recall"] == 0.5 and m["precision"] == 0.5
    assert m["threshold_accuracy"] == 0.0 and m["page_accuracy"] == 0.0
    assert m["unsafe_auto_accepts"] == 1
    assert s.misses == ["ICR"] and s.extras == ["CURRENT_RATIO"] and "gold >= 1.25" in s.wrong_thresholds[0]


def test_operator_and_none_thresholds():
    a = {"metric": "X", "category": "FINANCIAL", "operator": "<=", "threshold": None}
    assert score_document(label([a]), {"covenants": [a]}).threshold_correct == 1
    assert score_document(label([a]), {"covenants": [{**a, "operator": ">="}]}).threshold_correct == 0
    assert score_document(label([a]), {"covenants": [{**a, "threshold": 1.0}]}).threshold_correct == 0


def test_evaluate_skips_drafts_and_gate(tmp_path, capsys):
    labels, preds = tmp_path / "labels", tmp_path / "preds"
    labels.mkdir()
    preds.mkdir()
    (labels / "AGR-1.json").write_text(json.dumps(label([DSCR, ICR], doc="AGR-1")))
    (labels / "AGR-2.json").write_text(json.dumps(label([DSCR], status="DRAFT_UNVERIFIED", doc="AGR-2")))
    (preds / "AGR-1.json").write_text(json.dumps({"covenants": [DSCR, ICR]}))
    report = evaluate(labels, preds)
    assert report["documents"] == 1 and report["gate_passed"] is True
    with_drafts = evaluate(labels, preds, include_drafts=True)
    assert with_drafts["documents"] == 2 and with_drafts["overall"]["recall"] == 0.6667
    assert with_drafts["gate_passed"] is False
    assert main(["--labels", str(labels), "--predictions", str(preds)]) == 0
    assert main(["--labels", str(labels), "--predictions", str(preds), "--include-drafts"]) == 1
    assert '"gate_passed"' in capsys.readouterr().out


def test_empty_set_does_not_pass(tmp_path):
    (tmp_path / "l").mkdir()
    (tmp_path / "p").mkdir()
    assert evaluate(tmp_path / "l", tmp_path / "p")["gate_passed"] is False


def test_release_evidence_uses_the_prediction_time_and_requires_verified_coverage(tmp_path):
    labels, preds = tmp_path / "labels", tmp_path / "preds"
    labels.mkdir()
    preds.mkdir()
    (labels / "AGR-T.json").write_text(json.dumps({**label([DSCR]), "reviewedBy": "Review test"}))
    (preds / "AGR-T.json").write_text(json.dumps({"covenants": [DSCR]}))
    benchmark = {"mode": "pipeline", "pipelineVersion": "v-test", "predictedAt": "2026-01-01T12:00:00+00:00"}
    (preds / "_benchmark.json").write_text(json.dumps(benchmark))
    evidence = tmp_path / "evidence.json"
    assert main(["--labels", str(labels), "--predictions", str(preds), "--release-evidence", str(evidence)]) == 0
    result = json.loads(evidence.read_text())
    assert result["verifiedOnly"] and result["evaluatedAt"] == benchmark["predictedAt"]
    assert result["pipelineVersion"] == benchmark["pipelineVersion"]
    assert len(result["datasetSha256"]) == 64

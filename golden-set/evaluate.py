"""Golden-set evaluation for covenant extraction (release gate G6 in 05-prd.md).

Usage:
    python evaluate.py --predictions <dir of {documentId}.json> [--labels labels] [--include-drafts]
                       [--statuses VERIFIED,SYNTHETIC]

Each prediction file has the same shape as a label file: {"documentId": ..., "covenants": [...]}.
Matching is per document by (metric, category). Reported per document and overall:
  - covenant recall    = matched labelled covenants / labelled covenants
  - covenant precision = matched predicted covenants / predicted covenants
  - threshold accuracy = matched financial covenants with the same operator and threshold / matched financial
  - page accuracy      = matched covenants whose page equals the labelled page / matched with a labelled page
Statement labels (type FINANCIAL_STATEMENT) hold {"values": {KEY: amount in INR crore}}; a predicted value
counts as correct within 0.5% (or 0.01). value accuracy = correct values / labelled values.

Which labels count is chosen by status: VERIFIED by default (checked by a credit analyst); SYNTHETIC labels
belong to documents generated from a written specification (make_synthetic.py), so their truth is known by
construction; --include-drafts adds DRAFT_UNVERIFIED labels.

Gate (initial, to be agreed): threshold accuracy >= 0.95 and covenant recall >= 0.85, and value accuracy
>= 0.95 when statement labels count.

Improvement plan V2 (24 Nov 2026): several covenants of one metric are matched one to one (a step-down kept only the
last before); frequency and unit are scored where the label gives them (frequency accuracy, unit accuracy); and
precision and frequency accuracy are gated too (>= 0.90 and >= 0.95), so a flood of false covenants or wrong test
frequencies fails CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

GATE_THRESHOLD_ACCURACY = 0.95
GATE_RECALL = 0.85
GATE_VALUE_ACCURACY = 0.95
GATE_PRECISION = 0.90
GATE_FREQUENCY_ACCURACY = 0.95
GATE_DATE_ACCURACY = 0.95
REVIEW_ONLY = {"ABSTAINED", "UNRESOLVED", "REJECTED"}
MATERIAL_FIELDS = ("scope", "conditions", "change", "quote")


@dataclass
class Score:
    labelled: int = 0
    predicted: int = 0
    matched: int = 0
    financial_matched: int = 0
    threshold_correct: int = 0
    with_page: int = 0
    page_correct: int = 0
    with_frequency: int = 0
    frequency_correct: int = 0
    with_unit: int = 0
    unit_correct: int = 0
    with_date: int = 0
    date_correct: int = 0
    misses: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    wrong_thresholds: list[str] = field(default_factory=list)
    material_checked: dict[str, int] = field(default_factory=dict)
    material_correct: dict[str, int] = field(default_factory=dict)
    review_items: int = 0
    abstentions: int = 0
    unsafe_auto_accepts: int = 0

    def add(self, other: Score) -> None:
        for name in ("labelled", "predicted", "matched", "financial_matched", "threshold_correct",
                     "with_page", "page_correct", "with_frequency", "frequency_correct", "with_unit", "unit_correct",
                     "with_date", "date_correct", "review_items", "abstentions", "unsafe_auto_accepts"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for key in MATERIAL_FIELDS:
            self.material_checked[key] = self.material_checked.get(key, 0) + other.material_checked.get(key, 0)
            self.material_correct[key] = self.material_correct.get(key, 0) + other.material_correct.get(key, 0)

    @staticmethod
    def _ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    def metrics(self) -> dict:
        return {
            "recall": self._ratio(self.matched, self.labelled),
            "precision": self._ratio(self.matched, self.predicted),
            "threshold_accuracy": self._ratio(self.threshold_correct, self.financial_matched),
            "page_accuracy": self._ratio(self.page_correct, self.with_page),
            "frequency_accuracy": self._ratio(self.frequency_correct, self.with_frequency),
            "unit_accuracy": self._ratio(self.unit_correct, self.with_unit),
            "date_accuracy": self._ratio(self.date_correct, self.with_date),
            "review_items": self.review_items,
            "abstentions": self.abstentions,
            "unsafe_auto_accepts": self.unsafe_auto_accepts,
            "material_accuracy": {key: self._ratio(self.material_correct.get(key, 0),
                                                   self.material_checked.get(key, 0)) for key in MATERIAL_FIELDS},
        }


def _key(c: dict) -> tuple[str, str]:
    return (str(c.get("metric") or "").upper(), str(c.get("category") or "FINANCIAL").upper())


def _same_threshold(a: dict, b: dict) -> bool:
    if a.get("operator") != b.get("operator"):
        return False
    x, y = a.get("threshold"), b.get("threshold")
    if x is None or y is None:
        return x is y
    return abs(float(x) - float(y)) <= 1e-9


def _norm_unit(u) -> str:
    unit = str(u or "").strip().upper().rstrip(".")
    return "TIMES" if unit == "X" else unit


def _pairs(gold: list[dict], pred: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """One-to-one pairs per (metric, category): every exact threshold match first, then the rest in order."""
    pairs, left, waiting = [], list(pred), list(gold)
    for level in ("date", "threshold", "metric"):
        pending = []
        for g in waiting:
            same = next((p for p in left if _key(p) == _key(g)
                         and (level == "metric" or _same_threshold(g, p))
                         and (level != "date" or (g.get("firstTestDate") and
                                                  g["firstTestDate"] == p.get("firstTestDate")))), None)
            if same is None:
                pending.append(g)
            else:
                left.remove(same)
                pairs.append((g, same))
        waiting = pending
    return pairs, waiting, left


def score_document(label: dict, prediction: dict) -> Score:
    s = Score()
    gold = list(label.get("covenants", []))
    proposals = prediction.get("covenants", [])
    pred = [p for p in proposals if p.get("status") not in REVIEW_ONLY]
    s.review_items = prediction.get("reviewItems", sum(p.get("status") in {"NEEDS_REVIEW", "UNRESOLVED"}
                                                        for p in proposals))
    s.abstentions = sum(p.get("status") in {"ABSTAINED", "UNRESOLVED"} for p in proposals)
    s.labelled, s.predicted = len(gold), len(pred)
    pairs, missed, extra = _pairs(gold, pred)
    s.misses = [_key(g)[0] for g in missed]
    s.extras = [_key(p)[0] for p in extra]
    s.unsafe_auto_accepts = sum(p.get("status") == "AUTO_ACCEPTED" for p in extra)
    for g, p in pairs:
        k = _key(g)
        s.matched += 1
        safe = True
        if k[1] == "FINANCIAL":
            s.financial_matched += 1
            if _same_threshold(g, p):
                s.threshold_correct += 1
            else:
                safe = False
                s.wrong_thresholds.append(f"{k[0]}: gold {g.get('operator')} {g.get('threshold')} "
                                          f"vs {p.get('operator')} {p.get('threshold')}")
        if g.get("page") is not None:
            s.with_page += 1
            if g.get("page") == p.get("page"):
                s.page_correct += 1
            else:
                safe = False
        if g.get("frequency"):
            s.with_frequency += 1
            if str(g["frequency"]).upper() == str(p.get("frequency") or "").upper():
                s.frequency_correct += 1
            else:
                safe = False
        if g.get("unit"):
            s.with_unit += 1
            if _norm_unit(g["unit"]) == _norm_unit(p.get("unit")):
                s.unit_correct += 1
            else:
                safe = False
        if g.get("firstTestDate"):
            s.with_date += 1
            if g["firstTestDate"] == p.get("firstTestDate"):
                s.date_correct += 1
            else:
                safe = False
        for key in MATERIAL_FIELDS:
            expected = g.get(key)
            if expected is None:
                continue
            actual = p.get(key)
            correct = expected == actual
            if key == "quote":
                expected = " ".join(expected.casefold().split())
                actual = " ".join((actual or "").casefold().split())
                correct = bool(expected and expected in actual and (g.get("page") is None or g["page"] == p.get("page")))
            s.material_checked[key] = s.material_checked.get(key, 0) + 1
            if correct:
                s.material_correct[key] = s.material_correct.get(key, 0) + 1
            elif key != "quote" or not p.get("quote"):
                safe = False
        if not safe and p.get("status") == "AUTO_ACCEPTED":
            s.unsafe_auto_accepts += 1
    return s


def score_statement(label: dict, prediction: dict) -> tuple[int, int, list[str]]:
    """(labelled values, correct values, mismatches) for one statement."""
    gold = label.get("values") or {}
    pred = prediction.get("values") or {}
    wrong = []
    for key, value in gold.items():
        got = pred.get(key)
        if got is None or abs(float(got) - float(value)) > max(0.01, abs(float(value)) * 0.005):
            wrong.append(f"{key}: gold {value} vs {got}")
    return len(gold), len(gold) - len(wrong), wrong


def evaluate(labels_dir: Path, predictions_dir: Path, include_drafts: bool = False,
             statuses: tuple[str, ...] = ("VERIFIED",), documents: list[str] | None = None) -> dict:
    total = Score()
    per_doc = {}
    families: dict[str, Score] = {}
    values_labelled = values_correct = 0
    counted = set(statuses) | ({"DRAFT_UNVERIFIED"} if include_drafts else set())
    for label_path in sorted(labels_dir.glob("*.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        if label.get("status") not in counted or (documents and label["documentId"] not in documents):
            continue
        pred_path = predictions_dir / label_path.name
        prediction = json.loads(pred_path.read_text(encoding="utf-8")) if pred_path.exists() else {"covenants": []}
        if label.get("type") == "FINANCIAL_STATEMENT":
            n, ok, wrong = score_statement(label, prediction)
            values_labelled += n
            values_correct += ok
            per_doc[label["documentId"]] = {"value_accuracy": Score._ratio(ok, n), "wrong_values": wrong}
            continue
        s = score_document(label, prediction)
        total.add(s)
        if family := label.get("dealFamily"):
            families.setdefault(family, Score()).add(s)
        per_doc[label["documentId"]] = {**s.metrics(), "misses": s.misses, "extras": s.extras,
                                        "wrong_thresholds": s.wrong_thresholds}
    overall = {**total.metrics(), "value_accuracy": Score._ratio(values_correct, values_labelled)}
    passed = (overall["threshold_accuracy"] is not None and overall["recall"] is not None
              and overall["threshold_accuracy"] >= GATE_THRESHOLD_ACCURACY and overall["recall"] >= GATE_RECALL
              and (overall["precision"] is None or overall["precision"] >= GATE_PRECISION)
              and (overall["frequency_accuracy"] is None or overall["frequency_accuracy"] >= GATE_FREQUENCY_ACCURACY)
              and (overall["date_accuracy"] is None or overall["date_accuracy"] >= GATE_DATE_ACCURACY)
              and (overall["value_accuracy"] is None or overall["value_accuracy"] >= GATE_VALUE_ACCURACY))
    return {"documents": len(per_doc), "overall": overall, "gate_passed": passed, "per_document": per_doc,
            "by_deal_family": {family: score.metrics() for family, score in families.items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, default=Path(__file__).parent / "labels")
    ap.add_argument("--predictions", type=Path, required=True)
    ap.add_argument("--include-drafts", action="store_true")
    ap.add_argument("--statuses", default="VERIFIED", help="comma-separated label statuses to count")
    ap.add_argument("--document", action="append", help="evaluate only this document ID (repeatable)")
    ap.add_argument("--release-evidence", type=Path, help="write evaluation evidence for the signed release gate")
    args = ap.parse_args(argv)
    report = evaluate(args.labels, args.predictions, args.include_drafts,
                      tuple(s.strip() for s in args.statuses.split(",") if s.strip()), args.document)
    if args.release_evidence:
        if args.statuses != "VERIFIED" or args.include_drafts or args.document:
            ap.error("Release evidence needs the complete VERIFIED set.")
        benchmark = json.loads((args.predictions / "_benchmark.json").read_text(encoding="utf-8"))
        if benchmark.get("mode") != "pipeline" or not benchmark.get("pipelineVersion") or not benchmark.get("predictedAt"):
            ap.error("Release evidence needs predictions from the DPS pipeline.")
        labels = {path.name: path.read_bytes() for path in sorted(args.labels.glob("*.json"))
                  if (label := json.loads(path.read_bytes())).get("status") == "VERIFIED"
                  and str(label.get("reviewedBy") or "").strip()}
        if not labels or len(labels) != report["documents"] or not all((args.predictions / name).is_file()
                                                                     for name in labels):
            ap.error("Release evidence needs reviewer names and predictions for every verified label.")
        dataset = hashlib.sha256(json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in labels.items()},
                                            sort_keys=True).encode()).hexdigest()
        report["gate_passed"] = report["gate_passed"] and report["overall"]["unsafe_auto_accepts"] == 0
        evidence = {**report, "verifiedOnly": True, "datasetSha256": dataset,
                    "pipelineVersion": benchmark["pipelineVersion"], "evaluatedAt": benchmark["predictedAt"]}
        args.release_evidence.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

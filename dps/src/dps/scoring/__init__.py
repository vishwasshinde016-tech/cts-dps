"""Phase 5: confidence that means something measurable.

``signals`` collects the inputs on every field; ``RuleTableScorer`` scores while a document type has fewer than 200
labelled fields; ``LogisticScorer`` (trained offline, calibrated by isotonic regression) takes over after that.
Every score keeps its three biggest reasons. ``route`` applies the pack's thresholds and the 3% QA sample.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

MIN_LABELLED = 200
SAMPLE_RATE = 0.03
DEFAULT_THRESHOLDS = {"HIGH": None, "MEDIUM": 0.95, "LOW": 0.90}  # HIGH: always reviewed in the first release
RISK = {"LOW": 0.0, "MEDIUM": 0.5, "HIGH": 1.0}
FEATURES = ("grounded", "parse_quality", "ocr_conf_min", "rules_found", "ai_found", "agree", "crosscheck_pass",
            "history_delta", "label_known", "schema_ok", "field_risk")
REASON_TEXT = {
    "grounded": "NOT_GROUNDED", "parse_quality": "POOR_TEXT", "ocr_conf_min": "LOW_OCR_QUALITY",
    "rules_found": "RULES_MISSED", "ai_found": "AI_MISSED", "agree": "MODELS_DISAGREE",
    "crosscheck_pass": "CROSS_CHECK_FAILED", "history_delta": "UNUSUAL_CHANGE", "label_known": "NEW_WORDING",
    "schema_ok": "SCHEMA_INVALID", "field_risk": "HIGH_RISK_FIELD",
}
FAILING_CHECKS = {"TOTALS_DO_NOT_ADD", "DIRECTION_MISMATCH", "STEP_DOWN_ORDER", "FORMULA_INVALID", "CLAUSE_NOT_IN_BASE",
                  "CHANGED_TWICE", "OUTSIDE_TENOR", "CURRENCY_MISMATCH", "RESTATED", "NO_DECLARATION"}


def signals(f: dict, page_quality: dict[int, tuple[float, str]] | None = None, risk: str = "MEDIUM",
            history_delta: float | None = None, label_known: bool | None = None) -> dict:
    """The scorer's inputs for one dps.result field (``page_quality``: page -> (quality, method))."""
    src = f.get("source") or {}
    quality, method = (page_quality or {}).get(src.get("page") or 0, (1.0, "NATIVE"))
    method_used = str(f.get("method") or "")
    codes = {i.get("code") for i in f.get("issues", [])}
    return {
        "grounded": 1.0 if src.get("page") and src.get("quote") else 0.0,
        "parse_quality": float(quality),
        "ocr_conf_min": float(quality) if method == "OCR" else 1.0,
        "rules_found": 0.0 if method_used in ("AI", "AI_VISION", "MANUAL") else 1.0,
        "ai_found": 1.0 if "AI" in method_used.upper() else 0.0,
        "agree": 0.0 if "MODELS_DISAGREE" in codes else 1.0,  # rules and the agent read the same value
        "crosscheck_pass": 0.0 if codes & FAILING_CHECKS else 1.0,
        "history_delta": min(abs(history_delta), 5.0) / 5.0 if history_delta is not None else 0.0,
        "label_known": 1.0 if label_known else 0.0,
        "schema_ok": 0.0 if "SCHEMA_INVALID" in codes else 1.0,
        "field_risk": RISK.get(risk, 0.5),
    }


@dataclass
class Score:
    confidence: float
    reasons: list[str]
    scorer: str


class RuleTableScorer:
    """The documented rule table used until a document type has 200 labelled fields.

    Start at 0.97 for a grounded value read by the rules; each weakness subtracts its penalty; the result is
    clamped to [0.05, 0.99]. Penalties (and their reason codes) are the table below.
    """

    PENALTIES = (
        ("grounded", lambda s: s["grounded"] < 1, 0.45),
        ("agree", lambda s: s["agree"] == 0, 0.35),
        ("crosscheck_pass", lambda s: s["crosscheck_pass"] < 1, 0.30),
        ("schema_ok", lambda s: s["schema_ok"] < 1, 0.40),
        ("ocr_conf_min", lambda s: s["ocr_conf_min"] < 0.8, 0.20),
        ("parse_quality", lambda s: s["parse_quality"] < 0.5, 0.15),
        ("rules_found", lambda s: s["rules_found"] < 1, 0.12),
        ("history_delta", lambda s: s["history_delta"] >= 0.6, 0.10),
    )
    BONUSES = (("ai_found", lambda s: s["ai_found"] > 0 and s["rules_found"] > 0 and s["agree"] > 0, 0.02),
               ("label_known", lambda s: s["label_known"] > 0, 0.01))

    def score(self, s: dict) -> Score:
        value = 0.97
        hits = []
        for name, test, penalty in self.PENALTIES:
            if test(s):
                value -= penalty
                hits.append((penalty, REASON_TEXT[name]))
        for _, test, bonus in self.BONUSES:
            if test(s):
                value += bonus
        if s["field_risk"] >= 1.0:
            hits.append((0.0, "HIGH_RISK_FIELD"))
        hits.sort(key=lambda h: -h[0])
        return Score(round(min(0.99, max(0.05, value)), 3), [r for _, r in hits[:3]], "RULE_TABLE")


@dataclass
class LogisticScorer:
    weights: dict[str, float]
    intercept: float
    calibration: list[tuple[float, float]] = field(default_factory=list)  # isotonic (raw, calibrated) points

    def raw(self, s: dict) -> float:
        z = self.intercept + sum(self.weights.get(k, 0.0) * s.get(k, 0.0) for k in FEATURES)
        return 1.0 / (1.0 + math.exp(-z))

    def score(self, s: dict) -> Score:
        p = calibrate(self.raw(s), self.calibration)
        contrib = sorted(((self.weights.get(k, 0.0) * (s.get(k, 0.0) - 1.0), k) for k in FEATURES),
                         key=lambda c: c[0])
        reasons = [REASON_TEXT[k] for c, k in contrib if c < -0.05][:3]
        return Score(round(p, 3), reasons, "LOGISTIC")

    def to_json(self) -> str:
        return json.dumps({"weights": self.weights, "intercept": self.intercept, "calibration": self.calibration})

    @classmethod
    def load(cls, path: str | Path) -> LogisticScorer:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw["weights"], raw["intercept"], [tuple(p) for p in raw.get("calibration", [])])


def train(rows: Iterable[tuple[dict, bool]], epochs: int = 400, lr: float = 0.5, l2: float = 0.01) -> LogisticScorer:
    """Plain logistic regression by gradient descent (no extra dependency); rows are (signals, was_correct)."""
    data = [([s.get(k, 0.0) for k in FEATURES], 1.0 if ok else 0.0) for s, ok in rows]
    w = [0.0] * len(FEATURES)
    b = 0.0
    n = max(1, len(data))
    for _ in range(epochs):
        gw = [0.0] * len(FEATURES)
        gb = 0.0
        for x, y in data:
            p = 1.0 / (1.0 + math.exp(-(b + sum(wi * xi for wi, xi in zip(w, x, strict=True)))))
            err = p - y
            for i, xi in enumerate(x):
                gw[i] += err * xi
            gb += err
        w = [wi - lr * (g / n + l2 * wi) for wi, g in zip(w, gw, strict=True)]
        b -= lr * gb / n
    return LogisticScorer(dict(zip(FEATURES, w, strict=True)), b)


def isotonic(points: list[tuple[float, bool]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators: a non-decreasing map from raw score to observed accuracy."""
    pts = sorted((p, 1.0 if ok else 0.0) for p, ok in points)
    blocks: list[list[float]] = []  # [sum_y, count, max_x]
    for x, y in pts:
        blocks.append([y, 1.0, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            y2, c2, x2 = blocks.pop()
            blocks[-1][0] += y2
            blocks[-1][1] += c2
            blocks[-1][2] = x2
    return [(round(b[2], 6), round(b[0] / b[1], 6)) for b in blocks]


def calibrate(p: float, table: list[tuple[float, float]]) -> float:
    if not table:
        return p
    for x, y in table:
        if p <= x:
            return y
    return table[-1][1]


def expected_calibration_error(scored: list[tuple[float, bool]], bins: int = 10) -> float:
    if not scored:
        return 0.0
    total = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        bucket = [(p, ok) for p, ok in scored if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if bucket:
            conf = sum(p for p, _ in bucket) / len(bucket)
            acc = sum(1 for _, ok in bucket if ok) / len(bucket)
            total += len(bucket) / len(scored) * abs(conf - acc)
    return round(total, 4)


def brier(scored: list[tuple[float, bool]]) -> float:
    if not scored:
        return 0.0
    return round(sum((p - (1.0 if ok else 0.0)) ** 2 for p, ok in scored) / len(scored), 4)


def sampled(field_id: str, rate: float = SAMPLE_RATE) -> bool:
    """Deterministic 3% QA sample: the same field is always in or out (reproducible audits)."""
    h = int(hashlib.sha256(field_id.encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


def route(f: dict, risk: str, thresholds: dict | None = None, sample_rate: float = SAMPLE_RATE) -> None:
    """Sets ``status`` (and the SAMPLED_FOR_QA reason) from confidence, risk and the pack's thresholds."""
    if f.get("status") in ("REJECTED", "CONFIRMED", "CORRECTED"):
        return
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    need = limits.get(risk)
    conf = f.get("confidence") or 0.0
    reasons = (f.get("signals") or {}).get("reasons", [])
    if f.get("status") == "NEEDS_REVIEW" or need is None or conf < need:
        f["status"] = "NEEDS_REVIEW"
        return
    if sampled(f["id"], sample_rate):
        f["status"] = "NEEDS_REVIEW"
        f["signals"] = {**(f.get("signals") or {}), "reasons": [*reasons, "SAMPLED_FOR_QA"], "sampled": True}
        return
    f["status"] = "AUTO_ACCEPTED"


class Scorer:
    """Picks the logistic model for a document type when one was released (file per type), else the rule table."""

    def __init__(self, models_dir: str | Path | None = None) -> None:
        self.models_dir = Path(models_dir) if models_dir else None
        self.rules = RuleTableScorer()

    def for_type(self, doc_type: str):
        if self.models_dir is not None:
            path = self.models_dir / f"{doc_type.lower()}.json"
            if path.is_file():
                return LogisticScorer.load(path)
        return self.rules

    def apply(self, fields: list[dict], doc_type: str, risk_of, thresholds: dict | None = None,
              page_quality: dict[int, tuple[float, str]] | None = None) -> None:
        model = self.for_type(doc_type)
        for f in fields:
            risk = risk_of(f["name"])
            s = signals(f, page_quality, risk)
            result = model.score(s)
            f["confidence"] = result.confidence
            f["signals"] = {**(f.get("signals") or {}),
                            **{k: v for k, v in s.items() if not k.startswith("_")},
                            "reasons": result.reasons, "scorer": result.scorer}
            route(f, risk, thresholds)

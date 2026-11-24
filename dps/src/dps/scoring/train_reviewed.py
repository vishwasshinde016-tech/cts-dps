"""Train and test a scorer using independently reviewed fields split by deal family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dps.scoring import FEATURES, brier, calibrate, expected_calibration_error, isotonic, train

MAX_CALIBRATION_ERROR = 0.05


def train_reviewed(rows: list[dict]):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Reviewed fields must be a JSON array of records.")
    splits: dict[str, list[tuple[dict, bool]]] = {key: [] for key in ("TRAIN", "CALIBRATE", "HOLDOUT")}
    families: dict[str, str] = {}
    for row in rows:
        split, family, correct = row.get("split"), row.get("dealFamily"), row.get("wasCorrect")
        signals = row.get("signals")
        if (row.get("status") != "VERIFIED" or not str(row.get("reviewedBy") or "").strip()
                or not isinstance(family, str) or not family.strip() or split not in splits or type(correct) is not bool
                or not isinstance(signals, dict)
                or any(type(signals.get(k)) not in (int, float) or not 0 <= signals[k] <= 1 for k in FEATURES)):
            raise ValueError("Every row needs verified review, known split and complete numeric signals.")
        if families.setdefault(family, split) != split:
            raise ValueError("A deal family cannot appear in multiple splits.")
        splits[split].append((signals, correct))
    if any({correct for _, correct in examples} != {True, False} for examples in splits.values()):
        raise ValueError("Each split needs both correct and incorrect reviewed fields.")
    model = train(splits["TRAIN"])
    model.calibration = isotonic([(model.raw(s), correct) for s, correct in splits["CALIBRATE"]], precise=True)
    holdout = [(calibrate(model.raw(s), model.calibration), correct) for s, correct in splits["HOLDOUT"]]
    error = expected_calibration_error(holdout)
    return model, {"rows": {key: len(value) for key, value in splits.items()}, "calibrationError": error,
                   "brierScore": brier(holdout), "calibrationPassed": error <= MAX_CALIBRATION_ERROR}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-fields", type=Path, required=True)
    parser.add_argument("--candidate-model", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows = json.loads(args.reviewed_fields.read_bytes())
        model, report = train_reviewed(rows)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Training stopped. {exc}\n")
    print(json.dumps(report, indent=2))
    if not report["calibrationPassed"]:
        return 1
    with args.candidate_model.open("x", encoding="utf-8") as model_file:
        model_file.write(model.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

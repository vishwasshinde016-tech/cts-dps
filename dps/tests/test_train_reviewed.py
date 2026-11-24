import pytest

from dps.scoring import FEATURES
from dps.scoring.train_reviewed import train_reviewed


def test_training_calibration_and_evaluation_keep_deal_families_separate():
    examples = [{"split": split, "dealFamily": f"{split}-{correct}", "wasCorrect": correct,
                 "reviewedBy": "test reviewer", "status": "VERIFIED",
                 "signals": dict.fromkeys(FEATURES, float(correct))}
                for split in ("TRAIN", "CALIBRATE", "HOLDOUT") for correct in (True, False)]
    model, report = train_reviewed(examples)
    assert report["calibrationPassed"] and model.calibration
    examples[-1]["dealFamily"] = examples[0]["dealFamily"]
    with pytest.raises(ValueError, match="deal family"):
        train_reviewed(examples)

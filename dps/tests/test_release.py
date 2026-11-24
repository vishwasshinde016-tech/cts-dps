import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from dps.ops.release import signing_payload, verify

NOW = datetime(2026, 11, 24, tzinfo=UTC)
IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize("case", ("valid", "missing", "stale", "wrong_pipeline", "skipped", "bad_signature", "unsafe"))
def test_release_requires_current_signed_evaluation_for_exact_pipeline(tmp_path, case):
    key = Ed25519PrivateKey.generate()
    trusted = tmp_path / "trusted.pem"
    trusted.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    evaluation_path = tmp_path / "eval.json"
    dataset = "f" * 64
    evaluation = {"gate_passed": case != "skipped", "verifiedOnly": True, "documents": 5,
                  "overall": {"unsafe_auto_accepts": 1 if case == "unsafe" else 0},
                  "pipelineVersion": "wrong" if case == "wrong_pipeline" else "test-v1", "datasetSha256": dataset,
                  "evaluatedAt": (NOW - timedelta(days=7 if case == "stale" else 1)).isoformat()}
    data = signing_payload(evaluation)
    if case != "missing":
        evaluation_path.write_bytes(data)
    release = {"imageDigest": IMAGE, "evaluationSha256": hashlib.sha256(data).hexdigest(),
               "datasetSha256": dataset, "pipelineVersion": "test-v1", "approvedBy": "Risk test signer"}
    signature = key.sign(signing_payload(release)).hex()
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps({"manifest": release,
                                         "signature": "00" * 64 if case == "bad_signature" else signature}))
    settings = {"image": IMAGE, "pipeline": "test-v1", "max_age": timedelta(days=2), "now": NOW}
    if case == "valid":
        assert verify(manifest_path, evaluation_path, trusted, **settings) == release
    else:
        with pytest.raises(ValueError):
            verify(manifest_path, evaluation_path, trusted, **settings)

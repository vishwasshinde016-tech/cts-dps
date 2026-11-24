"""Verify signed release evidence before promoting a DPS image from SHADOW."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key


def signing_payload(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def verify(manifest_path: Path, evaluation_path: Path, public_key_path: Path, *, image: str, pipeline: str,
           max_age: timedelta, now: datetime | None = None) -> dict:
    """Return the authenticated release manifest, or raise ValueError for any failed precondition."""
    now = now or datetime.now(UTC)
    try:
        envelope = json.loads(manifest_path.read_bytes())
        manifest = envelope["manifest"]
        key = load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("The release verification key must be Ed25519.")
        key.verify(bytes.fromhex(envelope["signature"]), signing_payload(manifest))
        evaluation_bytes = evaluation_path.read_bytes()
        evaluation = json.loads(evaluation_bytes)
        evaluated = datetime.fromisoformat(evaluation["evaluatedAt"])
        current = timedelta(0) <= now - evaluated <= max_age
        exact_image = re.fullmatch(r"sha256:[0-9a-f]{64}", image) and manifest["imageDigest"] == image
        same_eval = manifest["evaluationSha256"] == hashlib.sha256(evaluation_bytes).hexdigest()
        same_pipeline = bool(pipeline) and manifest["pipelineVersion"] == evaluation["pipelineVersion"] == pipeline
        same_dataset = (re.fullmatch(r"[0-9a-f]{64}", evaluation["datasetSha256"])
                        and manifest["datasetSha256"] == evaluation["datasetSha256"])
        unsafe = evaluation["overall"]["unsafe_auto_accepts"]
        passed = (evaluation["gate_passed"] is True and evaluation["verifiedOnly"] is True
                  and type(evaluation["documents"]) is int and evaluation["documents"] > 0
                  and type(unsafe) is int and unsafe == 0)
        if not (current and exact_image and same_eval and same_pipeline and same_dataset and passed
                and isinstance(manifest["approvedBy"], str) and manifest["approvedBy"].strip()):
            raise ValueError("Release evidence is stale, failed, lacks approval, or does not match the release.")
    except (OSError, KeyError, TypeError, InvalidSignature) as exc:
        raise ValueError("Release evidence is missing, malformed, or has an invalid signature.") from exc
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Expected immutable sha256 digest")
    parser.add_argument("--pipeline", required=True, help="Expected deployed pipeline version")
    parser.add_argument("--max-age-hours", type=int, required=True)
    args = parser.parse_args()
    try:
        verify(args.manifest, args.evaluation, args.public_key, image=args.image, pipeline=args.pipeline,
               max_age=timedelta(hours=args.max_age_hours))
    except ValueError as exc:
        parser.exit(1, f"Release blocked. {exc}\n")
    print("Release evidence verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

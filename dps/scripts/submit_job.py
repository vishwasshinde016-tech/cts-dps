"""Upload a document and run a real job through the running DPS service, the way Spring and the DMS do.

Start the API and the worker first (two terminals, in dps/):

    .venv\\Scripts\\uvicorn --factory dps.main:create_app --port 9400
    .venv\\Scripts\\python -m dps.worker

Then, in a third terminal:

    python scripts/submit_job.py ../architect-documents/us_ex10_1_credit_agreement_2020.pdf        # ONBOARD
    python scripts/submit_job.py ../architect-documents/us_holly_amendment_2010.pdf --job EXTRACT_AMENDMENT
    python scripts/submit_job.py ../golden-set/documents/FS-011.pdf --job EXTRACT_STATEMENT --as-of 2026-03-31

What it does, step by step (each step prints a line):
1. "DMS": stores the file in DPS_STORAGE_DIR under its SHA-256, as the DMS keeps immutable versions.
2. "Spring": POST /dps/v1/jobs with the document reference (objectKey, sha256, size) and the context (CP id ...).
3. Polls GET /dps/v1/jobs/{id} and prints each stage the worker reports.
4. GET /dps/v1/jobs/{id}/result: prints the classification, the covenants and the coverage; --json saves it.

Local development only: it uses the dev sign-in headers (DPS_AUTH_MODE=DEV_HEADER in .env).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

MEDIA = {".pdf": "application/pdf", ".csv": "text/csv", ".xls": "application/vnd.ms-excel",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
JOBS = ["ONBOARD", "EXTRACT_AGREEMENT", "EXTRACT_AMENDMENT", "EXTRACT_SANCTION", "EXTRACT_STATEMENT",
        "EXTRACT_CERTIFICATE", "ANALYZE"]
FINAL = {"DONE", "NEEDS_REVIEW", "FAILED", "DEAD", "CANCELLED"}
HEADERS = {"X-User": "cts-spring", "X-Roles": "SERVICE"}


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--job", default="ONBOARD", choices=JOBS, help="job type (default ONBOARD)")
    ap.add_argument("--type", default=None, help="the type the user chose, e.g. LOAN_AGREEMENT (optional)")
    ap.add_argument("--cp", default="CP-DEMO-1", help="credit proposal id Spring would send")
    ap.add_argument("--as-of", default=None, help="period end for statements, YYYY-MM-DD")
    ap.add_argument("--api", default="http://localhost:9400")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--json", type=Path, help="save the full dps.result JSON here")
    ap.add_argument("--wait", type=int, default=900, help="seconds to wait for the worker")
    args = ap.parse_args()

    media = MEDIA.get(args.file.suffix.lower())
    if media is None:
        print(f"unsupported file type {args.file.suffix}; DPS reads {', '.join(MEDIA)}")
        return 2
    load_env(args.env)
    import httpx

    from dps.ingest.file_store import FileStore
    from dps.settings import Settings

    data = args.file.read_bytes()
    sha, key = FileStore(Settings().storage_dir).put(data, args.file.suffix.lower())
    print(f"1. DMS     stored {args.file.name} ({len(data) // 1024} KB) as {key}")

    context: dict = {"tenantId": "default", "cpIds": [args.cp], "requestedBy": "rm-demo"}
    if args.type:
        context["expectedType"] = args.type
    if args.as_of:
        context["asOfDate"] = args.as_of
    body = {"jobType": args.job, "context": context,
            "document": {"versionId": f"v-{sha[:12]}", "objectKey": key, "sha256": sha, "fileName": args.file.name,
                         "mediaType": media, "sizeBytes": len(data)}}
    with httpx.Client(base_url=args.api, headers=HEADERS, timeout=60) as http:
        r = http.post("/dps/v1/jobs", json=body, headers={"Idempotency-Key": f"demo-{uuid.uuid4().hex[:16]}"})
        if r.status_code >= 300:
            print(f"2. Spring  POST /dps/v1/jobs failed: {r.status_code} {r.text[:400]}")
            return 1
        job = r.json()
        job_id = job["jobId"]
        dup = (" (duplicate: DPS already has a job for this document and context, so it returns that one)"
               if job.get("duplicate") else "")
        print(f"2. Spring  POST /dps/v1/jobs {args.job} -> 202 jobId {job_id}{dup}")

        seen, start, status = None, time.time(), {}
        while time.time() - start < args.wait:
            status = http.get(f"/dps/v1/jobs/{job_id}").json()
            line = f"{status.get('status')} {status.get('stage') or ''}".strip()
            if line != seen:
                print(f"3. Worker  {line}  ({time.time() - start:.0f} s)")
                seen = line
            if status.get("status") in FINAL:
                break
            time.sleep(1.5)
        else:
            print("the worker did not finish in time: is `python -m dps.worker` running?")
            return 1
        if status.get("status") in {"FAILED", "DEAD", "CANCELLED"}:
            print(f"   job ended {status.get('status')}: {status.get('errorCode')} {status.get('error') or ''}")
            return 1

        result = http.get(f"/dps/v1/jobs/{job_id}/result").json()
    c = result.get("classification") or {}
    print(f"4. Result  {result.get('schema')} status {result.get('status')}, type {c.get('type')} "
          f"({c.get('confidence')}), skill {(result.get('pipeline') or {}).get('skillPack')}")
    for f in result.get("fields", []):
        if f["group"] in ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "EVENT_OF_DEFAULT", "AMENDMENT_CHANGE",
                          "STATEMENT_LINE"):
            v = f["value"]
            if f["group"] == "STATEMENT_LINE":
                if v.get("amount") is None:
                    continue  # a derived line (annualised, trailing) that this statement does not give
                level = f"{v['amount']} {v.get('unit') or ''}".strip()
            else:
                level = f"{v.get('operator') or ''} {'' if v.get('threshold') is None else v['threshold']}".strip()
            print(f"   p{f['source'].get('page')!s:>3}  {f['group'][:22]:<22} "
                  f"{str(v.get('code') or v.get('metric') or v.get('key') or f.get('name'))[:30]:<30} {level}")
    led = ((result.get("document") or {}).get("coverage") or {}).get("ledger")
    if led:
        print(f"   coverage: {led['candidates']} duty sentences, {led.get('unresolved', 0)} for a person to check")
    warnings = sorted({w["code"] for w in result.get("warnings") or []})
    if warnings:
        print(f"   warnings: {', '.join(warnings)}")
    print(f"   open in the browser: {args.api}/dps/v1/jobs/{job_id}/result  (needs the dev headers)")
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print(f"   full result saved to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

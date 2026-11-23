"""Bulk check: read a folder of real documents the way production does and report, per document and in total, what
DPS found and where it is weak. For testing on real agreements, statements and certificates before go-live.

Each file goes through the real path: POST /dps/v1/jobs, the queue, the worker (a HEAVY-pool worker too, for what
moves there), GET .../result. Everything runs on a throwaway database and storage in a temp folder; the real dps.db
is never touched. Rules only by default; --ai uses the Azure model of .env (needs network access to that
endpoint; costs tokens).

    python scripts/bulk_check.py C:/docs/agreements --out C:/reports/run1
    python scripts/bulk_check.py C:/docs/mixed --manifest C:/docs/mixed/manifest.csv --ai --max-tokens 600000

manifest.csv (optional) columns: file, jobType, asOfDate (YYYY-MM-DD, required for statements), cpId, expectedType.
Without a manifest: PDF / Word files run as ONBOARD (DPS finds the type), spreadsheets as EXTRACT_STATEMENT with
--as-of as their period end.

Writes <out>/<file>.json (the result and the report row), <out>/summary.csv and <out>/summary.md.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

MEDIA = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".xls": "application/vnd.ms-excel", ".csv": "text/csv"}
SHEETS = {".xlsx", ".xls", ".csv"}
COVENANTS = {"FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT"}
AI_KEYS = ("DPS_LLM_ENABLED", "DPS_AI_MODEL", "DPS_AI_ALLOWED_HOSTS", "DPS_AI_ROUTES_FILE", "DPS_AI_TEMPERATURE",
           "DPS_AI_RPM_SHARE")


def setup_env(tmp: Path, ai: bool, repo: Path) -> None:
    """A throwaway DPS: its own SQLite file and folders; AI keys only with --ai (values are never printed)."""
    os.environ.update({
        "DPS_DATABASE_URL": f"sqlite:///{(tmp / 'bulk.db').as_posix()}", "DPS_STORAGE_DIR": str(tmp / "storage"),
        "DPS_STAGING_DIR": str(tmp / "staging"), "DPS_DERIVED_DIR": str(tmp / "derived"),
        "DPS_SEALS_DIR": str(tmp / "seals"), "DPS_AUTH_MODE": "DEV_HEADER", "DPS_ENV": "dev",
        "DPS_WORKER_ID": "bulk-worker", "DPS_LLM_ENABLED": "false", "DPS_AI_MODEL": "", "DPS_CORE_BASE_URL": "",
        "CREWAI_DISABLE_TELEMETRY": "true",
    })
    for key in [k for k in os.environ if k.startswith("AZURE_")]:
        os.environ[key] = ""
    if not ai:
        return
    env = repo / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            if key.startswith("AZURE_") or key in AI_KEYS:
                os.environ[key] = value.strip('"')
    routes = os.environ.get("DPS_AI_ROUTES_FILE")
    if routes and not Path(routes).is_absolute():
        os.environ["DPS_AI_ROUTES_FILE"] = str((repo / routes).resolve())


def read_manifest(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["file"].strip(): row for row in csv.DictReader(f) if row.get("file")}


def documents(folder: Path, manifest: Path | None) -> list[Path]:
    """The files to read. The manifest is a CSV in the same folder: it describes the documents, it is not one of them
    (bulk run of 22 Nov 2026: it was read as a statement and refused)."""
    skip = {manifest.resolve()} if manifest else set()
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in MEDIA and p.is_file()
                  and p.resolve() not in skip and p.name.lower() != "manifest.csv")


def job_for(path: Path, row: dict, args) -> dict:
    """The job request for one file, as Spring would send it."""
    sheet = path.suffix.lower() in SHEETS
    job_type = (row.get("jobType") or "").strip() or ("EXTRACT_STATEMENT" if sheet else args.job_type)
    ctx = {"tenantId": "default", "cpIds": [(row.get("cpId") or "").strip() or "CP-BULK"], "requestedBy": "bulk-check"}
    as_of = (row.get("asOfDate") or "").strip() or (args.as_of if job_type == "EXTRACT_STATEMENT" else "")
    if as_of:
        ctx["asOfDate"] = as_of
    if (row.get("expectedType") or "").strip():
        ctx["expectedType"] = row["expectedType"].strip()
    return {"jobType": job_type, "priority": "BULK", "context": ctx}


def report_row(path: Path, job: dict, status: dict, result: dict | None, seconds: float, problems: list[str]) -> dict:
    """One line of the summary: what DPS found and where it is weak."""
    fields = (result or {}).get("fields") or []
    groups = Counter(f.get("group") for f in fields)
    cov = ((result or {}).get("document") or {}).get("coverage") or {}
    # the coverage ledger's "look here" items (method COVERAGE) are pointers for a reviewer, not covenants
    pointers = [f for f in fields if f.get("method") == "COVERAGE"]
    covenants = [f for f in fields if f.get("group") in COVENANTS and f.get("method") != "COVERAGE"]
    generic = sum(1 for f in covenants if str((f.get("value") or {}).get("code") or "").endswith("OTHER")
                  or not (f.get("value") or {}).get("code"))
    usage = (result or {}).get("usage") or {}
    return {
        "file": path.name, "jobType": job["jobType"], "status": status.get("status"),
        "errorCode": status.get("errorCode"), "type": ((result or {}).get("classification") or {}).get("type"),
        "fields": len(fields), "covenants": len(covenants),
        "financial": groups.get("FINANCIAL_COVENANT", 0), "nonFinancial": groups.get("NON_FINANCIAL_COVENANT", 0),
        "aiFound": sum(1 for f in covenants if str(f.get("method") or "").startswith("AI")),
        "lookHere": len(pointers), "eventsOfDefault": groups.get("EVENT_OF_DEFAULT", 0),
        "scheduleTerms": groups.get("SCHEDULE_TERM", 0), "statementLines": groups.get("STATEMENT_LINE", 0),
        # a certificate's figures and a sanction letter's terms are what those documents hold (not covenants)
        "certifiedFigures": groups.get("CERTIFIED_FIGURE", 0), "agreementTerms": groups.get("AGREEMENT_TERM", 0),
        "genericCodes": generic, "coveragePct": cov.get("percent"), "unexplained": cov.get("unexplainedCount"),
        "lowConfidence": sum(1 for f in fields if (f.get("confidence") or 1) < 0.8),
        "warnings": " ".join(sorted({w.get("code", "") for w in (result or {}).get("warnings") or []})),
        "contractProblems": len(problems), "aiCalls": usage.get("model_calls"), "tokens": usage.get("tokens"),
        "seconds": round(seconds, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="report folder (default: <folder>/bulk-report)")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--job-type", default="ONBOARD", help="for PDF / Word files without a manifest row")
    ap.add_argument("--as-of", default=None, help="period end for spreadsheets without a manifest row (YYYY-MM-DD)")
    ap.add_argument("--ai", action="store_true", help="use the Azure model of .env (costs tokens)")
    ap.add_argument("--max-tokens", type=int, default=0, help="stop starting new files after this many tokens")
    ap.add_argument("--limit", type=int, default=0, help="read at most this many files")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds per file before it is reported as stuck")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[1]
    tmp = Path(tempfile.mkdtemp(prefix="dps-bulk-"))
    setup_env(tmp, args.ai, repo)
    sys.path.insert(0, str(repo / "src"))
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from dps import results
    from dps.db import Base, Database
    from dps.ingest.file_store import FileStore
    from dps.integrations.spring import SpringClient
    from dps.main import create_app
    from dps.settings import Settings
    from dps.worker import Worker, build_analyzer, build_ingestor, build_pack_runner

    Base.metadata.create_all(create_engine(os.environ["DPS_DATABASE_URL"]))
    settings = Settings()
    store = FileStore(settings.storage_dir)
    client = TestClient(create_app(settings))
    headers = {"X-User": "bulk-check", "X-Roles": "SERVICE"}
    database = Database(settings.database_url)
    core = SpringClient.from_settings(settings)

    def worker(pool: str) -> Worker:
        s = Settings(worker_pool=pool) if pool != settings.worker_pool else settings
        models: dict = {}
        return Worker(s, database.session_factory, core, {}, build_ingestor(s) if s.ingest_enabled else None, models,
                      build_analyzer(s, database), build_pack_runner(s, database, core, models))

    workers = [worker(settings.worker_pool), worker("HEAVY")]
    manifest = read_manifest(args.manifest)
    files = documents(args.folder, args.manifest)
    files = files[:args.limit] if args.limit else files
    out = args.out or (args.folder / "bulk-report")
    out.mkdir(parents=True, exist_ok=True)
    rows, spent = [], 0
    print(f"{len(files)} file(s); throwaway DPS in {tmp}; AI {'on' if args.ai else 'off'}")
    for n, path in enumerate(files, 1):
        if args.max_tokens and spent >= args.max_tokens:
            print(f"token cap reached ({spent:,}); {len(files) - n + 1} file(s) not read")
            break
        started = time.monotonic()
        data = path.read_bytes()
        sha, key = store.put(data, path.suffix.lower())
        job = job_for(path, manifest.get(path.name, {}), args)
        job["document"] = {"versionId": f"v-{sha[:12]}", "objectKey": key, "sha256": sha, "fileName": path.name,
                           "mediaType": MEDIA[path.suffix.lower()], "sizeBytes": len(data)}
        idem = "bulk-" + hashlib.sha256(path.name.encode()).hexdigest()[:24]
        r = client.post("/dps/v1/jobs", json=job, headers={**headers, "Idempotency-Key": idem})
        if r.status_code not in (200, 202):
            status, result = {"status": "REFUSED", "errorCode": r.json().get("code")}, None
        else:
            job_id = r.json()["jobId"]
            while time.monotonic() - started < args.timeout and any([w.run_once() for w in workers]):
                pass
            status = client.get(f"/dps/v1/jobs/{job_id}", headers=headers).json()
            got = client.get(f"/dps/v1/jobs/{job_id}/result", headers=headers)
            result = got.json() if got.status_code == 200 else None
            if status.get("status") in ("QUEUED", "RUNNING"):
                status["errorCode"] = "STUCK_AFTER_TIMEOUT"
        problems = results.check_format(result) if result else []
        row = report_row(path, job, status, result, time.monotonic() - started, problems)
        spent += row["tokens"] or 0
        rows.append(row)
        (out / f"{path.name}.json").write_text(json.dumps({"report": row, "contractProblems": problems,
                                                            "result": result}, indent=1, default=str), encoding="utf-8")
        print(f"[{n}/{len(files)}] {path.name}: {row['status']} {row['type'] or ''} {row['covenants']} covenants, "
              f"coverage {row['coveragePct']}, {row['seconds']} s")
    write_summary(out, rows)
    print(f"report: {out / 'summary.md'}")
    return 0


def write_summary(out: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with (out / "summary.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    done = [r for r in rows if r["status"] in ("DONE", "NEEDS_REVIEW")]
    weak = [r for r in rows if r["status"] not in ("DONE", "NEEDS_REVIEW") or r["contractProblems"]
            or (r["coveragePct"] is not None and r["coveragePct"] < 95) or (r["fields"] == 0)]
    lines = ["# Bulk check", "", f"{len(rows)} file(s): {len(done)} read, {len(rows) - len(done)} not read.", "",
             "## Look at these first", ""]
    lines += [f"- **{r['file']}**: status {r['status']} {r['errorCode'] or ''}, {r['fields']} findings, coverage "
              f"{r['coveragePct']}, contract problems {r['contractProblems']}, warnings {r['warnings'] or '-'}"
              + (" (a statement or certificate: give it its job type and period end in the manifest)"
                 if "TYPE_NOT_ONBOARDING" in (r["warnings"] or "") else "")
              for r in weak] or ["- none"]
    cols = ["file", "status", "type", "covenants", "financial", "nonFinancial", "aiFound", "lookHere", "scheduleTerms",
            "statementLines", "certifiedFigures", "agreementTerms", "genericCodes", "coveragePct", "unexplained",
            "contractProblems", "tokens", "seconds"]
    lines += ["", "## Every file", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(r[c]) if r[c] is not None else "" for c in cols) + " |" for r in rows]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":  # the parse sandbox starts child processes (Windows spawn)
    raise SystemExit(main())

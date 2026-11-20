"""Show where a document is and what happened to it: the file, the page model, and every table row of one job.

    python scripts/show_job.py                 # the latest job
    python scripts/show_job.py <jobId>         # one job (submit_job.py prints the id)
    python scripts/show_job.py --list          # the last 10 jobs

It reads the local database (DPS_DATABASE_URL in .env, SQLite dps.db on a laptop) and the folders DPS_STORAGE_DIR
and DPS_DERIVED_DIR. Nothing is changed. Use it during the demo after each step to show where things went.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def head(text: str) -> None:
    print(f"\n--- {text}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_id", nargs="?")
    ap.add_argument("--list", action="store_true", help="list the last 10 jobs")
    ap.add_argument("--page", type=int, help="also print this page's parsed text from the page model")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    args = ap.parse_args()
    load_env(args.env)

    from sqlalchemy import select

    from dps.db import Database
    from dps.models import AiCall, Artifact, Document, EventFeed, Job, JobAttempt, Run, TraceEvent
    from dps.settings import Settings

    settings = Settings()
    db = Database(settings.database_url)
    with db.session_factory() as s:
        if args.list:
            print(f"{'created (UTC)':<20} {'job id':<38} {'type':<18} {'status':<13} file")
            for j in s.scalars(select(Job).order_by(Job.created_at.desc()).limit(10)):
                doc = s.get(Document, j.document_id)
                print(f"{j.created_at:%Y-%m-%d %H:%M:%S} {j.id:<38} {j.job_type:<18} {j.status:<13} "
                      f"{doc.file_name if doc else ''}")
            return 0
        job = s.get(Job, args.job_id) if args.job_id else s.scalars(select(Job).order_by(Job.created_at.desc())).first()
        if job is None:
            print("no job found. Run scripts/submit_job.py first (with the API and the worker running).")
            return 1
        doc = s.get(Document, job.document_id)

        head("1. The job Spring created (table dps_job)")
        print(f"job id        {job.id}")
        print(f"job type      {job.job_type}   flow {job.flow}   pool {job.pool}")
        print(f"status        {job.status}   attempts {job.attempts}   error {job.error_code or '-'}")
        print(f"created       {job.created_at:%H:%M:%S}   started {job.started_at and f'{job.started_at:%H:%M:%S}'}"
              f"   finished {job.finished_at and f'{job.finished_at:%H:%M:%S}'}")
        ctx = json.loads(job.request_json or "{}").get("context", {})
        print(f"context       {json.dumps(ctx)[:200]}")

        head("2. The uploaded file (the DMS store, folder DPS_STORAGE_DIR)")
        if doc:
            path = Path(settings.storage_dir) / (doc.object_key or doc.storage_path or "")
            print(f"file name     {doc.file_name}   ({doc.size_bytes // 1024} KB)")
            print(f"stored at     {path.resolve()}")
            print(f"exists        {path.exists()}   sha256 {doc.sha256[:16]}...  (table dps_document)")

        head("3. What the worker made from it (folder DPS_DERIVED_DIR, table dps_artifact)")
        arts = list(s.scalars(select(Artifact).where(Artifact.source_sha256 == job.sha256)))
        if not arts:
            print("no derived files yet (the worker has not parsed it)")
        for a in arts:
            print(f"{a.kind:<11} {(Path(settings.derived_dir) / a.path).resolve()}  ({a.bytes // 1024} KB)")
            if args.page and a.kind == "PAGE_MODEL":
                import gzip

                model = json.loads(gzip.decompress((Path(settings.derived_dir) / a.path).read_bytes()))
                pages = model.get("pages", [])
                page = next((p for p in pages if p.get("no") == args.page), None)
                print(f"            {len(pages)} pages, format {model.get('format')}")
                if page is None:
                    print(f"            page {args.page} is not in this document")
                else:
                    method = (page.get("blocks") or [{}])[0].get("method", "")
                    print(f"            page {args.page} ({method or 'text'}), first 1200 characters:\n")
                    print((page.get("text") or "")[:1200])

        head("4. Each attempt by a worker (table dps_job_attempt)")
        for a in s.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_no)):
            print(f"attempt {a.attempt_no}  worker {a.worker_id}  {a.started_at:%H:%M:%S}  -> {a.outcome}  "
                  f"{a.error_code or ''}")
        steps = json.loads(job.steps_json or "[]")
        if steps:
            print("steps         " + " -> ".join(str(x.get("step") or x.get("name") or x) for x in steps)[:300])

        head("5. Model calls (table dps_ai_call; empty when the agent is off)")
        calls = list(s.scalars(select(AiCall).where(AiCall.job_id == job.id)))
        print(f"{len(calls)} call(s)" + (f", {sum(c.prompt_tokens for c in calls)} prompt tokens" if calls else ""))
        for c in calls[:8]:
            print(f"  {c.purpose:<14} {c.model:<28} {c.status}  {c.seconds:.1f} s")

        head("6. The result Spring pulls (table dps_run, JSON dps.result/1.0)")
        run = s.scalars(select(Run).where(Run.job_id == job.id).order_by(Run.run_no.desc())).first()
        if run is None:
            print("no result yet")
        else:
            r = json.loads(run.result_json)
            c = r.get("classification") or {}
            print(f"run {run.run_no}  status {run.status}  type {c.get('type')}  "
                  f"skill {(r.get('pipeline') or {}).get('skillPack')}")
            groups: dict[str, int] = {}
            for f in r.get("fields", []):
                groups[f["group"]] = groups.get(f["group"], 0) + 1
            print("fields        " + ", ".join(f"{k} {v}" for k, v in sorted(groups.items())))
            print(f"see it        GET /dps/v1/jobs/{job.id}/result")

        head("7. Events Spring reads (table dps_event_feed, GET /dps/v1/events)")
        for e in s.scalars(select(EventFeed).where(EventFeed.job_id == job.id).order_by(EventFeed.seq)):
            print(f"#{e.seq:<5} {e.occurred_at:%H:%M:%S}  {e.type}")

        head("8. Document history (table dps_trace_event)")
        for t in s.scalars(select(TraceEvent).where(TraceEvent.job_id == job.id).order_by(TraceEvent.at).limit(12)):
            print(f"{t.at:%H:%M:%S}  {t.step:<8} {t.summary[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

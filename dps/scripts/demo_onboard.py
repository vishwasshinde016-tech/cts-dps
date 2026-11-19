"""Demo: read one document the way an ONBOARD job does and print what DPS found.

No Spring, DMS or database is needed: the file is parsed into a page model and run through the same classifier,
covenant skill, candidate ledger and checks as a real job. Rules only by default; ``--agent`` adds the Document
Agent on the Azure deployment configured in ``.env`` (needs the VPN).

    python scripts/demo_onboard.py ../golden-set/documents/AGR-001.pdf
    python scripts/demo_onboard.py my-agreement.pdf --agent --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

FORMATS = {".pdf": "PDF", ".docx": "DOCX", ".xlsx": "XLSX", ".xls": "XLS", ".csv": "CSV"}


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")


class Tracer:
    """A readable run log: each DPS function the run enters (file and function, indented by call depth; a function
    called many times in a row is written once with a count) and every DPS log line, in order."""

    MAX_LINES = 6000

    def __init__(self, path: Path) -> None:
        import logging

        self.out = path.open("w", encoding="utf-8")
        self.lines, self.depth, self.last, self.repeat = 0, 0, None, 0
        self.root = str(Path(__file__).resolve().parents[1] / "src") + os.sep
        handler = logging.StreamHandler(self)
        handler.setFormatter(logging.Formatter("      LOG %(levelname)s %(name)s: %(message)s"))
        logger = logging.getLogger("dps")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.out.write("DPS run log: '->' enters a function (file:line function), 'LOG' is a log line\n\n")

    def write(self, text: str) -> None:  # logging handler stream
        self._flush_repeat()
        self.out.write(text)

    def flush(self) -> None:
        self.out.flush()

    def step(self, text: str) -> None:
        self._flush_repeat()
        self.out.write(f"\n=== {text}\n")

    def _flush_repeat(self) -> None:
        if self.repeat:
            self.out.write(f"{'  ' * min(self.depth, 30)}   (x{self.repeat + 1})\n")
            self.repeat = 0

    def _profile(self, frame, event, arg) -> None:
        if event not in ("call", "return"):
            return
        file = frame.f_code.co_filename
        if not file.startswith(self.root):
            return
        if event == "return":
            self.depth = max(0, self.depth - 1)
            return
        self.depth += 1
        name = frame.f_code.co_name
        if name.startswith("<") or self.lines >= self.MAX_LINES:
            return
        rel = file[len(self.root):].replace(os.sep, "/")
        key = (rel, name)
        if key == self.last:
            self.repeat += 1
            return
        self._flush_repeat()
        self.last = key
        self.out.write(f"{'  ' * min(self.depth, 30)}-> {rel}:{frame.f_code.co_firstlineno} {name}\n")
        self.lines += 1

    def start(self) -> None:
        sys.setprofile(self._profile)

    def stop(self) -> None:
        sys.setprofile(None)

    def close(self) -> None:
        self._flush_repeat()
        self.out.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--type", default="LOAN_AGREEMENT", help="the type the user chose (checked against the classifier)")
    ap.add_argument("--agent", action="store_true", help="also run the Document Agent (Azure, needs VPN and .env)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--json", type=Path, help="write the full dps.result JSON here")
    ap.add_argument("--trace", type=Path, help="write a run log: every DPS function entered, with its file, plus the "
                                               "DPS log lines (shows how a document travels through the code)")
    args = ap.parse_args()

    fmt = FORMATS.get(args.file.suffix.lower())
    if fmt is None:
        print(f"unsupported file type {args.file.suffix}; use one of {', '.join(FORMATS)}")
        return 2
    load_env(args.env)

    from dps.models import Job
    from dps.parse import page_model
    from dps.skills.runner import run_onboard

    data = args.file.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    tracer = Tracer(args.trace) if args.trace else None
    t = time.time()
    if tracer:
        tracer.step(f"1. PARSE {args.file.name} ({fmt}, sha256 {sha[:12]}...) into the page model")
        tracer.start()
    model = page_model.build(data, fmt, sha)
    gateway = None
    if args.agent:
        from dps.agent.gateway import from_settings
        from dps.settings import Settings

        gateway = from_settings(Settings())
    job = Job(id="demo", document_id=args.file.stem, job_type="ONBOARD", tenant_id="default", sha256=sha,
              document_version_id="demo-v1", request_json=json.dumps({"context": {"expectedType": args.type}}))
    if tracer:
        tracer.step(f"2. ONBOARD job: classify, pick the skill pack, extract, ledger, score "
                    f"({'rules + Document Agent' if gateway else 'rules only'})")
    result = run_onboard(job, model, gateway)
    secs = time.time() - t
    if tracer:
        tracer.stop()
        tracer.step(f"3. RESULT dps.result/1.0: {len(result['fields'])} field(s), route {result['document']['route']}, "
                    f"{secs:.1f} s")
        tracer.close()
        print(f"run log written to {args.trace} ({tracer.lines} lines)")

    c = result.get("classification") or {}
    print(f"\n{args.file.name}: {len(model.get('pages', []))} page(s), {secs:.1f} s, "
          f"{'rules + Document Agent' if gateway else 'rules only'}")
    print(f"classified as {c.get('type')} ({c.get('confidence')}), route {result['document']['route']}")
    print("\ncovenants:")
    for f in result["fields"]:
        if f["group"] not in ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "POSSIBLE_COVENANT", "EVENT_OF_DEFAULT"):
            continue
        v = f["value"]
        level = f"{v.get('operator') or ''} {'' if v.get('threshold') is None else v['threshold']}".strip()
        issues = ",".join(i["code"] for i in f.get("issues") or [])
        name = str(v.get("code") or v.get("metric"))[:30]
        print(f"  p{f['source'].get('page')!s:>3}  {f['group'][:14]:<14} {name:<30} "
              f"{level:<12} {f['method']:<18} {f['status']:<13} {issues}")
    led = (result["document"].get("coverage") or {}).get("ledger")
    if led:
        print(f"\ncoverage: {led['candidates']} duty sentence(s) in the whole document, {led['percentExplained']}% "
              f"explained, {led['unresolved']} unresolved (each a review item), 100% accounted")
    pages = result["document"].get("pages")
    if pages:
        print(f"pages: {pages['parsed']} text, {pages['ocr']} OCR, {pages['blank']} blank, {pages['unreadable']} "
              "unreadable")
    print("warnings: " + (", ".join(sorted({w["code"] for w in result["warnings"]})) or "none"))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print(f"full result written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

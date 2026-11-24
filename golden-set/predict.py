"""Runs DPS's own parser and extractors over the golden-set documents and writes one prediction per document.

    python golden-set/predict.py --out <dir>      # needs the DPS package (dps/.venv or `pip install -e dps`)
    python golden-set/evaluate.py --predictions <dir> --statuses SYNTHETIC

Agreements: the terms Flow A extracts ({"covenants": [...]} like a label). Statements: the line items Flow B
reads, in INR crore ({"values": {...}}). Only documents with a label are predicted.

``--pipeline`` (improvement plan Q1, 24 Nov 2026) runs agreements through the production path instead: the sandboxed
page model, the covenants skill pack (rules, ledger, merge, taxonomy stamp, scoring) exactly as a job does. With
``--replies <dir>`` the AI readers answer from recorded replies (``replay.py``), so a prompt or model change is
measured in CI without calling a model; without it the AI is off (rules only).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def predict_agreement(data: bytes) -> dict:
    from dps.extraction.agreement import extract_terms
    from dps.parse.engine import ParserEngine
    from dps.skills.covenants.obligation import first_test_date

    terms = extract_terms(ParserEngine().parse(data))
    return {"covenants": [{"metric": t.metric, "category": t.category, "operator": t.operator,
                           "threshold": t.threshold, "unit": t.unit, "frequency": t.frequency, "page": t.page,
                           "quote": t.quote, "firstTestDate": first_test_date(t.quote)} for t in terms]}


def predict_agreement_pipeline(document_id: str, data: bytes, gateway=None, parser=None) -> dict:
    """The covenants a job would propose (fields of the covenant groups), in the label's shape."""
    import hashlib

    from dps.models import Job
    from dps.ingest.pipeline import page_hint
    from dps.parse import sandbox
    from dps.skills import load
    from dps.skills import runner as skills

    request = sandbox.ParseRequest(data, "PDF", hashlib.sha256(data).hexdigest())
    model = parser(request, page_hint(data, "PDF")) if parser else sandbox.parse_in_process(request)
    job = Job(id=f"golden-{document_id}", document_id=document_id, job_type="EXTRACT_AGREEMENT", tenant_id="default",
              request_json="{}")
    result = skills.run(load("covenants"), job, model, gateway, None)
    groups = {"FINANCIAL_COVENANT": "FINANCIAL", "NON_FINANCIAL_COVENANT": "NON_FINANCIAL"}
    out = []
    for f in result["fields"]:
        if f["group"] not in groups or f.get("status") == "REJECTED":
            continue
        v = f.get("value") or {}
        out.append({"metric": v.get("metric"), "category": groups[f["group"]], "operator": v.get("operator"),
                    "threshold": v.get("threshold"), "unit": v.get("unit"),
                    "firstTestDate": (f.get("test") or {}).get("firstTestDate"),
                    "frequency": (f.get("test") or {}).get("frequency"), "page": (f.get("source") or {}).get("page"),
                    "quote": (f.get("source") or {}).get("quote"), "method": f.get("method")})
    return {"covenants": out, "warnings": [w["code"] for w in result.get("warnings", [])],
            "pipeline": result.get("pipeline", {}).get("version")}


def predict_statement(name: str, data: bytes) -> dict:
    from dps.extraction import statement as st

    if name.endswith(".pdf"):
        from dps.extraction.statement_pdf import read_pdf_statement
        from dps.parse.engine import RapidOcrBackend

        table = read_pdf_statement(data, RapidOcrBackend())
        rows, header = table.rows, table.header_text
    elif name.endswith(".csv"):
        rows, header = st.rows_from_csv(data), st.header_from_csv(data)
    elif name.endswith(".xls"):
        rows, header = st.rows_from_xls(data), st.header_from_xls(data)
    else:
        rows, header = st.rows_from_xlsx(data), st.header_from_xlsx(data)
    result = st.extract_values([tuple(r) for r in rows], header)
    return {"values": {v.key: v.value for v in result.values}, "issues": result.issues}


def main(argv: list[str] | None = None) -> int:
    from dps.parse import sandbox

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--labels", type=Path, default=HERE / "labels")
    ap.add_argument("--documents", type=Path, default=HERE / "documents")
    ap.add_argument("--pipeline", action="store_true", help="agreements through the production skills pipeline")
    ap.add_argument("--replies", type=Path, default=None, help="recorded AI replies for --pipeline (replay.py)")
    ap.add_argument("--record", action="store_true", help="with --replies: call the live model and save its replies")
    ap.add_argument("--env-file", type=Path, default=Path(".env"), help="local model configuration for recording")
    ap.add_argument("--document", action="append", help="predict only this document ID (repeatable)")
    ap.add_argument("--statuses", help="comma-separated label statuses to predict")
    ap.add_argument("--parallel", type=int, default=1, help="concurrent AI reads")
    ap.add_argument("--parser", choices=("cold", "warm"), default="cold", help="agreement parser for --pipeline")
    args = ap.parse_args(argv)
    if args.record and (not args.pipeline or args.replies is None or args.parallel != 1):
        ap.error("--record requires --pipeline, --replies and serial reads")
    warm = sandbox.WarmParser(sandbox.Limits()) if args.pipeline and args.parser == "warm" else None
    try:
        return _predict(args, warm.parse if warm else
                        lambda request, hint: sandbox.run(request, pages_hint=hint))
    finally:
        if warm:
            warm.close()


def _predict(args, parser) -> int:
    from dps.agent import usage
    from dps.settings import Settings

    gateway = None
    if args.replies is not None:
        from replay import replay_gateway

        record_with = None
        if args.record:  # pragma: no cover - live model
            from dps.agent.gateway import from_settings
            from dps.settings import export_model_env

            export_model_env(args.env_file)
            live = from_settings(Settings(_env_file=args.env_file))
            record_with = lambda route: (lambda messages: live.call(route, messages))  # noqa: E731
        gateway = replay_gateway(args.replies, record_with, parallel=args.parallel)
    args.out.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    costs: dict[str, dict] = {}
    settings = Settings(_env_file=args.env_file)
    statuses = {status.strip() for status in args.statuses.split(",")} if args.statuses else None
    for label_path in sorted(args.labels.glob("*.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        doc_id = label["documentId"]
        if (args.document and doc_id not in args.document) or (statuses and label.get("status") not in statuses):
            continue
        source = args.documents / label["sourceFile"]
        if not source.exists():  # the first ten keep their original file name in the label
            source = args.documents / f"{label['documentId']}.pdf"
        if not source.exists():
            continue
        data = source.read_bytes()
        started = time.perf_counter()
        usage.begin()
        if label.get("type") == "FINANCIAL_STATEMENT":
            prediction = predict_statement(source.name, data)
        elif args.pipeline:
            prediction = predict_agreement_pipeline(label["documentId"], data, gateway, parser)
        else:
            prediction = predict_agreement(data)
        timings[doc_id] = round(time.perf_counter() - started, 3)
        tokens = usage.take()
        if tokens and tokens.calls:
            costs[doc_id] = {"inputTokens": tokens.prompt, "outputTokens": tokens.completion,
                             "cachedInputTokens": sum(call.cached_tokens for call in tokens.detail),
                             "estimatedCost": usage.cost(settings, tokens)}
        (args.out / label_path.name).write_text(json.dumps({"documentId": label["documentId"], **prediction},
                                                           indent=1), encoding="utf-8")
    # Improvement plan V3: speed next to quality, with the machine it ran on, so two runs compare like with like.
    if args.document and set(args.document) - timings.keys():
        raise SystemExit("A requested document or its label is missing")
    from evaluate import evaluate

    quality = evaluate(args.labels, args.out, statuses=("SYNTHETIC", "VERIFIED"),
                       documents=list(timings))["overall"]
    (args.out / "_benchmark.json").write_text(json.dumps({
        "mode": "pipeline" if args.pipeline else "extractor", "replies": bool(args.replies),
        "liveRecording": args.record, "parallel": args.parallel, "parser": args.parser,
        "quality": quality, "modelUsage": costs,
        "documents": len(timings), "seconds": round(sum(timings.values()), 2),
        "slowest": sorted(timings.items(), key=lambda kv: -kv[1])[:5], "perDocument": timings,
        "machine": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "aiCalls": (gateway.replies.hits + gateway.replies.recorded) if gateway is not None else 0,
    }, indent=1), encoding="utf-8")
    if gateway is not None:
        r = gateway.replies
        print(json.dumps({"replies": {"hits": r.hits, "misses": r.misses, "recorded": r.recorded,
                                      "errors": r.errors}}))
        if r.misses or r.errors or not (r.hits + r.recorded):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

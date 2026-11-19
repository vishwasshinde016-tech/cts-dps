"""Run held-out documents through DPS's real flows, as production does, and save what each would publish.

Run inside a DPS worker container (Flow A needs Core for the covenant library, read only). Every document goes into
a throwaway SQLite database, never the running one. A document the standard pool hands to the heavy pool
(outcome HEAVY) is run again with ``--heavy`` in the dps-worker-heavy container, as the platform would.

  python run_flows.py <out.json> <kind: agreement|statement> <pdf>... [--heavy] [--ai]

--ai turns on the optional AI readers (DPS_AGREEMENT_AI_CHECK / DPS_STATEMENT_AI_CHECK) with DPS_AI_MODEL.
"""
import json
import os
import sys
import tempfile
import time
from datetime import date

from dps.db import Base, Database
from dps.models import Document, DocumentCpLink
from dps.settings import get_settings

AS_OF = date(2025, 3, 31)  # period end; only used for the borrower's earlier figures, of which there are none


def deps_for(kind: str, database: Database, heavy: bool, ai: bool):
    from dps.parse.engine import DoclingConverter, HeavyEngine, ParserEngine, RapidOcrBackend

    settings = get_settings()
    ocr = RapidOcrBackend()
    parser = HeavyEngine(ParserEngine(ocr), DoclingConverter()) if heavy else ParserEngine(ocr)
    completion = None
    if ai:
        from dps.advisory.advisor import crewai_completion

        completion = crewai_completion(settings.ai_model, settings.ai_temperature, settings.ai_timeout_seconds)
    if kind == "agreement":
        from dps.integrations.core import CoreClient
        from dps.extraction.agreement_ai import read as read_covenants
        from dps.flows.intake import AgreementIntakeFlow, IntakeDeps

        reader = (lambda pages, terms: read_covenants(pages, terms, completion)) if ai else None
        deps = IntakeDeps(database.session_factory, read_file, parser, CoreClient.from_settings(settings),
                          settings.review_confidence, heavy, reader)
        return AgreementIntakeFlow, deps
    from dps.extraction.statement_ai import check
    from dps.extraction.statement_pdf import read_pdf_statement
    from dps.flows.statement import StatementDeps, StatementProcessingFlow

    second = (lambda text, values: check(text, values, completion, settings.statement_entity)) if ai else None
    deps = StatementDeps(database.session_factory, read_file, parser, settings.core_data_source_id, heavy,
                         lambda data: read_pdf_statement(data, ocr, entity=settings.statement_entity), second)
    return StatementProcessingFlow, deps


def read_file(doc: Document) -> bytes:
    return open(doc.storage_path, "rb").read()


def add(database: Database, kind: str, path: str) -> str:
    with database.session_factory() as s:
        d = Document(type="LOAN_AGREEMENT" if kind == "agreement" else "FINANCIAL_STATEMENT",
                     file_name=os.path.basename(path), storage_path=path, sha256=os.path.basename(path), size_bytes=1,
                     uploaded_by="held-out", as_of_date=None if kind == "agreement" else AS_OF)
        s.add(d)
        s.flush()
        s.add(DocumentCpLink(document_id=d.id, cp_id="CP-HELD-OUT-" + os.path.basename(path), linked_by="held-out"))
        s.commit()
        return d.id


def run(kind: str, path: str, heavy: bool, ai: bool) -> dict:
    tmp = tempfile.mkdtemp()
    database = Database(f"sqlite:///{tmp}/ho.db")
    Base.metadata.create_all(database.engine)
    doc_id = add(database, kind, path)
    flow_cls, deps = deps_for(kind, database, heavy, ai)
    flow = flow_cls(deps)
    started = time.time()
    flow.kickoff(inputs={"job_id": "held-out", "document_id": doc_id})
    st = flow.state
    out = {"outcome": st.outcome, "error": st.error, "seconds": round(time.time() - started, 1), "heavy": heavy,
           "ai": ai}
    if kind == "agreement":
        out["proposals"] = [{k: p.get(k) for k in ("metric_raw", "name", "category", "operator", "threshold", "unit",
                                                   "confidence", "method", "issues", "page", "quote")}
                            for p in st.proposals]
    else:
        out["values"] = st.values
        out["issues"] = st.issues
        out["likely"] = st.likely
        out["confirmed"] = st.confirmed
        out["unit"] = st.unit_scale
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    heavy, ai = "--heavy" in sys.argv, "--ai" in sys.argv
    out_path, kind, pdfs = args[0], args[1], args[2:]
    results = json.load(open(out_path)) if os.path.exists(out_path) else {}
    for path in pdfs:
        doc = os.path.splitext(os.path.basename(path))[0]
        try:
            results[doc] = run(kind, path, heavy, ai)
        except Exception as ex:  # noqa: BLE001 - one bad document must not stop the run
            results[doc] = {"outcome": "CRASH", "error": f"{type(ex).__name__}: {str(ex)[:300]}", "heavy": heavy,
                            "ai": ai}
        r = results[doc]
        print(doc, r["outcome"], r.get("seconds"), len(r.get("proposals", r.get("values", []))), flush=True)
        json.dump(results, open(out_path, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()

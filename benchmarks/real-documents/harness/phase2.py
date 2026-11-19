"""Phase 2 on the benchmark with the live model (run inside the DPS worker, which has the Azure settings).

  python phase2.py agreements  -> r_p2_ag.json   (cached pages ag_pages.json; rules, safety net, AI vote)
  python phase2.py statements  -> r_p2_st.json   (stmt_pages/*.pdf; table reader + grounded second reader)
"""
import json
import os
import sys
import time

from dps.advisory.advisor import crewai_completion
from dps.extraction.agreement import extract_terms
from dps.extraction.agreement_ai import ai_covenants, flag_unconfirmed, vote
from dps.extraction.safety_net import possible_covenants
from dps.parse.engine import Page

completion = crewai_completion(os.environ["DPS_AI_MODEL"], None, 120)


def term_dict(t):
    return {"metric": t.metric, "category": t.category, "operator": t.operator, "threshold": t.threshold,
            "unit": t.unit, "page": t.page, "method": t.method, "issues": t.issues, "name": t.name,
            "confidence": t.confidence, "quote": t.quote[:300]}


def agreements():
    out_path = sys.argv[2] if len(sys.argv) > 2 else "r_p2_ag.json"
    res = json.load(open(out_path)) if os.path.exists(out_path) else {}
    for doc, raw in sorted(json.load(open("ag_pages.json")).items()):
        if doc in res:
            continue
        pages = [Page(*p) for p in raw]
        terms = extract_terms(pages)
        cands, _ = possible_covenants(pages, terms)
        before = [term_dict(t) for t in terms + cands]
        t0 = time.perf_counter()
        try:
            ai, dropped = ai_covenants(pages, terms + cands, completion)
            voted = flag_unconfirmed(vote(terms + cands, ai))
            rec = {"before": before, "ai": [term_dict(a) for a in ai], "dropped": dropped,
                   "after": [term_dict(t) for t in voted]}
        except Exception as ex:  # noqa: BLE001
            rec = {"before": before, "error": f"{type(ex).__name__}: {ex}"[:200]}
        rec["seconds"] = round(time.perf_counter() - t0, 1)
        res[doc] = rec
        json.dump(res, open(out_path, "w"), indent=1)
        print(doc, len(rec.get("ai", [])), rec.get("dropped"), rec["seconds"], rec.get("error", ""), flush=True)


def statements():
    from dps.extraction.statement import extract_values
    from dps.extraction.statement_ai import ai_read, mark_pages, vote as svote
    from dps.extraction.statement_pdf import read_pdf_statement
    from dps.parse.engine import RapidOcrBackend

    ocr = RapidOcrBackend()
    out_path = "r_p2_st.json"
    res = json.load(open(out_path)) if os.path.exists(out_path) else {}
    for name in sorted(os.listdir("stmt_pages")):
        doc = name[:-4]
        if doc in res or not name.endswith(".pdf"):
            continue
        table = read_pdf_statement(open(f"stmt_pages/{name}", "rb").read(), ocr)
        result = extract_values(table.rows, table.header_text)
        text = mark_pages(list(zip(table.pages, table.texts, strict=False)))
        t0 = time.perf_counter()
        try:
            readings = ai_read(text, completion)
            r = svote(result.values, readings)
            rec = {"values": {v.key: v.value for v in result.values if not v.derived},
                   "readings": [vars(x) for x in readings], "issues": r.issues, "confirmed": r.confirmed,
                   "suggestions": r.suggestions, "dropped": r.dropped}
        except Exception as ex:  # noqa: BLE001
            rec = {"values": {v.key: v.value for v in result.values if not v.derived},
                   "error": f"{type(ex).__name__}: {ex}"[:200]}
        rec["seconds"] = round(time.perf_counter() - t0, 1)
        res[doc] = rec
        json.dump(res, open(out_path, "w"), indent=1)
        print(doc, len(rec.get("confirmed", [])), len(rec.get("issues", [])), rec.get("dropped"), rec["seconds"],
              rec.get("error", ""), flush=True)


if __name__ == "__main__":
    {"agreements": agreements, "statements": statements}[sys.argv[1]]()

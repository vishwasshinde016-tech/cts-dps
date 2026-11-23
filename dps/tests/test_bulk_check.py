"""scripts/bulk_check.py: the job each file gets, the report row and the summary (the run itself is the real worker)."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bulk_check.py"
spec = importlib.util.spec_from_file_location("bulk_check", SCRIPT)
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)

ARGS = Namespace(job_type="ONBOARD", as_of="2024-03-31")


def test_pdfs_run_as_onboard_and_spreadsheets_as_statements_with_their_period_end():
    assert bulk.job_for(Path("a.pdf"), {}, ARGS)["jobType"] == "ONBOARD"
    sheet = bulk.job_for(Path("fs.xlsx"), {}, ARGS)
    assert sheet["jobType"] == "EXTRACT_STATEMENT" and sheet["context"]["asOfDate"] == "2024-03-31"
    row = {"jobType": "EXTRACT_STATEMENT", "asOfDate": "2025-03-31", "cpId": "CP-9",
           "expectedType": "FINANCIAL_STATEMENT"}
    job = bulk.job_for(Path("fs.pdf"), row, ARGS)
    assert job["context"] == {"tenantId": "default", "cpIds": ["CP-9"], "requestedBy": "bulk-check",
                              "asOfDate": "2025-03-31", "expectedType": "FINANCIAL_STATEMENT"}


def test_the_report_row_counts_what_was_found_and_what_is_weak(tmp_path):
    result = {"classification": {"type": "LOAN_AGREEMENT"}, "usage": {"model_calls": 3, "tokens": 900},
              "document": {"coverage": {"percent": 90.0, "unexplainedCount": 2}},
              "warnings": [{"code": "COVERAGE_GAP"}],
              "fields": [{"group": "FINANCIAL_COVENANT", "value": {"code": "FIN_DSCR"}, "confidence": 0.97},
                         {"group": "POSSIBLE_COVENANT", "value": {"code": "AFF_OTHER"}, "confidence": 0.77,
                          "method": "AI_AGENT"},
                         {"group": "POSSIBLE_COVENANT", "value": {}, "confidence": 0.3, "method": "COVERAGE"},
                         {"group": "SCHEDULE_TERM", "value": {"kind": "REPAYMENT"}, "confidence": 0.85}]}
    row = bulk.report_row(Path("a.pdf"), {"jobType": "ONBOARD"}, {"status": "NEEDS_REVIEW"}, result, 4.24, ["x"])
    # the coverage ledger's "look here" pointer is counted on its own, not as a covenant or an AI finding
    found = (row["covenants"], row["financial"], row["aiFound"], row["lookHere"], row["scheduleTerms"])
    assert found == (2, 1, 1, 1, 1)
    assert (row["genericCodes"], row["lowConfidence"], row["coveragePct"], row["contractProblems"]) == (1, 2, 90.0, 1)
    bulk.write_summary(tmp_path, [row])
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Look at these first" in summary and "**a.pdf**" in summary
    assert (tmp_path / "summary.csv").read_text(encoding="utf-8").startswith("file,jobType,status")

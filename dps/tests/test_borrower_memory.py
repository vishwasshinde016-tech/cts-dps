"""Phase 3 of "never miss silently": lines matched by meaning, and borrower memory of confirmed wordings."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from test_api import session

from dps.extraction.similarity import canonical, nearest, similarity
from dps.extraction.statement import likely_lines
from dps.models import AuditEvent, LabelMemory


def test_new_wordings_are_matched_by_meaning():
    for label, key in [("Net cash inflow from trading activities", "CFO"),
                       ("Term loans - non-current portion", "LT_DEBT"), ("Stock-in-trade", "INVENTORY"),
                       ("Sundry debtors", "RECEIVABLES"),
                       ("Finance charges", "INTEREST_PAID"), ("Depreciation and amortization", "DEPRECIATION"),
                       ("Total shareholders' funds", "NET_WORTH"),
                       ("Notes payable, less current maturities", "LT_DEBT")]:
        got, score = nearest(label)
        assert (got, score >= 0.6) == (key, True), label
    assert canonical("(ii) Borrowings (refer note 12)") == "borrowings"
    assert similarity("Amortization", "amortisation") == 1.0


def test_a_meaning_match_outranks_a_word_match_for_a_missing_line():
    # A wording the catalog does not know (the catalog now reads "Borrowings - non-current" itself).
    rows = [("Borrowings from banks for working capital", 50.0, 2, None),
            ("Term loans - non-current portion", 300.0, 2, None)]
    [lt] = [x for x in likely_lines(rows, found=set(), factor=1.0) if x["key"] == "LT_DEBT"]
    assert lt["label"] == "Term loans - non-current portion"


def test_a_confirmed_wording_is_remembered_and_used_for_the_next_statement(client, app):
    from statement_shim import StatementDeps, StatementProcessingFlow, as_result
    from test_flows import add_document
    from test_statement_pdf import FakeParser

    from dps.db import Database
    from dps.extraction.statement_pdf import PdfStatement
    from dps.services import remember_label

    with session(app) as s:  # a reviewer confirmed this wording as LT_DEBT for CP-1 (the review API does this)
        remember_label(s, "CP-1", "Term loans - non-current portion 300.0", "LT_DEBT", None, "rm-anil")
        s.commit()
    with session(app) as s:
        [memory] = s.scalars(select(LabelMemory)).all()
        assert (memory.cp_id, memory.label, memory.key, memory.confirmed_by) == (
            "CP-1", "term borrowings longterm portion", "LT_DEBT", "rm-anil")
        assert s.scalars(select(AuditEvent).where(AuditEvent.action == "LABEL_REMEMBERED")).one()

    # Next year: the same wording (in brackets this time) maps on its own; another wording does not.
    database = Database(app.state.settings.database_url)
    rows = [("Term loans (non-current portion)", 280.0, 2, None), ("EBITDA", 50.0, 1, None),
            ("Net worth", 300.0, 1, None), ("Total debt", 280.0, 1, None)]
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fy27.pdf", date(2027, 3, 31), ("CP-1",))
    deps = StatementDeps(database.session_factory, lambda d: b"%PDF", FakeParser([]), "ds-dps", False,
                         lambda d: PdfStatement(rows, "Rs. in crore", [], None, ["p"], [1]))
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j2", "document_id": doc_id})
    values = {v["key"]: v["value"] for v in flow.state.values}
    assert values["LT_DEBT"] == 280.0 and flow.state.remembered == ["LT_DEBT"]
    fields, _, extra = as_result(flow.state, date(2027, 3, 31))
    assert extra["statement"]["remembered"] == ["LT_DEBT"]  # the reviewer sees why the line was read so

    # Another CP's statement with the same wording learns nothing from CP-1.
    other = add_document(database, "FINANCIAL_STATEMENT", "x.pdf", date(2027, 3, 31), ("CP-2",))
    flow2 = StatementProcessingFlow(deps)
    flow2.kickoff(inputs={"job_id": "j3", "document_id": other})
    assert "LT_DEBT" not in {v["key"] for v in flow2.state.values if not v["derived"]}


def test_memory_that_disagrees_with_the_catalog_holds_the_statement(client, app):
    from statement_shim import StatementDeps, StatementProcessingFlow
    from test_flows import add_document
    from test_statement_pdf import FakeParser

    from dps.db import Database
    from dps.extraction.statement_pdf import PdfStatement
    from dps.services import remember_label

    with session(app) as s:
        remember_label(s, "CP-1", "Short-term borrowings", "LT_DEBT", None, "rm-anil")  # a wrong confirmation
        s.commit()
    database = Database(app.state.settings.database_url)
    rows = [("Short-term borrowings", 40.0, 2, None), ("EBITDA", 50.0, 1, None), ("Net worth", 300.0, 1, None),
            ("Total debt", 40.0, 1, None)]
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fy27.pdf", date(2027, 3, 31), ("CP-1",))
    deps = StatementDeps(database.session_factory, lambda d: b"%PDF", FakeParser([]), "ds-dps", False,
                         lambda d: PdfStatement(rows, "Rs. in crore", [], None, ["p"], [1]))
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j4", "document_id": doc_id})
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert flow.state.issues == ["LABEL_MEMORY_CONFLICT: 'Short-term borrowings' was confirmed as LT_DEBT, "
                                 "now read as ST_DEBT"]

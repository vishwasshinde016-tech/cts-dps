"""Covenant recall (18 Nov 2026 review): the agent sees the covenant sections, duty pages and definitions, not only the
pages the rules used; its tools reach the whole document; quotes may cross a page break; a repaired answer with one
bad quote keeps its good items."""

from __future__ import annotations

from test_phase3_analyze import model_of
from test_phase4_skills import fake_job

from dps.agent.document_agent import FindDefinitionTool, PageTextTool, SearchTextTool, ToolContext
from dps.agent.gateway import JobBudget, ModelOutputInvalid
from dps.skills import load
from dps.skills import runner as skills
from dps.skills.common import agent_proposals, grounded
from dps.skills.covenants.pages import covenant_pages, duty_sentences

FILLER = "This page describes the parties, the recitals and the general background of the arrangement."


def pages_of(texts: dict[int, str], total: int) -> list[dict]:
    return [{"no": n, "text": texts.get(n, FILLER), "quality": 0.95} for n in range(1, total + 1)]


# ---------- the page finder ----------

def test_a_covenant_section_far_from_any_rule_find_is_sent():
    doc = pages_of({2: "ARTICLE 1 DEFINITIONS AND INTERPRETATION\n\"EBITDA\" means earnings before interest.\n"
                        "\"Total Debt\" means all borrowings.\n\"Interest Coverage\" means EBITDA to interest.",
                    31: "ARTICLE 9 NEGATIVE COVENANTS\n9.1 The Borrower shall not permit its gearing to exceed 2.",
                    32: "9.2 The Borrower shall not create any lien over its assets.",
                    40: "SCHEDULE 6 FORM OF COMPLIANCE CERTIFICATE\nWe confirm the ratios below."}, 45)
    rule_find = [{"source": {"page": 12}}]
    chosen, note = covenant_pages(doc, rule_find, max_pages=25)
    sent = {p["no"] for p in chosen}
    assert {12, 31, 32, 40, 41, 2} <= sent  # rules, section, run-on page, certificate schedule, definitions
    whys = {s["page"]: s["why"] for s in note["sent"]}
    assert whys[12] == "RULES" and whys[31] == "SECTION" and whys[2] == "DEFINITIONS"
    assert 7 not in sent  # plain filler pages are not sent


def test_duty_pages_rank_by_how_many_duties_they_hold_and_the_budget_is_kept():
    duty = "The Borrower shall maintain a ratio of not less than 1.25 times. "
    doc = pages_of({5: duty * 6, 9: duty, 20: duty * 3}, 30)
    chosen, note = covenant_pages(doc, [], max_pages=2)
    assert [p["no"] for p in chosen] == [5, 20]  # the two pages with the most duties, in page order
    assert note["notSent"] == [9]  # left out, and said so
    big = pages_of({n: duty * 300 for n in range(1, 6)}, 5)
    chosen, _ = covenant_pages(big, [], max_pages=25, max_chars=40_000)
    assert sum(len(p["text"]) for p in chosen) <= 40_000  # the prompt budget holds
    assert len(chosen) >= 1


def test_a_document_with_no_signal_sends_its_first_pages_and_empty_input_is_safe():
    chosen, note = covenant_pages(pages_of({}, 40), [], max_pages=5)
    assert [p["no"] for p in chosen] == [1, 2, 3, 4, 5] and note["sent"][0]["why"] == "FIRST_PAGES"
    assert covenant_pages([], [], 25) == ([], {"sent": [], "notSent": []})
    assert duty_sentences("The Borrower shall maintain a Current Ratio of at least 1.1.") == 1
    assert duty_sentences(FILLER) == 0


# ---------- the agent's tools reach the whole document ----------

def test_tools_search_and_open_pages_that_were_not_in_the_prompt():
    doc = pages_of({3: "\"EBITDA\" means profit before interest, tax, depreciation and amortisation.",
                    44: "The ratios in Schedule 7 apply."}, 50)
    ctx = ToolContext(doc, limit=5)
    assert "page 44" in SearchTextTool(ctx)._run("Schedule 7")
    assert PageTextTool(ctx)._run(44).startswith("The ratios")
    assert "page 3" in FindDefinitionTool(ctx)._run("EBITDA")
    assert "1 to 50" in PageTextTool(ctx)._run(99)


# ---------- grounding ----------

def test_grounding_allows_a_neighbour_page_a_page_break_and_hyphenated_lines():
    doc = [{"no": 1, "text": "The Borrower shall maintain a Debt Service Coverage"},
           {"no": 2, "text": "Ratio of not less than 1.25 times.\nIt shall keep its Net Worth above INR 500 cr-\nore."},
           {"no": 3, "text": "Other text."}]
    assert grounded(doc, 1, "Debt Service Coverage Ratio of not less than 1.25 times")  # across the break
    assert grounded(doc, 3, "Ratio of not less than 1.25 times")  # page number one off
    assert grounded(doc, 2, "Net Worth above INR 500 crore")  # hyphenated line end
    assert not grounded(doc, 2, "a ratio of not less than 9.99 times")
    assert not grounded(doc, 7, "Other text.")  # no such page
    assert not grounded(doc, 1, "abc")  # too short to mean anything


# ---------- a repaired answer keeps its grounded items ----------

class Salvage:
    """call_json gives up after the repair, with the last (schema-valid) answer attached, like the real gateway."""

    def __init__(self, items):
        self.items, self.routes = items, []

    def call_json(self, route, messages, schema, check):
        self.routes.append(route)
        answer = {"items": self.items}
        problem = check(answer)
        if problem:
            raise ModelOutputInvalid(problem, answer)
        return answer

    def begin_job(self, limit=None):
        return JobBudget(limit or 1000)

    def end_job(self):
        return None


def test_one_bad_quote_no_longer_throws_away_the_good_covenants():
    page = "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times."
    good = {"name": "DSCR", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.25 times",
            "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.25}}
    bad = {"name": "ICR", "page": 1, "quote": "Interest Coverage Ratio of not less than 2.00 times",
           "value": {"metric": "ICR", "operator": ">=", "threshold": 2.0}}
    notes: list[dict] = []
    out = agent_proposals(load("sanction"), Salvage([good, bad]), [{"no": 1, "text": page}], notes=notes)
    assert [p["name"] for p in out] == ["DSCR"]
    assert notes[0]["code"] == "AI_UNGROUNDED" and "1 answer" in notes[0]["message"]
    notes = []
    assert agent_proposals(load("sanction"), Salvage([bad]), [{"no": 1, "text": page}], notes=notes) is None
    assert notes[0]["code"] == "AI_UNGROUNDED"  # nothing grounded: the rules' answer stands


def test_a_quote_from_a_page_outside_the_prompt_is_kept_when_it_is_on_that_page():
    doc = [{"no": 1, "text": FILLER}, {"no": 2, "text": "The Current Ratio shall be at least 1.10 times."}]
    item = {"name": "CURRENT_RATIO", "page": 2, "quote": "Current Ratio shall be at least 1.10 times",
            "value": {"metric": "CURRENT_RATIO"}}
    out = agent_proposals(load("sanction"), Salvage([item]), doc[:1], all_pages=doc)
    assert out and out[0]["page"] == 2


# ---------- end to end: the covenants skill sends the far covenant section to the agent ----------

class Recorder(Salvage):
    def __init__(self):
        super().__init__([])
        self.prompts = []

    def call_json(self, route, messages, schema, check):
        self.prompts.append(messages[0]["content"])
        return {"items": []}


def test_the_covenants_skill_prompt_holds_the_far_section_and_says_when_pages_did_not_fit():
    texts = [FILLER] * 40
    texts[0] = "FINANCIAL COVENANTS\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20."
    texts[35] = "ARTICLE 12 INFORMATION UNDERTAKINGS\nThe Borrower shall deliver audited accounts within 180 days."
    gw = Recorder()
    result = skills.run(load("covenants"), fake_job("ONBOARD"), model_of(*texts), gw, None)
    assert any("<<<page 36>>>" in p and "INFORMATION UNDERTAKINGS" in p for p in gw.prompts)
    assert not [w for w in result["warnings"] if w["code"] == "PAGES_NOT_SENT"]
    many = [("The Borrower shall maintain a ratio of not less than 1.25 times. " * 40)] * 100  # > 4 batches
    result = skills.run(load("covenants"), fake_job("ONBOARD"), model_of(*many), Recorder(), None)
    assert [w for w in result["warnings"] if w["code"] == "PAGES_NOT_SENT"]


# ---------- one-to-one vote; non-financial merge per clause area ----------

def rule(metric, threshold, page, basis=None, op=">="):
    return {"name": metric, "value": {"metric": metric, "operator": op, "threshold": threshold}, "method": "REGEX",
            "confidence": 0.8, "issues": [], "status": "NEEDS_REVIEW", "source": {"page": page, "quote": "q"},
            "test": {"basis": basis}}


def agent(metric, threshold, page, basis=None, op=">="):
    return {"name": metric, "page": page, "quote": "q",
            "value": {"metric": metric, "operator": op, "threshold": threshold, "basis": basis}}


def test_two_covenants_on_the_same_metric_each_get_their_own_match():
    company, group = rule("DSCR", 1.25, 3, "STANDALONE"), rule("DSCR", 1.10, 7, "CONSOLIDATED")
    proposals = [agent("DSCR", 1.10, 7, "CONSOLIDATED"), agent("DSCR", 1.25, 3, "STANDALONE")]
    skills._vote(load("covenants"), [company, group], proposals)
    assert company["method"] == group["method"] == "RULES+AI_CONFIRMED"
    assert not company["issues"] and not group["issues"]  # before: one of them was flagged MODELS_DISAGREE
    assert all(p["_matched"] for p in proposals)


def test_a_different_number_is_flagged_on_the_right_covenant_and_a_repeat_changes_nothing():
    company, group = rule("DSCR", 1.25, 3, "STANDALONE"), rule("DSCR", 1.10, 7, "CONSOLIDATED")
    proposals = [agent("DSCR", 1.25, 3, "STANDALONE"), agent("DSCR", 1.05, 7, "CONSOLIDATED"),
                 agent("DSCR", 1.25, 3, "STANDALONE")]
    skills._vote(load("covenants"), [company, group], proposals)
    assert company["method"] == "RULES+AI_CONFIRMED" and not company["issues"]
    assert [i["code"] for i in group["issues"]] == ["MODELS_DISAGREE"]
    assert proposals[2]["_matched"]  # the repeated item is not a new agent-only covenant
    extra = skills._agent_only(load("covenants"), [{"no": 3, "text": "q"}], [company, group], proposals)
    assert extra == []


def test_reporting_duties_merge_per_clause_but_far_apart_duties_stay_separate():
    from dps.skills.covenants import names

    items = [{"name": "Annual accounts", "page": 5, "value": {"metric": "ANNUAL_FINANCIAL_STATEMENTS"}},
             {"name": "Quarterly accounts", "page": 5, "value": {"metric": "MANAGEMENT_ACCOUNTS"}},
             {"name": "Breach notice", "page": 6, "value": {"metric": "BREACH_NOTICE"}},
             {"name": "Lender's information request", "page": 31, "value": {"metric": "INFORMATION_REQUEST"}}]
    out = names.clean(items)
    assert [(p["value"]["metric"], p["page"]) for p in out] == [("REPORTING_COVENANT", 5), ("REPORTING_COVENANT", 31)]


# ---------- coverage: every duty sentence on the covenant pages is explained or listed ----------

def test_coverage_counts_explained_duties_and_lists_the_rest():
    from dps.skills.covenants import coverage

    page = ("The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times. "
            "The Borrower shall not change the general nature of its business without consent of the Lender. "
            "The Borrower may request a drawdown by giving not less than 5 days notice. "
            "\"EBITDA\" shall mean profit before interest and tax.")
    field = {"source": {"page": 4, "quote": "Debt Service Coverage Ratio of not less than 1.25 times"}}
    cov = coverage.check([{"no": 4, "text": page}], [field])
    assert cov["dutySentences"] == 2  # the drawdown mechanic and the definition do not count
    assert cov["explained"] == 1 and cov["percent"] == 50.0
    assert cov["unexplained"][0]["page"] == 4 and "general nature" in cov["unexplained"][0]["text"]
    assert coverage.check([], [])["percent"] == 100.0


def test_a_covenant_job_reports_coverage_and_a_gap_warning():
    texts = ["NEGATIVE COVENANTS\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 "
             "times.\nThe Borrower shall not permit any change in its business without the consent of the Lender."]
    result = skills.run(load("covenants"), fake_job("ONBOARD"), model_of(*texts), None, None)
    cov = result["document"]["coverage"]
    assert cov["dutySentences"] >= 2 and cov["explained"] >= 1
    gaps = [w for w in result["warnings"] if w["code"] == "COVERAGE_GAP"]
    if cov["unexplainedCount"]:
        assert gaps and "page" in gaps[0]["message"]


# ---------- EXTRACT_AGREEMENT reads with the covenants skill ----------

def test_the_older_agreement_flow_reads_with_the_covenants_skill():
    from dps.extraction.agreement import extract_terms
    from dps.extraction.agreement_ai import read_with_skill
    from dps.parse.engine import Page

    text = ("FINANCIAL COVENANTS\n"
            "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times.\n"
            "The Borrower shall maintain an Interest Coverage Ratio of not less than 2.00 times.\n"
            "NEGATIVE COVENANTS\n"
            "The Borrower shall not create any lien over its assets without prior written consent of the Lender.\n"
            "The Borrower shall not change the general nature of its business.\n")
    pages = [Page(1, text, "TEXT", 1.0)]
    terms = extract_terms(pages)
    items = [
        {"name": "DSCR", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.25 times",
         "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.25, "unit": "TIMES"}},
        {"name": "Interest Coverage Ratio", "page": 1, "quote": "Interest Coverage Ratio of not less than 2.00 times",
         "value": {"metric": "ICR", "operator": ">=", "threshold": 2.0, "unit": "TIMES"}},
        {"name": "Negative pledge", "page": 1, "quote": "shall not create any lien over its assets",
         "value": {"metric": "NEGATIVE_PLEDGE"}},
        {"name": "No change of business", "page": 1, "quote": "shall not change the general nature of its business",
         "value": {"metric": "RESTRICTION_ON_CHANGE_OF_BUSINESS"}},
        {"name": "Drawdown notice", "page": 1, "quote": "shall maintain a Debt Service Coverage Ratio",
         "value": {"metric": "DRAWDOWN_NOTICE"}}]
    voted = read_with_skill(pages, terms, Salvage(items))
    by = {(t.metric, t.category): t for t in voted}
    assert by[("DSCR", "FINANCIAL")].method == "REGEX+AI"
    assert by[("ICR", "FINANCIAL")].method in ("REGEX+AI", "AI")
    pledge = by.get(("NEGATIVE_PLEDGE", "NON_FINANCIAL"))
    assert pledge is not None and pledge.method in ("REGEX+AI", "AI")  # kept, not dropped for having no number
    change = by.get(("NEG_CHANGE_OF_BUSINESS", "NON_FINANCIAL"))  # a taxonomy code now, not a candidate
    assert change is not None and "general nature" in (change.quote or "")
    assert not any("DRAWDOWN" in str(t.metric) for t in voted)  # a loan mechanic is not a covenant
    assert read_with_skill(pages, terms, None) == terms  # no model: the rules' terms unchanged


def test_skill_terms_apply_the_older_sense_checks():
    from dps.extraction.agreement_ai import skill_terms
    from dps.parse.engine import Page

    pages = [Page(1, "Cash Flow Coverage of not less than 125% and leverage of no greater than 70%.", "TEXT", 1.0)]
    items = [{"name": "Cash flow coverage", "page": 1, "quote": "Cash Flow Coverage of not less than 125%",
              "value": {"metric": "DSCR", "operator": ">=", "threshold": 125}},
             {"name": "Leverage", "page": 1, "quote": "leverage of no greater than 70%",
              "value": {"metric": "DEBT_TO_EBITDA", "operator": "<=", "threshold": 70}}]
    out, dropped = skill_terms(pages, items)
    assert (out[0].metric, out[0].threshold) == ("DSCR", 1.25)  # 125% coverage is 1.25 times
    assert out[1].category == "CANDIDATE"  # 70 is not a leverage ratio: a person decides
    assert dropped == 0


# ---------- statements: spreadsheets are checked too; an empty AI answer is retried, then flagged ----------

def test_a_spreadsheet_statement_reaches_the_ai_checker_with_its_units_line(tmp_path):
    from datetime import date

    from test_real_documents import FakeParser, add_document

    from dps.db import Database
    from dps.extraction.statement_ai import SecondReading
    from dps.models import Base

    database = Database(f"sqlite:///{tmp_path / 'stmt.db'}")
    Base.metadata.create_all(database.engine)
    from dps.flows.statement import StatementDeps, StatementProcessingFlow

    csv = (b"Amounts in INR crore,\nRevenue from operations,100\nEBITDA,50\nTotal debt,120\nTotal equity,300\n")
    seen = []

    def second(text, values):
        seen.append(text)
        return SecondReading(confirmed=["NET_WORTH"])

    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fs.csv", date(2026, 3, 31), ("CP-1",))
    deps = StatementDeps(database.session_factory, lambda d: csv, FakeParser([]), "ds-dps", False, None, second)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    assert seen and "Amounts in INR crore" in seen[0] and "Revenue from operations 100" in seen[0]
    assert flow.state.confirmed == ["NET_WORTH"]


def test_an_empty_ai_answer_is_asked_again_then_flagged():
    from dps.extraction.statement import extract_values
    from dps.extraction.statement_ai import check

    values = extract_values([("Revenue", 100.0, 1, None), ("Total debt", 120.0, 1, None)], "Rs. in crore").values
    calls = []

    def empty(messages):
        calls.append(1)
        return '{"unit": "crore", "values": {}}'

    reading = check("=== Page 1 ===\nRevenue 100\nTotal debt 120", values, empty)
    assert len(calls) == 2 and reading.issues[0].startswith("SECOND_READER_EMPTY")
    answers = iter(['{"unit": "crore", "values": {}}',
                    '{"unit": "crore", "values": {"REVENUE": {"value": 100, "page": 1, "line": "Revenue 100"}}}'])
    reading = check("=== Page 1 ===\nRevenue 100\nTotal debt 120", values, lambda m: next(answers))
    assert reading.confirmed == ["REVENUE"] and not reading.issues  # the second try answered


# ---------- long documents: OCR opens a PDF once; Docling reads only its batch ----------

def test_pdf_slice_keeps_only_the_batch_pages():
    from pypdf import PdfReader
    from test_phase2_ingest import text_pdf

    from dps.parse.engine import pdf_slice

    data = text_pdf([f"Page number {n} of the agreement" for n in range(1, 13)])
    part = pdf_slice(data, 5, 8)
    reader = PdfReader(__import__("io").BytesIO(part))
    assert len(reader.pages) == 4
    assert "Page number 5" in reader.pages[0].extract_text() and "Page number 8" in reader.pages[3].extract_text()
    assert len(PdfReader(__import__("io").BytesIO(pdf_slice(data, 11, 99))).pages) == 2  # clipped at the end


def test_ocr_opens_a_pdf_once_per_document_and_closes_it(monkeypatch):
    import pypdfium2 as pdfium
    from test_phase2_ingest import text_pdf

    from dps.parse.engine import RapidOcrBackend

    opened, real = [], pdfium.PdfDocument

    def counting(data, *a, **k):
        opened.append(len(data))
        return real(data, *a, **k)

    monkeypatch.setattr(pdfium, "PdfDocument", counting)
    ocr = RapidOcrBackend(dpi=36)
    ocr._engine = lambda image: ([([[0, 0], [1, 0], [1, 1], [0, 1]], "text", 0.9)], None)
    first, second = text_pdf(["a", "b", "c"]), text_pdf(["d", "e"])
    for i in range(3):
        assert ocr.ocr_boxes(first, i, 36)[0][1] == "text"
    assert len(opened) == 1  # three pages, one open (before: one per page)
    ocr.ocr_boxes(second, 0, 36)
    assert len(opened) == 2 and ocr._doc_key[1] == len(second)  # a new document replaces the old one
    ocr.close()
    assert ocr._doc is None


# ---------- long agreements: covenant pages read in batches ----------

def test_long_agreements_split_into_batches_and_short_ones_do_not():
    from dps.skills.covenants.pages import covenant_batches

    duty = "The Borrower shall maintain a ratio of not less than 1.25 times. "
    long_doc = pages_of({n: duty * 20 for n in range(10, 50)}, 60)
    batches, note = covenant_batches(long_doc, [], max_pages=10, max_chars=200_000, max_batches=4)
    assert [len(b) for b in batches] == [10, 10, 10, 10]
    assert {s["batch"] for s in note["sent"]} == {1, 2, 3, 4}
    assert all([p["no"] for p in b] == sorted(p["no"] for p in b) for b in batches)  # each batch in page order
    sent = {p["no"] for b in batches for p in b}
    assert len(sent) == 40 and not note["notSent"]
    capped, note = covenant_batches(long_doc, [], max_pages=10, max_chars=200_000, max_batches=2)
    assert len(capped) == 2 and len(note["notSent"]) == 20  # beyond the cap: listed, not silently dropped
    short = pages_of({3: duty}, 6)
    batches, _ = covenant_batches(short, [], max_pages=25, max_chars=60_000, max_batches=4)
    assert len(batches) == 1 and [p["no"] for p in batches[0]] == [3]


class Batches(Salvage):
    """Answers each batch with the covenants on its own pages; can run out of budget after N reads."""

    def __init__(self, answers: dict[int, list[dict]], budget_after: int | None = None):
        super().__init__([])
        self.answers, self.budget_after, self.reads = answers, budget_after, 0

    def call_json(self, route, messages, schema, check):
        from dps.agent.gateway import AiBudgetExceeded

        self.reads += 1
        if self.budget_after is not None and self.reads > self.budget_after:
            raise AiBudgetExceeded("job budget spent")
        content = messages[0]["content"]
        items = [i for page, items in self.answers.items() if f"<<<page {page}>>>" in content for i in items]
        return {"items": items}


def test_batch_answers_are_merged_once_and_a_spent_budget_stops_the_rest():
    from dps.skills.common import batched_proposals

    doc = [{"no": n, "text": f"On page {n} the Borrower shall keep covenant {n}."} for n in range(1, 5)]
    item = {n: [{"name": f"C{n}", "page": n, "quote": f"shall keep covenant {n}", "value": {"metric": f"C{n}"}}]
            for n in range(1, 5)}
    item[2].append(dict(item[1][0]))  # the same covenant read again in another batch
    batches = [[doc[0]], [doc[1]], [doc[2]], [doc[3]]]
    out = batched_proposals(load("sanction"), Batches(item), batches, all_pages=doc)
    assert sorted(p["name"] for p in out) == ["C1", "C2", "C3", "C4"]  # merged, the repeat kept once
    notes: list[dict] = []
    gw = Batches(item, budget_after=2)
    out = batched_proposals(load("sanction"), gw, batches, notes=notes, all_pages=doc)
    assert sorted(p["name"] for p in out) == ["C1", "C2"] and gw.reads == 3  # stopped after the budget ran out
    assert any("batches 4 to 4" in n["message"] for n in notes if n["code"] == "AI_BUDGET_EXCEEDED")
    assert batched_proposals(load("sanction"), None, batches) is None  # no model at all


def test_the_covenants_skill_reads_a_long_agreement_in_several_batches():
    duty = "The Borrower shall maintain a ratio of not less than 1.25 times. " * 120
    gw = Recorder()
    skills.run(load("covenants"), fake_job("ONBOARD"), model_of(*([duty] * 40)), gw, None)
    assert len(gw.prompts) >= 2 and "part 1 of" in gw.prompts[0]


# ---------- coverage counts a covenant's whole clause, not only its quoted sentence ----------

def test_one_insurance_covenant_covers_its_whole_clause_and_an_escrow_duty_stays_listed():
    from dps.skills.covenants import coverage

    page = ("INSURANCE. The Borrower shall insure the plant against all risks. "
            "The Borrower shall pay all premiums on time. "
            "The Borrower shall send copies of the policies to the Lender. "
            "The Borrower shall renew the policies before they expire. "
            "The Borrower shall open and maintain an escrow account with a bank approved by the Lender.")
    field = {"group": "NON_FINANCIAL_COVENANT", "name": "INSURANCE_MAINTENANCE",
             "value": {"metric": "INSURANCE_MAINTENANCE"},
             "source": {"page": 3, "quote": "The Borrower shall insure the plant against all risks"}}
    cov = coverage.check([{"no": 3, "text": page}], [field])
    assert cov["dutySentences"] == 5 and cov["byQuote"] == 1 and cov["byKind"] == 3
    assert cov["unexplainedCount"] == 1 and "escrow" in cov["unexplained"][0]["text"]
    assert cov["unexplained"][0]["kind"] == "AFF_ACCOUNTS"  # its own kind, and no escrow covenant: still listed


def test_a_financial_sentence_is_covered_only_by_a_covenant_on_the_same_metric():
    from dps.skills.covenants import coverage

    page = ("The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times. "
            "The Borrower shall maintain an Interest Coverage Ratio of not less than 2.00 times.")
    dscr = {"group": "FINANCIAL_COVENANT", "name": "DSCR", "value": {"metric": "DSCR"},
            "source": {"page": 5, "quote": "unrelated words only"}}
    cov = coverage.check([{"no": 5, "text": page}], [dscr])
    assert cov["byKind"] == 1 and cov["unexplainedCount"] == 1
    assert cov["unexplained"][0]["kind"] == "FIN_INTEREST_COVERAGE"  # the ICR duty is not covered by the DSCR covenant
    far = dict(dscr, source={"page": 9, "quote": "unrelated words only"})
    assert coverage.check([{"no": 5, "text": page}], [far])["byKind"] == 0  # a covenant far away covers nothing here


def test_schedule_terms_and_other_fields_have_no_kind():
    from dps.skills.covenants.coverage import field_kind

    assert field_kind({"group": "SCHEDULE_TERM", "name": "cure_window", "value": {"kind": "cure_window"}}) is None
    assert field_kind({"group": "POSSIBLE_COVENANT", "name": "x", "value": {"metric": "OTHER"}}) is None
    assert field_kind({"group": "NON_FINANCIAL_COVENANT", "name": "x", "value": {"metric": "NEGATIVE_PLEDGE"}}) \
        == "NEG_LIENS"


def test_the_newer_kinds_are_recognised_in_sentences_and_covered_by_their_covenant():
    from dps.skills.covenants import coverage

    cases = {
        "The Loan shall be utilized by the Borrower solely for capital expenditure.": "AFF_USE_OF_PROCEEDS",
        "The Borrower shall open and maintain an escrow account with a bank approved by the Lender.": "AFF_ACCOUNTS",
        "The Borrower shall procure that all loans from its promoters are subordinated to the Facility.":
            "AFF_SUBORDINATION",
        "The Borrower shall create a first-ranking mortgage over the property within 90 days.":
            "AFF_SECURITY_PERFECTION",
        "The Borrower shall not terminate any material contract without consent of the Lender.":
            "NEG_MATERIAL_CONTRACTS",
    }
    for sentence, kind in cases.items():
        assert coverage.sentence_kind(sentence) == kind, sentence
    page = " ".join(cases)
    escrow = {"group": "POSSIBLE_COVENANT", "name": "Escrow account", "value": {"metric": "ESCROW_ACCOUNT"},
              "source": {"page": 4, "quote": "unrelated words"}}
    cov = coverage.check([{"no": 4, "text": page}], [escrow])
    assert cov["byKind"] == 1 and cov["unexplainedCount"] == 4
    assert "escrow" not in " ".join(u["text"] for u in cov["unexplained"])

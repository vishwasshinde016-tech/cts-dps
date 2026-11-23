"""The review of 22 Nov 2026 (real AI run of a 100-page US credit agreement): each bug found, as a test."""

from __future__ import annotations

from dps.analyze.identify import find_identifiers, missing_required

COVER = ("364-DAY CREDIT AGREEMENT\n\nDated as of April 3, 2020\n\namong\n\nAUTOZONE, INC.,\nas Borrower,\n\n"
         "THE SEVERAL LENDERS\nFROM TIME TO TIME PARTY HERETO,\n\nU.S. BANK NATIONAL ASSOCIATION,\n"
         "as Administrative Agent and Sole Lead Arranger,\nBANK OF AMERICA, N.A.,\n")
PREAMBLE = ("364-DAY CREDIT AGREEMENT, dated as of April 3, 2020, among AUTOZONE,\nINC., a Nevada corporation (the "
            "“Borrower”), the several lenders identified on the signature pages hereto.")
SIGNED = ("IN WITNESS WHEREOF, the parties hereto have caused this Credit Agreement to be duly executed.\n\n"
          "AUTOZONE, INC.\n\nBy: /s/ Jane Doe\nName: Jane Doe\nTitle: Treasurer\n")
EXHIBIT = "EXHIBIT A\nFORM OF NOTE\n[Reserved]\n"


def _model(*texts: str) -> dict:
    return {"pages": [{"no": i + 1, "text": t, "blocks": []} for i, t in enumerate(texts)]}


def test_a_us_credit_agreement_shows_its_borrower_issuer_and_signature():
    """B3: REQUIRED_MISSING said BORROWER_NAME, ISSUER, SIGNATURE on a signed agreement."""
    found = find_identifiers(_model(COVER, PREAMBLE, *["text"] * 3, SIGNED, EXHIBIT, EXHIBIT, EXHIBIT))
    names = {f.value for f in found if f.kind == "BORROWER_NAME"}
    assert "AUTOZONE, INC" in names
    issuer = next(f for f in found if f.kind == "ISSUER")
    assert issuer.value == "U.S. BANK NATIONAL ASSOCIATION" and issuer.extra["role"] == "AGENT"
    sig = next(f for f in found if f.kind == "SIGNATURE")
    assert sig.value == "PRESENT" and sig.page == 6
    assert missing_required("LOAN_AGREEMENT", found) == []


def test_a_name_across_a_line_break_is_one_name():
    found = find_identifiers(_model(PREAMBLE))
    assert "AUTOZONE, INC" in {f.value for f in found if f.kind == "BORROWER_NAME"}


def test_an_unsigned_document_still_says_unsure():
    found = find_identifiers(_model(COVER, "body text only"))
    assert next(f for f in found if f.kind == "SIGNATURE").value == "UNSURE"


# ---------- B1: the rules quote the covenant itself, not the definitions or the table of contents ----------
from dps.extraction.agreement import extract_terms  # noqa: E402
from dps.parse.engine import Page  # noqa: E402

TOC = ("TABLE OF CONTENTS\n6.2 Preservation of Existence and Franchises 57\n6.3 Books and Records 57\n"
       "6.4 Insurance 58\n7.1 Liens 64\n")
DEFINITIONS = ("ARTICLE I DEFINITIONS\n1.1 Definitions.\nAs used in this Agreement, the following terms shall have the "
               "meanings set forth below. “Compliance Certificate” means a certificate duly executed by a "
               "Responsible Officer. “Insurance” means insurance the Borrower shall maintain. The Borrower "
               "shall keep proper books of record and account.\n")
COVENANTS = ("ARTICLE VI AFFIRMATIVE COVENANTS\n6.1 Financial Statements.\nThe Borrower shall furnish to the "
             "Administrative Agent, concurrently with the delivery of the financial statements, a Compliance "
             "Certificate signed by a Responsible Officer.\n6.3 Books and Records.\nThe Borrower shall keep proper "
             "books of record and account in which full, true and correct entries are made.\n6.4 Insurance.\nThe "
             "Borrower shall maintain insurance with financially sound and reputable insurance companies.\n")


def _terms():
    pages = [Page(1, TOC, "TEXT", 1.0), Page(2, DEFINITIONS, "TEXT", 1.0), Page(3, COVENANTS, "TEXT", 1.0)]
    return {t.metric: t for t in extract_terms(pages) if t.category == "NON_FINANCIAL"}


def test_non_financial_covenants_are_found_in_the_covenant_article():
    terms = _terms()
    for metric in ("COMPLIANCE_CERTIFICATE", "BOOKS_AND_RECORDS", "INSURANCE_MAINTENANCE"):
        assert terms[metric].page == 3, (metric, terms[metric].page, terms[metric].quote)


def test_the_quote_is_the_duty_not_the_heading():
    terms = _terms()
    assert "shall furnish" in terms["COMPLIANCE_CERTIFICATE"].quote
    assert "shall maintain insurance" in terms["INSURANCE_MAINTENANCE"].quote
    assert terms["BOOKS_AND_RECORDS"].quote != "6.3 Books and Records."


# ---------- B2: a number in the definitions is not a covenant ----------
DEFS_WITH_FLOOR = ("ARTICLE I DEFINITIONS\n1.1 Definitions.\n“Consolidated Interest Coverage Ratio” means the "
                   "ratio of Consolidated EBITDAR to Consolidated Interest Expense. “Benchmark Replacement” "
                   "means the sum of (a) the alternate rate and (b) the Benchmark Replacement Adjustment; provided "
                   "that, "
                   "if the Benchmark Replacement as so determined would be less than 0.75%, the Benchmark Replacement "
                   "will be deemed to be 0.75%.\n")
ICR_COVENANT = ("ARTICLE VI AFFIRMATIVE COVENANTS\n6.11 Interest Coverage Ratio.\nThe Borrower shall cause the "
                "Consolidated Interest Coverage Ratio as of the last day of each fiscal quarter to be no less than "
                "2.50 to 1.0.\n")


def test_a_rate_floor_in_the_definitions_is_not_an_interest_cover_covenant():
    pages = [Page(1, DEFS_WITH_FLOOR, "TEXT", 1.0), Page(2, ICR_COVENANT, "TEXT", 1.0)]
    icr = [t for t in extract_terms(pages) if t.metric == "ICR"]
    assert [(t.operator, t.threshold, t.page) for t in icr] == [(">=", 2.5, 2)]


# ---------- B10: every AI call is traceable to the prompt it used ----------
def test_prompt_version_fingerprints_the_task_not_the_pages():
    """dps_ai_call.prompt_version was "-" on all 30 calls: DPS sends one user message, no system message."""
    from dps.agent.usage import prompt_version
    from dps.skills import load
    from dps.skills.common import prompt_for

    pack = load("covenants")
    a = prompt_for(pack, [{"no": 1, "text": "The Borrower shall maintain a DSCR of 1.20."}])
    b = prompt_for(pack, [{"no": 7, "text": "Other pages, other words."}], extra="These are part 2 of 3.")
    other = prompt_for(load("schedule_terms"), [{"no": 1, "text": "The Borrower shall maintain a DSCR of 1.20."}])
    assert prompt_version(a) != "-" and len(prompt_version(a)) == 8
    assert prompt_version(a) == prompt_version(b)  # same task and format, other pages and batch note
    assert prompt_version(a) != prompt_version(other)  # another skill's task
    assert prompt_version([{"role": "system", "content": "x"}]) != "-"  # a system prompt still counts


# ---------- B9: a temperature set for a route in the routes file is used ----------
def test_a_routes_file_temperature_is_used_and_unset_means_the_model_default(tmp_path):
    from types import SimpleNamespace

    from dps.agent.gateway import Routes, temperature_for

    path = tmp_path / "routes.yaml"
    path.write_text("routes:\n  strong: {deployment: d1, temperature: 0}\n  small: {deployment: d2}\n",
                    encoding="utf-8")
    routes = Routes.load(path).routes
    unset = SimpleNamespace(ai_temperature=None)
    assert temperature_for(routes["strong"], unset) == 0.0
    assert temperature_for(routes["small"], unset) is None  # newer Azure models refuse any temperature
    assert temperature_for(routes["small"], SimpleNamespace(ai_temperature=0.2)) == 0.2
    assert Routes.load(None, "azure/m").routes["strong"].temperature is None


class _Gateway:
    """call_json only (no agent tools path); records each call's message size."""

    def __init__(self):
        self.sizes: list[int] = []

    def call_json(self, route, messages, schema, check=None):
        self.sizes.append(len(messages[0]["content"]))
        return {"items": []}


# ---------- B7: every unexplained sentence is read again, within the job's token budget ----------
def test_the_residual_read_reads_every_open_sentence():
    """121 open sentences: 4 calls of 25 read 100, and 21 were left (RESIDUAL_BUDGET)."""
    from dps.skills.covenants import omissions

    entries = [{"id": f"c{i}", "page": 1, "_full": f"The Borrower shall do thing {i}.", "references": []}
               for i in range(121)]
    gw, notes = _Gateway(), []
    omissions.read(gw, entries, [{"no": 1, "text": "x"}], notes)
    assert len(gw.sizes) == 5
    assert not [n for n in notes if n["code"] == "RESIDUAL_BUDGET"]


# ---------- B8: schedule pages that do not fit one prompt are read in batches ----------
def test_schedule_pages_are_read_in_batches_not_left_out():
    """PROMPT_LIMIT said pages 59, 61, 67, 68 did not fit in the schedule read."""
    from dps.skills import load
    from dps.skills.common import MAX_PROMPT_CHARS, batched_proposals, page_batches

    pages = [{"no": n, "text": f"Repayment within 30 days of each quarter. {'word ' * 2400}"} for n in range(1, 21)]
    batches = page_batches(pages)
    assert len(batches) > 1 and [p["no"] for b in batches for p in b] == list(range(1, 21))
    assert all(sum(len(p["text"]) + 40 for p in b) <= MAX_PROMPT_CHARS for b in batches)
    gw, notes = _Gateway(), []
    batched_proposals(load("schedule_terms"), gw, batches, notes=notes)
    assert len(gw.sizes) == len(batches) and not [n for n in notes if n["code"] == "PROMPT_LIMIT"]


# ---------- B5 + B6: one sentence, one finding, with the most specific code ----------
def _f(method, group, code, quote="The Borrower shall deliver annual financial statements.", page=59):
    return {"id": f"{method}-{code}", "group": group, "name": code, "method": method, "status": "NEEDS_REVIEW",
            "value": {"metric": code, "code": code}, "source": {"page": page, "quote": quote}, "issues": []}


def test_two_ai_readings_of_one_sentence_become_one_with_the_specific_code():
    from dps.skills.runner import merge_same_sentence

    fields = [_f("AI", "POSSIBLE_COVENANT", "AFF_OTHER"), _f("AI", "POSSIBLE_COVENANT", "REP_ANNUAL_FS"),
              _f("AI", "POSSIBLE_COVENANT", "REP_INTERIM_FS", quote="Quarterly statements within 45 days.")]
    out = merge_same_sentence(fields)
    assert [f["value"]["code"] for f in out] == ["REP_ANNUAL_FS", "REP_INTERIM_FS"]
    assert out[0]["value"]["alsoCodes"] == ["AFF_OTHER"]
    assert any(i["code"] == "SAME_SENTENCE" for i in out[0]["issues"])


def test_a_rules_finding_is_never_dropped_and_keeps_the_ai_code_as_an_alternative():
    from dps.skills.runner import merge_same_sentence

    q = "The Borrower shall cause the ratio of Consolidated Adjusted Debt to Consolidated EBITDAR to be <= 3.50."
    rules = _f("REGEX", "FINANCIAL_COVENANT", "DEBT_TO_EBITDA", quote=q, page=63)
    ai = _f("AI", "POSSIBLE_COVENANT", "FIN_RENT_ADJUSTED_LEVERAGE", quote=q, page=63)
    other_rule = _f("REGEX", "FINANCIAL_COVENANT", "ICR", quote=q, page=63)
    out = merge_same_sentence([rules, ai, other_rule])
    assert [f["method"] for f in out] == ["REGEX", "REGEX"]
    assert out[0]["value"]["alsoCodes"] == ["FIN_RENT_ADJUSTED_LEVERAGE"]


def test_an_event_of_default_is_not_merged_into_a_covenant():
    from dps.skills.runner import merge_same_sentence

    out = merge_same_sentence([_f("REGEX", "NON_FINANCIAL_COVENANT", "NEGATIVE_PLEDGE"),
                               _f("AI", "EVENT_OF_DEFAULT", "EOD_COVENANT_BREACH")])
    assert len(out) == 2


def test_an_other_code_becomes_the_specific_one_its_name_is_known_by():
    """B6: the undertakings part gave AFF_OTHER to "Officer's Certificate"; the taxonomy calls it REP_COMPLIANCE_CERT.
    A name that is not a covenant type keeps OTHER, and an event-of-default code is never put on a covenant."""
    from dps.skills.covenants.codes import stamp

    fields = [_f("AI", "POSSIBLE_COVENANT", "AFF_OTHER"), _f("AI", "POSSIBLE_COVENANT", "AFF_OTHER"),
              _f("AI", "POSSIBLE_COVENANT", "AFF_OTHER")]
    for f, name in zip(fields, ("Officer’s Certificate", "Indemnity", "ERISA"), strict=True):
        f["name"] = name
    stamp(fields)
    assert [(f["value"]["code"], f["value"]["category"]) for f in fields] == [
        ("REP_COMPLIANCE_CERT", "reporting"), ("AFF_OTHER", "affirmative"), ("AFF_OTHER", "affirmative")]


# ---------- B4: the confidence follows the evidence ----------
def test_confidence_follows_the_evidence():
    """Every AI field scored 0.85 and every rules field 0.97 (a false finding too): now the weak points DPS already
    knows lower the score, and the reasons say which."""
    from dps.scoring import RuleTableScorer, signals

    def score(f):
        return RuleTableScorer().score(signals(f, None, "HIGH"))

    q = "The Borrower shall cause the Consolidated Interest Coverage Ratio to be no less than 2.50 to 1.0."
    clean = _f("REGEX", "FINANCIAL_COVENANT", "FIN_INTEREST_COVERAGE", quote=q)
    no_number = {**_f("REGEX", "FINANCIAL_COVENANT", "FIN_INTEREST_COVERAGE", quote=q),
                 "issues": [{"code": "NUMBER_NOT_IN_QUOTE"}]}
    heading = _f("REGEX", "NON_FINANCIAL_COVENANT", "NEG_DEBT", quote="7.5 Subsidiary Indebtedness.")
    ai_specific = _f("AI", "POSSIBLE_COVENANT", "REP_ANNUAL_FS", quote=q)
    ai_other = _f("AI", "POSSIBLE_COVENANT", "AFF_OTHER", quote=q)
    disputed = {**_f("AI", "POSSIBLE_COVENANT", "REP_ANNUAL_FS", quote=q)}
    disputed["value"]["alsoCodes"] = ["AFF_OTHER"]
    assert score(clean).confidence == 0.97
    assert score(no_number).confidence < 0.8 and "VALUE_NOT_IN_QUOTE" in score(no_number).reasons
    assert score(heading).confidence < 0.8 and "QUOTE_IS_A_HEADING" in score(heading).reasons
    assert score(ai_other).confidence < score(ai_specific).confidence < score(clean).confidence
    assert score(disputed).confidence < score(ai_specific).confidence


def test_a_model_can_be_trained_with_the_evidence_signals():
    from dps.scoring import EVIDENCE, FEATURES, signals, train

    q = "The Borrower shall deliver annual financial statements."
    good = _f("AI", "POSSIBLE_COVENANT", "REP_ANNUAL_FS", quote=q)
    weak = _f("AI", "POSSIBLE_COVENANT", "AFF_OTHER", quote=q)
    rows = [(signals(good, None, "HIGH"), True)] * 30 + [(signals(weak, None, "HIGH"), False)] * 30
    model = train(rows, features=FEATURES + EVIDENCE)
    assert model.weights["code_specific"] > 0
    assert model.score(signals(good, None, "HIGH")).confidence > model.score(signals(weak, None, "HIGH")).confidence


def test_classify_and_residual_prompts_have_a_version_too():
    """The real run of 22 Nov 2026: 6 of 32 calls (the classify call and the residual read) still showed "-"."""
    from dps.agent.usage import prompt_version

    classify = [{"role": "user", "content": "You classify one bank lending document.\nClasses: X\nFacts: a\nPages"}]
    residual = [{"role": "user", "content": "Instructions\nSchema:\n{}\n\nCandidates:\n\n[c1] page 3"}]
    other_doc = [{"role": "user", "content": "You classify one bank lending document.\nClasses: X\nFacts: b\nOther"}]
    assert prompt_version(classify) != "-" and prompt_version(residual) != "-"
    assert prompt_version(classify) == prompt_version(other_doc)


def test_the_agent_role_of_an_issuer_is_in_the_contract():
    """The save-time check flagged it on the real run: identifiers[].role AGENT was not in result.json."""
    import jsonschema
    from test_jobapi import _schema

    result = _schema("result")
    found = find_identifiers(_model(COVER))
    issuer = next(f for f in found if f.kind == "ISSUER").as_result()
    jsonschema.Draft202012Validator({**result["$defs"]["identifier"], "$defs": result["$defs"]}).validate(issuer)


def test_a_long_sentence_with_a_decimal_and_a_sub_heading_are_quoted_whole():
    """7.5 quoted its heading only (its one sentence holds "Schedule 7.5"); "(g) Change of Control." quoted the
    sub-heading, not the duty after it; a page number line started a quote."""
    text = ("ARTICLE VII NEGATIVE COVENANTS\n7.5 Subsidiary Indebtedness.\nThe Borrower will not permit any of its "
            "Subsidiaries to incur any Indebtedness, except: (a) Indebtedness set forth on Schedule 7.5 and (b) other "
            "Indebtedness.\n7.6 Other Matters.\nThe Borrower will furnish notices.\n55\n(g) Change of Control. Upon "
            "knowledge thereof, the Borrower will promptly notify the Administrative Agent of any change of control.\n")
    terms = {t.metric: t for t in extract_terms([Page(1, text, "TEXT", 1.0)]) if t.category == "NON_FINANCIAL"}
    assert terms["DEBT_INCURRANCE_RESTRICTION"].quote.startswith("The Borrower will not permit")
    assert "will promptly notify" in terms["CHANGE_OF_CONTROL"].quote
    assert not terms["CHANGE_OF_CONTROL"].quote.startswith("55")

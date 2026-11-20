"""The statement reader of the statement_lines skill (full review WP7: one pipeline).

EXTRACT_STATEMENT runs in the skills flow like every other job type. This module holds the statement reading that
used to be its own CrewAI flow (Flow B), unchanged in what it reads and checks:

1. **read** the file: the table reader for PDFs (OCR quality, periods, comparative columns), the text lines
   otherwise; XLSX, XLS and CSV rows; the number style the document itself uses; prompt-injection signs;
2. **extract**: borrower label memory, the loan profile (currency, financial-year end, group or company figures,
   number style), the statement's currency and the bank's FX rate, the lines and their months, unit jumps against
   the previous period, the assessment cycle's expectations (period, basis, entity, currency, units), the language,
   and the optional AI second reader;
3. **route**: a file the standard reader cannot read goes to the HEAVY pool; a file with no lines fails; everything
   else is a proposal for a person (a statement is never reported unreviewed: G21).

What changed (full review G9, G21, G22): nothing is written to the values table and no event goes to Core while
reading. ``publish`` writes the figures a person approved, when the review of the job is complete. Figures DPS
works out itself (total debt, EBITDA from its parts, trailing twelve months) are not reported unless
``DPS_REPORT_DERIVED_VALUES`` is on: Spring's formula engine calculates them from the printed figures.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import false, select
from sqlalchemy.orm import Session, sessionmaker

from dps import history, services
from dps.agent.guard import injection_signs
from dps.extraction.statement import (
    FLOW_KEYS,
    LINE_ALIASES,
    LineValue,
    StatementResult,
    check,
    derive,
    detect_number_style,
    extract_values,
    header_from_csv,
    header_from_xls,
    header_from_xlsx,
    likely_lines,
    match_key,
    number_style,
    rows_from_csv,
    rows_from_text,
    rows_from_xls,
    rows_from_xlsx,
)
from dps.extraction.statement_ai import SecondReading, mark_pages
from dps.extraction.statement_pdf import PdfStatement
from dps.language import detect as detect_language
from dps.language import name as language_name
from dps.models import Document, Job, StatementValue
from dps.money import currency_of, detect_currency, money_unit, to_money_unit
from dps.parse.engine import ParseError, needs_heavy
from dps.profiles import LoanProfile

ACTOR = "dps-statement"
# Holds a person clears outside the statement (an FX rate, a borrower setting, relinking): the statement is read
# again with them before anything is approved.
REREAD = ("CURRENCY_DIFFERS", "CURRENCY_MIXED", "LOAN_CURRENCY_UNKNOWN", "LOANS_DIFFER")
# Lines compared with the CP's previous period, and the change that means a unit misread (lakhs read as crore is
# 100 times, rupees read as thousands 1,000 times; a real business rarely moves 20 times in a year).
SCALE_KEYS = ("REVENUE", "TOTAL_ASSETS", "NET_WORTH", "PAT", "CURRENT_ASSETS")
SCALE_JUMP = 20.0
# A scan read below this mean OCR confidence is held for a person to confirm the figures. Benchmark: clean
# scans read at 0.986-0.993 with every value right; a blurred 100 dpi scan read at 0.952 and lost 3 of 12 lines.
OCR_REVIEW_QUALITY = 0.97
log = logging.getLogger(__name__)


class NeedsHeavyPool(Exception):
    """The standard reader cannot read this statement; the HEAVY pool (Docling) reads it."""


class ContentFailed(Exception):
    """The statement has no readable lines (or the file is not a statement DPS can read)."""


@dataclass
class StatementDeps:
    session_factory: sessionmaker[Session]
    read_file: Callable[[Document], bytes]
    parser: object
    data_source_id: str  # the bank app's data-source id (kept for callers; nothing is pushed to Core any more)
    heavy_pool: bool
    # Table-aware PDF reader (extraction.statement_pdf.read_pdf_statement); None: plain text lines only.
    pdf_reader: Callable[[bytes], PdfStatement] | None = None
    # Optional second reader (extraction.statement_ai.check): (page text, values[, scale]) -> grounded reading and
    # vote; scale turns the model's crore into the values' money unit when the loan is not in INR.
    second_reader: Callable[..., SecondReading] | None = None
    entity: str = "STANDALONE"  # DPS_STATEMENT_ENTITY: whose column a spreadsheet is read for
    fiscal_year_end_month: int = 3  # DPS_FISCAL_YEAR_END_MONTH: a statement's months when it does not state them
    # Money for any country: a CP's loan currency (Core), the bank's FX rate (Core, Settings > FX rates) and the
    # currency used when Core cannot say (DPS_DEFAULT_CURRENCY). None: every loan is in default_currency.
    loan_currency: Callable[[str], str | None] | None = None
    fx_rate: Callable[[str, str, date], float | None] | None = None
    default_currency: str = "INR"
    # What applies to each borrower (Core: country profile and borrower settings; wins over loan_currency, entity
    # and fiscal_year_end_month) and the currency words of every country profile.
    loan_profile: Callable[[str], LoanProfile | None] | None = None
    currency_words: Callable[[], list] | None = None
    # Full review G22: report the figures DPS works out (total debt, EBITDA from its parts, twelve months).
    # Off: Spring's formula engine works them out from the printed figures.
    report_derived_values: bool = False


class StatementState(BaseModel):
    id: str = ""
    job_id: str = ""
    document_id: str = ""
    file_kind: str = ""
    header_text: str = ""
    page_text: str = ""
    rows: list[list] = Field(default_factory=list)
    values: list[dict] = Field(default_factory=list)
    unit_scale: str = ""
    likely: list[dict] = Field(default_factory=list)  # possible lines for covenant inputs not found
    confirmed: list[str] = Field(default_factory=list)  # covenant inputs both readers read the same
    remembered: list[str] = Field(default_factory=list)  # lines mapped from this borrower's confirmed wording
    net_interest_confirmed: bool = False  # this borrower's interest wording was confirmed by a person before
    issues: list[str] = Field(default_factory=list)
    route: str = ""
    review_done: bool = False  # read again after a person cleared a hold (an FX rate added, a borrower relinked)
    parse_issues: list[str] = Field(default_factory=list)
    # 12-month covenants: each page's period in months, and the comparative columns (last year's same period, the
    # last full year and the month it ended, "YYYY-MM") read beside this period's.
    page_months: dict[str, int | None] = Field(default_factory=dict)
    prior_rows: list[list] = Field(default_factory=list)
    annual_rows: list[list] = Field(default_factory=list)
    annual_end: str = ""
    prior_values: list[dict] = Field(default_factory=list)
    annual_values: list[dict] = Field(default_factory=list)
    # Money for any country: the currency the statement is printed in, the loans' currency its figures are kept in
    # and the FX rate between the two (1 when they are the same).
    currency: str = ""
    loan_currency: str = ""
    fx_rate: float = 1.0
    # A rate was found for a statement printed in another currency than the loans (a rate of exactly 1.0 is a rate:
    # it was taken for "no rate" and the figures stayed unconverted with no hold; local end-to-end run, 20 Nov 2026).
    fx_known: bool = False
    fiscal_month: int = 0
    entity: str = ""
    number_style: str = ""
    profile_issues: list[str] = Field(default_factory=list)
    language: str = "en"
    lines: dict = Field(default_factory=dict)  # catalogue lines: expected, found, notFound
    not_found: list[str] = Field(default_factory=list)  # catalogue lines the statement did not give
    error: str = ""
    outcome: Literal["", "REPORTED", "NEEDS_REVIEW", "FAILED", "HEAVY"] = ""


class StatementReader:
    """Reads one statement for one job. ``read`` never writes the values table (``publish`` does, after review)."""

    def __init__(self, deps: StatementDeps) -> None:
        self._deps = deps
        self._raw_text = ""
        self.state = StatementState()

    def read(self, job_id: str, document_id: str, review_done: bool = False) -> StatementState:
        self.state = StatementState(id=str(uuid.uuid4()), job_id=job_id, document_id=document_id,
                                    review_done=review_done)
        self._parse_document()
        self._extract_and_calculate()
        self._validate()
        return self.state

    # ---------- 1. read ----------

    def _parse_document(self) -> None:
        with self._deps.session_factory() as db:
            doc = db.get(Document, self.state.document_id)
            data, ext = self._deps.read_file(doc), _suffix(doc.file_name)
        self.state.file_kind = ext.lstrip(".")
        self.state.profile_issues = self._profile()
        style = self.state.number_style
        with number_style(style):
            rows = self._parse(data, ext)
        seen = detect_number_style("\n".join((self.state.header_text, self.state.page_text, self._raw_text)))
        signs = injection_signs("\n".join((self.state.header_text, self.state.page_text, self._raw_text)))
        if signs:
            self.state.profile_issues.append(f"PROMPT_INJECTION_SUSPECTED: the statement addresses an AI model: "
                                             f"'{signs[0][:120]}'")
        if seen == "UNCLEAR":
            self.state.parse_issues.append("NUMBER_STYLE_UNCLEAR: the statement writes numbers both as 1,234.56 and "
                                           "as 1.234,56; check each figure")
        elif seen and seen != style and not (seen == "WESTERN" and style == "INDIAN"):
            # The document's own numbers say how they are written: read it again that way.
            self.state.number_style = seen
            with number_style(seen):
                rows = self._parse(data, ext)
        self.state.rows = [list(r) for r in rows]

    def _parse(self, data: bytes, ext: str) -> list[tuple]:
        entity = self.state.entity or self._deps.entity
        self.state.parse_issues, self._raw_text = [], ""
        try:
            if ext == ".pdf":
                table = self._read_table(data, entity)
                if table is not None and table.rows:
                    rows, self.state.header_text = table.rows, table.header_text
                    self.state.page_months = {str(p): m for p, m in table.periods.months.items()}
                    self.state.prior_rows = [list(r) for r in table.periods.prior]
                    self.state.annual_rows = [list(r) for r in table.periods.annual]
                    end = table.periods.annual_end
                    self.state.annual_end = f"{end[0]:04d}-{end[1]:02d}" if end else ""
                    numbers = table.pages or list(range(1, len(table.texts) + 1))
                    self.state.page_text = mark_pages(list(zip(numbers, table.texts, strict=False)))
                    if table.ocr_quality is not None and table.ocr_quality < OCR_REVIEW_QUALITY:
                        self.state.parse_issues = [
                            f"LOW_OCR_QUALITY: scanned pages read at {table.ocr_quality:.0%} confidence"]
                else:
                    pages = self._deps.parser.parse(data)
                    if not self._deps.heavy_pool and needs_heavy(pages):
                        self.state.route = "heavy"
                    rows = rows_from_text([(p.no, p.text) for p in pages])
                    self.state.header_text = " ".join(p.text[:600] for p in pages[:2])
                    self._raw_text = "\n".join(p.text for p in pages)
                    if not self.state.page_text:  # the AI reader cites pages of text PDFs too
                        self.state.page_text = mark_pages([(p.no, p.text) for p in pages])
            elif ext == ".xlsx":
                rows = rows_from_xlsx(data, entity)
                self.state.header_text = header_from_xlsx(data)
            elif ext == ".xls":
                rows = rows_from_xls(data, entity)
                self.state.header_text = header_from_xls(data)
            elif ext == ".csv":
                rows = rows_from_csv(data, entity)
                self.state.header_text = header_from_csv(data)
                self._raw_text = data.decode("utf-8-sig", errors="replace")[:200_000]
            else:
                raise ParseError(f"{ext} statements are not supported; upload PDF, XLSX, XLS or CSV")
        except ParseError as ex:
            self.state.error, rows = str(ex), []
        except Exception as ex:  # noqa: BLE001 - a corrupt workbook or CSV is a content failure, not a crash
            self.state.error, rows = f"Unreadable statement: {type(ex).__name__}", []
        return rows

    def _read_table(self, data: bytes, entity: str) -> PdfStatement | None:
        if self._deps.pdf_reader is None:
            return None
        try:
            if entity != self._deps.entity:
                return self._deps.pdf_reader(data, entity=entity)  # the borrower's figures: group or company
            return self._deps.pdf_reader(data)
        except Exception as ex:  # noqa: BLE001 - an odd PDF: the plain-text reader below still gets a go
            log.warning("Table reader failed, using text lines: %s", type(ex).__name__)
            return None

    # ---------- 2. extract ----------

    def _extract_and_calculate(self) -> None:
        rows, memory_issues = self._recall([tuple(r) for r in self.state.rows])
        money_issues = self._money()
        ccy, rate = self.state.loan_currency, self.state.fx_rate
        if self.state.currency != ccy and not self.state.fx_known:
            ccy = self.state.currency  # no rate: read in the statement's own currency and held, never reported
        result: StatementResult = extract_values(rows, self.state.header_text, ccy, rate)
        self._set_months(result.values)
        self.state.values = [v.as_dict() for v in result.values]
        for rows_in, into in ((self.state.prior_rows, "prior_values"), (self.state.annual_rows, "annual_values")):
            side = (extract_values([tuple(r) for r in rows_in], self.state.header_text, ccy, rate).values
                    if rows_in else [])
            setattr(self.state, into, [v.as_dict() for v in side if v.key in FLOW_KEYS])
        self.state.unit_scale = result.unit_scale
        issues = result.issues
        if self.state.net_interest_confirmed:
            issues = [i for i in issues if not i.startswith("INTEREST_IS_NET")]
        self.state.issues = (self.state.profile_issues + money_issues + issues + self.state.parse_issues
                             + self._scale_jumps(result.values) + memory_issues + self._cycle_mismatches())
        found = {v.key for v in result.values if not v.derived}
        factor = to_money_unit(1.0, result.unit_scale, ccy, rate)
        self.state.likely = likely_lines([tuple(r) for r in self.state.rows], found, factor) if result.values else []
        # The rules read English labels; a statement in another language is read by the AI reader.
        lang = detect_language(" ".join((self.state.header_text, self.state.page_text[:20000],
                                         " ".join(str(r[0]) for r in self.state.rows[:200]))))
        self.state.language = lang
        foreign = lang != "en"
        if foreign and self._deps.second_reader is None:
            self.state.issues.append(f"LANGUAGE_NOT_READ: the statement is in {language_name(lang)}; the rules read "
                                     "English only. Switch on the AI reader (DPS_AI_MODEL) or enter the figures "
                                     "by hand")
        if not self.state.page_text and self.state.rows:
            # A spreadsheet has no page text: its heading (with the units, "Amounts in INR lakhs") and its lines, as
            # read, are what the AI checker reads and is checked against, so spreadsheets are checked too.
            lines = [self.state.header_text] if self.state.header_text else []
            lines += [" ".join(str(c) for c in r if c not in (None, "")) for r in self.state.rows]
            self.state.page_text = mark_pages([(1, "\n".join(line for line in lines if line.strip()))])
        if self._deps.second_reader is not None and self.state.page_text and (result.values or foreign):
            scale = to_money_unit(1e7, "unit", ccy, rate)  # the model answers in crore of the printed currency
            extra = {} if self.state.entity in ("", self._deps.entity) else {"entity": self.state.entity}
            reading = (self._deps.second_reader(self.state.page_text, result.values, **extra) if scale == 1.0
                       else self._deps.second_reader(self.state.page_text, result.values, scale, **extra))
            self.state.issues += reading.issues
            self.state.confirmed = reading.confirmed
            if foreign and reading.suggestions:
                result = self._ai_values(result, reading.suggestions, ccy)
            # A line the second reader found, on the page it cited, is a better hint than a word match.
            by_ai = {s["key"] for s in reading.suggestions}
            self.state.likely = reading.suggestions + [x for x in self.state.likely if x["key"] not in by_ai]

    def _cycle_mismatches(self) -> list[str]:
        """Spec Flow 3: the statement against what its cycle expects (period, basis, legal entity, currency, units),
        before any value is reported. A mismatch holds the statement for a person."""
        from dps.analyze import obligations
        from dps.analyze.classify import sub_attributes
        from dps.analyze.identify import find_identifiers
        with self._deps.session_factory() as db:
            job = db.get(Job, self.state.job_id) if self.state.job_id else None
            ctx = (json.loads(job.request_json or "{}").get("context") or {}) if job is not None else {}
        if not any(ctx.get(k) for k in ("asOfDate", "basis", "legalEntity", "expectedCurrency", "expectedUnits")):
            return []
        text = "\n".join(t for t in (self.state.header_text, self.state.page_text or self._raw_text) if t)
        model = {"pages": [{"no": 1, "text": text}]}
        basis = (self.state.entity if self.state.entity in ("CONSOLIDATED", "STANDALONE")
                 else sub_attributes(model, "FINANCIAL_STATEMENT")[0])
        return [f"{w['code']}: {w['message']}" for w in obligations.check(ctx, find_identifiers(model), basis, model)]

    def _ai_values(self, result: StatementResult, suggestions: list[dict], ccy: str) -> StatementResult:
        """The lines the AI reader read from a statement in another language, each on the page it cites (the
        grounding check), become the statement's figures with their totals; a person checks every one."""
        found = {v.key for v in result.values if not v.derived}
        unit = money_unit(ccy)
        added = [LineValue(s["key"], float(s["value"]), unit, False, s.get("page"), None,
                           str(s.get("label") or "")[:120]) for s in suggestions if s["key"] not in found]
        ai = StatementResult([v for v in result.values if not v.derived] + added, result.unit_scale or "AI", money=unit)
        derive(ai)
        check(ai)
        self._set_months(ai.values)
        self.state.values = [v.as_dict() for v in ai.values]
        self.state.likely = [x for x in self.state.likely if x["key"] not in {s["key"] for s in suggestions}]
        # The rules' checks on the first reading (lines missing, totals that did not add up) give way to the same
        # checks on the figures as now read.
        self.state.issues = [i for i in self.state.issues if i not in result.issues] + ai.issues + [
            f"READ_BY_AI: {len(added)} figures of this {language_name(self.state.language)} statement were read by "
            "the AI reader, each found on its page; check every figure before approving"]
        return ai

    def _money(self) -> list[str]:
        """Money for any country: the loans' currency (all linked CPs must share one), the statement's (what its header
        names, else the loans') and the bank's FX rate between them on the statement date. Without a rate the
        statement waits: a figure in one currency is never compared with a limit in another."""
        with self._deps.session_factory() as db:
            as_of = db.get(Document, self.state.document_id).as_of_date or date.today()
        issues = []
        loan = self.state.loan_currency or self._deps.default_currency
        words = self._deps.currency_words() if self._deps.currency_words else []
        printed = detect_currency(self.state.header_text, words) or loan
        rate = 1.0
        if printed != loan:
            got = self._deps.fx_rate(printed, loan, as_of) if self._deps.fx_rate else None
            if got:
                rate = got
                self.state.fx_known = True
            else:
                issues.append(f"CURRENCY_DIFFERS: the statement is in {printed}, the loan in {loan}, and there is no "
                              f"{printed} to {loan} rate for {as_of.isoformat()}; add it in Settings (FX rates), "
                              "then submit the statement again")
        self.state.currency, self.state.loan_currency, self.state.fx_rate = printed, loan, rate
        return issues

    def _profile(self) -> list[str]:
        """What applies to the linked borrowers (Core: borrower settings over the country profile): the loan
        currency, the financial-year end, group or company figures and the number style. Linked loans must agree on
        all four; a loan whose currency nobody knows (the CP service did not answer) waits."""
        with self._deps.session_factory() as db:
            cp_ids = db.get(Document, self.state.document_id).active_cp_ids()
        d = self._deps
        default = LoanProfile(d.default_currency, True, d.fiscal_year_end_month, d.entity, "INDIAN")
        found = {}
        for cp in cp_ids:
            p = d.loan_profile(cp) if d.loan_profile else None
            if p is None:
                p = LoanProfile((d.loan_currency(cp) if d.loan_currency else None) or d.default_currency, True,
                                d.fiscal_year_end_month, d.entity, "INDIAN")
            found[cp] = p
        first = found[cp_ids[0]] if cp_ids else default
        issues = []
        unknown = [cp for cp, p in found.items() if not p.currency_known]
        if unknown:
            issues.append(f"LOAN_CURRENCY_UNKNOWN: the CP service did not say the currency of {', '.join(unknown)}; "
                          "set it on the borrower (Borrower settings) or try again when the CP service answers")
        if len({p.currency for p in found.values()}) > 1:
            issues.append("CURRENCY_MIXED: the statement is linked to loans in " + ", ".join(
                f"{cp} ({p.currency})" for cp, p in found.items()) + "; link it to loans in one currency")
        if len({(p.fiscal_year_end_month, p.statement_entity) for p in found.values()}) > 1:
            issues.append("LOANS_DIFFER: the linked borrowers differ in financial-year end or in group or company "
                          "figures; link the statement to one borrower's loans")
        self.state.loan_currency = first.currency
        self.state.fiscal_month = first.fiscal_year_end_month
        self.state.entity = first.statement_entity
        self.state.number_style = first.number_style
        return issues

    def _set_months(self, values: list[LineValue]) -> None:
        """The months each profit or cash-flow figure covers, from its page; a derived one takes its inputs'."""
        pages = {int(p): m for p, m in self.state.page_months.items()}
        for v in values:
            if v.key in FLOW_KEYS and not v.derived:
                v.months = pages.get(v.page) if v.page is not None else None
        known = [v.months for v in values if v.months]
        for v in values:
            if v.key in FLOW_KEYS and v.derived and known:
                v.months = max(set(known), key=known.count)

    def _recall(self, rows: list[tuple]) -> tuple[list[tuple], list[str]]:
        """Borrower memory: a wording a person confirmed for this CP before is read as that line again.

        Only a line the catalog does not map takes the remembered meaning; where the catalog now reads the
        wording as another line, the statement waits for a person (the memory and the catalog disagree).
        """
        from dps.extraction.similarity import canonical

        with self._deps.session_factory() as db:
            doc = db.get(Document, self.state.document_id)
            memory = services.label_memory(db, doc.active_cp_ids())
        if not memory:
            return rows, []
        out, issues, remembered = [], [], []
        for row in rows:
            label = row[0]
            hit = memory.get(canonical(label)[:300])
            if hit is None:
                out.append(row)
                continue
            key, _confirmed = hit
            now = match_key(label)
            if now == key == "INTEREST_PAID":
                self.state.net_interest_confirmed = True  # a person confirmed this interest line for the CP
            if now is None:
                out.append((LINE_ALIASES[key][0], *row[1:]))
                if key not in remembered:
                    remembered.append(key)
            else:
                out.append(row)
                if now != key:
                    issues.append(f"LABEL_MEMORY_CONFLICT: '{label[:80]}' was confirmed as {key}, now read as {now}")
        self.state.remembered = remembered
        return out, issues

    def _scale_jumps(self, values: list[LineValue]) -> list[str]:
        """A figure 20 times larger or smaller than the same CP's previous period: the unit was probably misread."""
        with self._deps.session_factory() as db:
            doc = db.get(Document, self.state.document_id)
            previous = services.previous_values(db, doc.active_cp_ids(), doc.as_of_date, SCALE_KEYS)
        out = []
        for v in values:
            before = previous.get(v.key)
            if v.derived or before is None or not before[0] or not v.value:
                continue
            ratio = abs(v.value / before[0])
            if ratio > SCALE_JUMP or ratio < 1 / SCALE_JUMP:
                code = currency_of(v.unit)
                words = "" if code is None else " in INR crore" if code == "INR" else f" in {code} million"
                out.append(f"SCALE_JUMP: {v.key} {v.value} against {before[0]} for {before[1].isoformat()}{words}")
        return out

    # ---------- 3. route ----------

    def _validate(self) -> None:
        if self.state.route == "heavy":
            self.state.outcome, self.state.route = "HEAVY", ""
            return
        if self.state.error or not self.state.rows:
            self.state.error = self.state.error or "No line items found in the statement"
            self.state.outcome = "FAILED"
            return
        # Figures reach the values table only after a person reviewed them (architect Flow 3: "Checker approves or
        # rejects"; full review G21). Every catalogue line not found is listed, never dropped silently.
        from dps.catalog import lines_not_found

        self.state.lines = lines_not_found(v["key"] for v in self.state.values)
        self.state.not_found = list(self.state.lines["notFound"])
        self.state.outcome = "NEEDS_REVIEW"


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""


# ---------- the statement as result fields ----------

def as_result(state: StatementState, as_of: date | None, report_derived: bool = False) -> tuple[list, list, dict]:
    """(fields, warnings, extra) for dps.result: one STATEMENT_LINE per figure read, every one for a person; the
    statement's issues as warnings; what ``publish`` needs later (currencies, comparatives) in ``extra``."""
    from dps.skills.common import issue, make_field

    fields = []
    as_of_text = as_of.isoformat() if as_of else None
    confirmed = set(state.confirmed)
    for i, v in enumerate(state.values):
        if v.get("derived") and not report_derived:
            continue  # G22: Spring works it out from the printed figures
        value = {"amount": v.get("value"), "unit": v.get("unit"), "currency": currency_of(v.get("unit")),
                 "asOfDate": as_of_text, "periodMonths": v.get("months"), "derived": bool(v.get("derived"))}
        f = make_field("statement_lines", [], "STATEMENT_LINE", v["key"], value, v.get("page"), v.get("label") or None,
                       "RULES+AI_CONFIRMED" if v["key"] in confirmed else "RULES",
                       0.95 if v["key"] in confirmed else 0.85, cell_ref=v.get("cell"), key=f"{v['key']}-{i}")
        f["status"] = "NEEDS_REVIEW"  # a statement is never reported unreviewed (G21)
        fields.append(f)
    warnings = []
    for text in state.issues:
        code, _, message = str(text).partition(":")
        warnings.append(issue(code.strip() or "ISSUE", message.strip() or None))
    if state.not_found:  # visible, not blocking: a person sees which lines the statement did not give
        warnings.append(issue("LINES_NOT_FOUND", f"{len(state.not_found)} statement line(s) not found: "
                                                 + ", ".join(state.not_found[:40])))
    extra = {"statement": {
        "currency": state.currency or None, "loanCurrency": state.loan_currency or None,
        "fxRate": state.fx_rate if state.fx_rate != 1.0 else None, "unitScale": state.unit_scale or None,
        "fiscalYearEndMonth": state.fiscal_month or None, "entity": state.entity or None,
        "numberStyle": state.number_style or None, "language": state.language, "likely": state.likely,
        "confirmed": state.confirmed, "remembered": state.remembered,
        "comparatives": {"prior": state.prior_values, "annual": state.annual_values,
                         "annualEnd": state.annual_end or None},
        "derivedReported": report_derived}, "_lines": state.lines}
    return fields, warnings, extra


# ---------- after review: the approved figures ----------

class CurrencyNotSettled(ValueError):
    """The figures are in another currency than the loans and no rate was applied: nothing is stored."""


def publish(db: Session, job: Job, result: dict, report_derived: bool = False, fiscal_year_end_month: int = 3,
            actor: str = ACTOR) -> int:
    """Stores the figures a person approved (CONFIRMED, CORRECTED, or left as read when the review completed) in
    the values table, for the bank app's data-source contract (``/api/data/values``). Idempotent: run again for the
    same document it replaces its own rows. Returns how many figures were stored.

    Not done here any more: no event to Core (G9: the job's ``dps.extraction.completed`` tells Spring), and no
    figure DPS works out (G22) unless ``report_derived`` is on."""
    doc = db.get(Document, job.document_id)
    if doc is None or doc.as_of_date is None:
        return 0
    meta = result.get("statement") or {}
    lines = [f for f in result.get("fields", []) if f.get("group") == "STATEMENT_LINE"
             and f.get("status") != "REJECTED" and (f.get("value") or {}).get("amount") is not None]
    values = [LineValue(f["name"], float(f["value"]["amount"]), f["value"].get("unit") or "",
                        bool(f["value"].get("derived")), (f.get("source") or {}).get("page"),
                        (f.get("source") or {}).get("cellRef"), ((f.get("source") or {}).get("quote") or "")[:120],
                        f["value"].get("periodMonths")) for f in lines]
    if not report_derived:
        values = [v for v in values if not v.derived]
    loan = money_unit(meta["loanCurrency"]) if meta.get("loanCurrency") else None
    if loan and any(v.unit and v.unit != loan for v in values):
        raise CurrencyNotSettled(f"The figures are in {meta.get('currency')}, the loan in {meta.get('loanCurrency')}: "
                                 "add the FX rate, then submit the statement again")
    loan_ccy = meta.get("loanCurrency") or "INR"
    corrected_keys = {f["name"] for f in lines if f.get("status") == "CORRECTED"}
    cp_ids = doc.active_cp_ids()
    comparatives = meta.get("comparatives") or {}
    for cp in cp_ids:
        kept_user = set()
        for old in db.scalars(select(StatementValue).where(
                StatementValue.cp_id == cp, StatementValue.as_of_date == doc.as_of_date,
                StatementValue.key.in_([v.key for v in values]), StatementValue.superseded == false())):
            if old.document_id == doc.id and old.source_type == "USER":
                kept_user.add(old.key)  # a person's figure for this statement stays
            elif old.document_id == doc.id:
                db.delete(old)
            else:
                old.superseded = True
        db.flush()
        for v in values:
            if v.key in kept_user:
                continue
            db.add(StatementValue(cp_id=cp, key=v.key, value=v.value, unit=v.unit,
                                  currency=currency_of(v.unit) or loan_ccy, as_of_date=doc.as_of_date,
                                  period_months=v.months, derived=v.derived,
                                  source_type="USER" if v.key in corrected_keys else "DOCUMENT",
                                  document_id=doc.id, page=v.page, cell=v.cell,
                                  entered_by=actor if v.key in corrected_keys else None))
        db.flush()
        months = next((v.months for v in values if v.key in ("PAT", "PBT", "REVENUE") and v.months), None)
        prior = [LineValue(**x) for x in comparatives.get("prior") or []]
        annual = [LineValue(**x) for x in comparatives.get("annual") or []]
        if not report_derived:
            prior, annual = [x for x in prior if not x.derived], [x for x in annual if not x.derived]
        services.store_comparatives(db, doc, cp, prior, annual, comparatives.get("annualEnd") or "", months)
        if report_derived:
            if corrected_keys:
                services._rederive(db, doc, cp, actor)  # noqa: SLF001 - totals from the corrected inputs
            else:
                services.build_twelve_months(db, doc, cp, meta.get("fiscalYearEndMonth") or fiscal_year_end_month)
    net = any(w.get("code") == "INTEREST_IS_NET" for w in result.get("warnings") or [])
    interest = next((v for v in values if v.key == "INTEREST_PAID" and v.label), None)
    if net and interest is not None:  # the reviewer kept the net interest: the next statement is not held for it
        for cp in cp_ids:
            services.remember_label(db, cp, interest.label, "INTEREST_PAID", doc.id, actor)
    doc.status = "PUBLISHED"
    # The statement's reading as approved (its year end, units, currency ...): recomputing a corrected statement's
    # twelve-month figures reads the borrower's year end from here.
    doc.review_json = json.dumps({**{k: v for k, v in meta.items() if k not in ("comparatives", "derivedReported")},
                                  "issues": [f"{w.get('code')}: {w.get('message') or ''}".rstrip(": ")
                                             for w in result.get("warnings") or []]}, default=str)
    services.audit(db, actor, "STATEMENT_APPROVED", "document", doc.id, values=len(values), cpIds=cp_ids)
    history.result(db, doc.id, job.id, "APPROVED",
                   f"after review, {len(values)} figures stored for {', '.join(cp_ids) or 'no CP'}")
    return len(values)

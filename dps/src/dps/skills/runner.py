"""The generic flow (Phase 4): one runner for every skill pack.

1. page model (phase 2) and context; 2. ``pack.rules`` proposals; 3. page selection; 4. the document agent
(gateway, grounded answers only); 5. voting rules vs agent per field; 6. verify: Java formula validation and covenant
library (Spring tools) and the pack's cross-checks;
7. route (phase 5 scoring replaces the simple rule here); 8. store the run and emit the event.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps import feed, jobs
from dps.models import Job, Run
from dps.ops import observability as obs
from dps.skills import Pack, for_job_type, load
from dps.skills.common import (
    MAX_PROMPT_CHARS,
    batched_proposals,
    issue,
    make_field,
    page_batches,
    select_pages,
    squash,
    vote,
)

log = logging.getLogger(__name__)
SCHEMA = "dps.result/1.0"
KEYWORDS = {
    "sanction": re.compile(r"sanction|limit|tenor|rate of interest|security|covenant", re.I),
    "certificate": re.compile(r"certif|covenant|ratio|complied|UDIN", re.I),
    "amendment_diff": re.compile(r"clause|amend|substitut|delet|insert|covenant", re.I),
    "schedule_terms": re.compile(r"within\s+\d+\s+days|instal|reset|cure|tested", re.I),
}
UNREADABLE_QUALITY = 0.3  # below this, OCR could not read the page well enough to trust any value on it
BLOCKING = frozenset({"NOTHING_FOUND", "NOT_GROUNDED", "VERDICT_NOT_IN_RESULTS", "UNREADABLE_PAGES",
                      "TOTALS_DO_NOT_ADD", "PERIOD_MISMATCH", "BASIS_MISMATCH", "ENTITY_MISMATCH",
                      "CURRENCY_MISMATCH", "UNITS_MISMATCH", "BASE_MISSING", "SET_INCOMPLETE",
                      "SET_MANIFEST_INVALID",
                      # a statement with no period date sent and none in its heading: its figures cannot be stored
                      "PERIOD_DATE_MISSING",
                      # a statement's holds a person clears outside it (an FX rate, a borrower setting, relinking)
                      "CURRENCY_DIFFERS", "CURRENCY_MIXED", "LOAN_CURRENCY_UNKNOWN", "LOANS_DIFFER",
                      # a document that addresses an AI model, or that the rules cannot read (another language)
                      "PROMPT_INJECTION_SUSPECTED", "LANGUAGE_NOT_READ",
                      # a document of another borrower, or of another type than the caller chose: a person accepts it
                      # before the job can close (CTS review, 23 Nov 2026)
                      "MAPPING_CONFLICT", "TYPE_MISMATCH"})
COVENANT_GROUPS = ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT")
# Inspection and reader flags a reviewer must see on an extraction result (full review G15).
FILE_FLAGS = {"TRACKED_CHANGES": "the text read is with every change accepted",
              "COMMENTS": "comments are not read", "CONTENT_CONTROLS": "form fields were read",
              "HIDDEN_SHEETS": "hidden sheets were read", "HIDDEN_ROWS_OR_COLUMNS": "hidden rows or columns were read",
              "NO_CACHED_VALUE": "some formula cells have no saved value", "EXTERNAL_LINKS": "links to other files",
              "REPAIRED": "the PDF was damaged and repaired", "OWNER_RESTRICTED": "the PDF has owner restrictions",
              "IMAGES": "the file has pictures that are not read", "POOR_TEXT": "much of the text layer is poor",
              "IMAGE_REGIONS": "pages with large pictures were also OCR-read",
              "SEARCHABLE_SCAN": "pages are pictures with the scanner's text layer; that text was read",
              "FORM_SCRIPTS": "a fillable form with field scripts; the scripts were not run",
              "FORMULA_TEXT": "cells start with =, +, - or @"}


RATIO_UNITS = {"TIMES", "PERCENT", "RATIO", "MULTIPLE", "X", "%"}
BUDGET_WARN_SHARE = 0.8


def budget_warning(budget) -> dict | None:
    """A job that used most of its token budget (``TOKEN_BUDGET_HIGH``): a longer document of the same kind may not
    be read in full, so operations sees it before it happens (the residual read and review items still catch what a
    spent budget leaves unread). Not blocking."""
    limit = getattr(budget, "limit", None)
    used = getattr(budget, "used", 0) or 0
    if not limit or used < BUDGET_WARN_SHARE * limit:
        return None
    return issue("TOKEN_BUDGET_HIGH", f"used {used:,} of {limit:,} tokens ({100 * used / limit:.0f}%); a longer "
                                      "document may not be read in full")


def check_numbers(fields: list[dict]) -> None:
    """A covenant's threshold must be written in its own quote (outside the agent: the model cannot vouch for its own
    number). Ratios and percentages only; amounts may be written in words or scaled ("Rs. 50 crore")."""
    from dps.extraction.verify import threshold_in

    for f in fields:
        value = f.get("value") or {}
        threshold, quote = value.get("threshold"), (f.get("source") or {}).get("quote")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not quote:
            continue
        unit = str(value.get("unit") or "").upper()
        if not (unit in RATIO_UNITS or (not unit and abs(threshold) < 100)):
            continue
        if threshold_in(quote, float(threshold)):
            continue
        if any(i.get("code") == "NUMBER_NOT_IN_QUOTE" for i in f.get("issues") or []):
            continue
        f.setdefault("issues", []).append(issue("NUMBER_NOT_IN_QUOTE", f"the threshold {threshold:g} is not "
                                                                       "written in the quoted text; check the source"))
        f["status"] = "NEEDS_REVIEW"


def document_checks(model: dict | None, ai_on: bool) -> list[dict]:
    """What the old agreement flow checked on the whole document, now for every reading job (one pipeline, WP7):
    text written to steer an AI model (PROMPT_INJECTION_SUSPECTED), and a document in another language when no AI
    reader is on (LANGUAGE_NOT_READ: the rules read English only). Both send the job to a person."""
    from dps.agent.guard import injection_signs
    from dps.language import detect, name

    text = "\n".join(p.get("text") or "" for p in (model or {}).get("pages", []))
    if not text.strip():
        return []
    out = []
    signs = injection_signs(text)
    if signs:
        out.append(issue("PROMPT_INJECTION_SUSPECTED", f"the document addresses an AI model: '{signs[0][:120]}'"))
    lang = detect(text)
    if lang != "en" and not ai_on:
        out.append(issue("LANGUAGE_NOT_READ", f"the document is in {name(lang)}; the rules read English only. "
                                              "Switch on the AI reader (DPS_AI_MODEL) or enter its values by hand"))
    return out


def page_accounting(pages: list[dict]) -> dict:
    """Every page's outcome: read from its text, read by OCR, blank, or unreadable (so not trusted), with the pages
    that were not read well listed."""
    counts = {"PARSED": 0, "OCR": 0, "BLANK": 0, "UNREADABLE": 0}
    listed = []
    for p in pages:
        method = str(p.get("sourceMethod") or p.get("method") or "TEXT").upper()
        if method == "BLANK":
            state = "BLANK"
        elif float(p.get("quality", 1.0)) < UNREADABLE_QUALITY:
            state = "UNREADABLE"
        elif method == "OCR":
            state = "OCR"
        else:
            state = "PARSED"
        counts[state] += 1
        if state != "PARSED":
            listed.append({"no": p.get("no"), "state": state, "quality": round(float(p.get("quality", 0.0)), 2)})
    return {"total": len(pages), **{k.lower(): v for k, v in counts.items()},
            "readable": counts["PARSED"] + counts["OCR"], "accounted": len(pages), "notParsed": listed[:200]}


def verify_with_spring(fields: list[dict], tools, correlation_id: str | None, as_of=None) -> list[dict]:
    """Covenant fields: library match, the condition in the bank app's formula language and its validation in Java
    (batched, one call each). Runs after the taxonomy stamp and the scope enrichment, so the formula is built on the
    final metric and its mapping sees conditions and scope (CTS integration, 23 Nov 2026; see skills/app_formula)."""
    from dps.skills import app_formula

    warnings: list[dict] = []
    covenants = [f for f in fields if f["group"] in COVENANT_GROUPS and isinstance(f.get("value"), dict)]
    if not covenants:
        return warnings
    library: list = []
    if tools is not None:
        library, fresh = tools.covenant_library()
        if not library and not fresh:
            warnings.append(issue("SPRING_UNAVAILABLE", "covenant library not reached; library matches missing"))
    app_formula.apply(covenants, library, as_of=as_of)
    items = [(f["id"], f["value"]["expression"]) for f in covenants if f["value"].get("expression")]
    if tools is None or not items:
        return warnings
    app_formula.record_checks(covenants, tools.validate_formulas(items, correlation_id))
    return warnings


# An amendment's covenants the agent alone read are possible covenants, as an agreement's are: they were all filed as
# FINANCIAL_COVENANT, so "Personal Data processing procedures" was a financial covenant (bulk run, 22 Nov 2026).
AGENT_GROUPS = {"sanction": "AGREEMENT_TERM", "certificate": "CERTIFIED_FIGURE", "schedule_terms": "SCHEDULE_TERM",
                "amendment_diff": "POSSIBLE_COVENANT", "covenants": "POSSIBLE_COVENANT", "waiver": "WAIVER"}


COVENANT_KEYS = ("metric", "operator", "threshold", "unit", "expression")
SCHEDULE_KINDS = ("TESTING_CALENDAR", "REPORTING_DEADLINE", "REPAYMENT", "INTEREST_RESET", "CURE_WINDOW")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _schedule_value(value: dict, name: str) -> dict:
    """The agent's schedule term in the contract's shape (result check of 22 Nov 2026): kind one of the five (else
    OTHER), frequency MONTHLY … YEARLY or null, dates only real dates. Words that do not fit are kept in rule."""
    from dps.skills.common import amount
    from dps.skills.schedule_terms.rules import _freq

    raw_kind = str(value.get("kind") or name or "")
    kind = re.sub(r"[\s-]+", "_", raw_kind.strip()).upper()
    raw_freq = value.get("frequency")
    freq = raw_freq if raw_freq in (None, "MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY") else _freq(str(raw_freq))
    raw_dates = value.get("dates")
    raw_dates = [raw_dates] if isinstance(raw_dates, str) else raw_dates if isinstance(raw_dates, list) else []
    dates = [d for d in raw_dates if isinstance(d, str) and ISO_DATE.match(d)]
    days = value.get("daysAfterPeriodEnd")
    days = days if isinstance(days, int) and not isinstance(days, bool) else (
        int(days) if isinstance(days, str) and days.strip().isdigit() else None)
    money = value.get("amount")
    money = float(money) if isinstance(money, (int, float)) and not isinstance(money, bool) else (
        amount(money) if isinstance(money, str) else None)
    kept = [str(x) for x in (
        None if kind in SCHEDULE_KINDS else f"kind: {raw_kind}",
        None if freq or not raw_freq else f"when: {raw_freq}",
        *[f"date: {d}" for d in raw_dates if d not in dates and d]) if x]
    rule = "; ".join([str(x) for x in [value.get("rule"), *kept] if x])[:300] or None
    kind = kind if kind in SCHEDULE_KINDS else "OTHER"
    return {**value, "kind": kind, "frequency": freq, "dates": dates, "daysAfterPeriodEnd": days, "amount": money,
            "rule": rule}


_METHOD_RANK = {"RULES+AI_CONFIRMED": 0, "REGEX": 1, "RULES": 1, "AI": 2, "AI_VISION": 2, "AI_RESIDUAL": 3}
_MERGE_GROUPS = (*COVENANT_GROUPS, "EVENT_OF_DEFAULT")


def _code_of(f: dict) -> str:
    v = f.get("value") or {}
    return str(v.get("code") or v.get("metric") or "")


def _generic(f: dict) -> bool:
    code = _code_of(f).upper()
    return not code or code.endswith("OTHER")


def _second_covenant(f: dict, keep: dict) -> bool:
    """Preserve a second specific threshold even when both readings share the same quote and metric."""
    if _generic(f) or _generic(keep):
        return False
    mine, theirs = (f.get("value") or {}).get("threshold"), (keep.get("value") or {}).get("threshold")
    return isinstance(mine, (int, float)) and not isinstance(mine, bool) and mine != theirs


def merge_same_sentence(fields: list[dict]) -> list[dict]:
    """One sentence, one finding (review of 22 Nov 2026: 24 sentences gave 50 fields, mostly two AI parts reading the
    same sentence, one with a specific code and one with *_OTHER). Fields with the same page and quote are merged
    into the best one: rules before AI, a specific code before OTHER. Only AI fields are dropped; their codes stay on
    the kept field as ``value.alsoCodes`` and an issue says so. A rules finding is never dropped, and an event of
    default is never merged into a covenant."""
    by_sentence: dict[tuple, list[dict]] = {}
    for f in fields:
        source = f.get("source") or {}
        if f.get("group") in _MERGE_GROUPS and source.get("quote"):
            by_sentence.setdefault((source.get("page"), squash(source["quote"])), []).append(f)
    dropped: set[int] = set()
    for same in by_sentence.values():
        if len(same) < 2 or not any(str(f.get("method", "")).startswith("AI") for f in same):
            continue
        keep = min(same, key=lambda f: (_METHOD_RANK.get(f.get("method"), 2), _generic(f)))
        merged = []
        for f in same:
            if f is keep or not str(f.get("method", "")).startswith("AI"):
                continue
            if (f["group"] == "EVENT_OF_DEFAULT") != (keep["group"] == "EVENT_OF_DEFAULT"):
                continue
            if _second_covenant(f, keep):
                continue  # "DSCR of at least 1.25 and Leverage of at most 3.5": two covenants, both kept
            dropped.add(id(f))
            code = _code_of(f)
            if code and code != _code_of(keep) and code not in merged:
                merged.append(code)
        if merged:
            keep["value"]["alsoCodes"] = [*keep["value"].get("alsoCodes", []), *merged]
            keep["issues"].append(issue("SAME_SENTENCE", f"{len(merged)} other reading(s) of this sentence merged "
                                                         f"here; other codes: {', '.join(merged)}"))
    return [f for f in fields if id(f) not in dropped]


def _agent_only(pack: Pack, pages: list[dict], fields: list[dict], proposals: list[dict] | None,
                method: str = "AI") -> list[dict]:
    """Agent proposals the rules did not find: kept with method AI (or AI_VISION), high-risk ones and every covenant
    and image read to review. A person decides; the agent never adds an accepted covenant on its own."""
    out = []
    group = AGENT_GROUPS.get(pack.name)
    if group is None:
        return out
    for p in proposals or []:
        if p.get("_matched") or not p.get("page") or not p.get("quote"):
            continue
        value = dict(p.get("value") or {})
        if group == "AGREEMENT_TERM":
            value = {"key": p["name"], **value}
        if group == "SCHEDULE_TERM":
            value = _schedule_value(value, p["name"])
        if group == "CERTIFIED_FIGURE":
            value = {"covenant": value.get("covenant", p["name"]), "figure": value.get("figure"), **value}
        # the contract's covenant value has these keys on every covenant (22 Nov 2026), also on an amendment's
        # covenants the agent read (bulk run: Holly's six broke result.json, "'expression' is a required property")
        unread_op = None
        if group in COVENANT_GROUPS:
            from dps.skills.app_formula import normalise_operator

            value = {**{k: None for k in COVENANT_KEYS}, **value,
                     "metric": value.get("metric") or p.get("_written") or p["name"]}
            # the agent's operator in the contract's terms ("≥", "minimum" -> ">="); anything else is not an operator
            raw_op = value.get("operator")
            value["operator"] = normalise_operator(raw_op)
            if raw_op is not None and value["operator"] is None:
                unread_op = str(raw_op)[:40]
            threshold = value.get("threshold")
            if isinstance(threshold, str):  # "1.25x", "3.50:1" -> 1.25, 3.5
                m = re.search(r"-?\d+(?:\.\d+)?", threshold.replace(",", ""))
                value["threshold"] = float(m.group(0)) if m else None
            elif isinstance(threshold, bool):
                value["threshold"] = None
            value["expression"] = None  # always built by DPS in the app's formula language (app_formula)
        if group == "WAIVER":  # the agent's reading in the waiver shape; the target is matched by a person
            from dps.skills.waiver.rules import KINDS

            kind = str(value.get("kind") or "WAIVER").upper()
            extended_to = value.get("extendedTo")
            extended_to = extended_to if isinstance(extended_to, str) and ISO_DATE.match(extended_to) else None
            value = {"kind": kind if kind in KINDS else "WAIVER",
                     **({"extendedTo": extended_to} if kind == "EXTENSION" else {}),
                     "target": {"metric": value.get("metric"), "clause": value.get("clause"),
                                "appliedCovenantId": None, "name": p.get("_written") or p["name"]},
                     "periods": [x for x in value.get("periods") or [] if isinstance(x, dict)],
                     "conditions": [str(x)[:300] for x in value.get("conditions") or []],
                     "expiresOn": value.get("expiresOn"), "reinstatement": None, "fee": None,
                     "metric": value.get("metric") or "OTHER"}
        f = make_field(pack.name, pages, group, p.get("_written") or p["name"], value, p["page"], p["quote"],
                       method=method,
                       confidence=0.6 if method == "AI_VISION" else 0.7,
                       key=method.lower() + (f"-{p['_residual']}" if p.get("_residual") else ""))
        if p.get("_span"):  # the agent read's trace span; moved into dps_trace_event by finish(), never sent to Spring
            f["_aiSpan"] = p["_span"]
        if unread_op:
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("OPERATOR_UNREAD", f"the Document Agent wrote '{unread_op}', which is not an "
                                                        "operator; set it from the quote"))
        if group == "SCHEDULE_TERM" and value["kind"] == "OTHER":
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("AI_ONLY", "found by the Document Agent only; its kind is not one the bank knows"))
        if method == "AI_VISION":
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("READ_FROM_IMAGE", f"page {p['page']} was read from its image; confirm it"))
        elif pack.risk(p["name"]) == "HIGH" or group == "POSSIBLE_COVENANT":
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("AI_ONLY", "found by the Document Agent only"))
        out.append(f)
    return out


def _covenant_key(f: dict) -> str:
    """Covenants are matched on their taxonomy code (the rules' DEBT_TO_NET_WORTH and the agent's FIN_DEBT_TO_EQUITY
    are one covenant); a name the taxonomy does not know is matched as written."""
    from dps.skills.covenants.names import code_of

    raw = (f.get("value") or {}).get("metric") or f.get("name") or ""
    return code_of(raw) or re.sub(r"[^a-z0-9]", "", str(raw).lower())


def _same_test(a: dict, b: dict | None) -> bool:
    """Covenant rules vs agent: the same operator and threshold (names and wording may differ)."""
    b = b or {}
    ta, tb = a.get("threshold"), b.get("threshold")
    same_threshold = (ta is None and tb is None) or (
        isinstance(ta, (int, float)) and isinstance(tb, (int, float)) and abs(float(ta) - float(tb)) < 1e-6)
    return same_threshold and (a.get("operator") or None) == (b.get("operator") or None)


def _same_change(a: dict, b: dict | None) -> bool:
    return _same_test(a, b) and ((a.get("change") or {}).get("type") == ((b or {}).get("change") or {}).get("type"))


def _basis(f: dict) -> str | None:
    return ((f.get("test") or {}).get("basis") or (f.get("value") or {}).get("basis") or None)


def _party(name) -> str | None:
    from dps.skills.covenants.obligation import _party as spelled

    return spelled(str(name)).lower() if name else None


def _same_party(f: dict, party: str | None) -> bool:
    """The rules field binds the party the agent named (WP6, F4: the Borrower's DSCR and the Guarantor's DSCR are
    two covenants, never one). A field whose quote names no party matches any."""
    from dps.skills.covenants.obligation import obligors

    stated = [p.lower() for p in obligors((f.get("source") or {}).get("quote"))]
    return party is None or not stated or party in stated


def _vote_covenants(fields: list[dict], proposals: list[dict], compare) -> list[dict]:
    """Rules vs agent, one to one. Two covenants on the same metric (a DSCR for the company and one for the group, or
    two levels) each get their own match: an unmatched rule covenant with the same test first, then the same basis
    (STANDALONE / CONSOLIDATED), then the same page, then any unmatched one. An agent item that repeats a covenant
    already matched with the same test is a duplicate and changes nothing."""
    taken: set[int] = set()
    by_key: dict[str, list[dict]] = {}
    for f in fields:
        by_key.setdefault(_covenant_key(f), []).append(f)
    for p in proposals:
        group = by_key.get(p.get("name")) or []
        if not group:
            continue
        free = [f for f in group if id(f) not in taken]
        value, page, basis = p.get("value") or {}, p.get("page"), (p.get("value") or {}).get("basis")
        party = _party(value.get("obligor"))
        pick = (next((f for f in free if compare(f["value"], value) and _same_party(f, party)), None)
                or next((f for f in free if party and _same_party(f, party)
                         and (not basis or _basis(f) in (None, basis))), None)
                or next((f for f in free if compare(f["value"], value) and not party), None)
                or next((f for f in free if basis and _basis(f) == basis), None)
                or next((f for f in free if page is not None and (f.get("source") or {}).get("page") == page), None)
                or (free[0] if free else None))
        if pick is None:
            if any(compare(f["value"], value) for f in group):
                p["_matched"] = True  # the same covenant read twice by the agent
            continue
        taken.add(id(pick))
        vote([pick], [{**p, "name": _covenant_key(pick)}], key=_covenant_key, compare=compare)
        p["_matched"] = True
    return fields


def _vote(pack: Pack, fields: list[dict], proposals: list[dict] | None) -> list[dict]:
    if pack.name == "waiver" and proposals:  # waivers are matched by the covenant they waive, then the periods
        for p in proposals:
            p.setdefault("_written", p.get("name"))
            p["name"] = _covenant_key({"value": p.get("value"), "name": p.get("name")}) or p.get("name")
        return vote(fields, proposals, key=lambda f: _covenant_key({"value": {"metric": (f["value"].get("target") or {})
                                                                               .get("metric")}, "name": f["name"]}),
                    compare=_same_periods)
    if pack.name in ("covenants", "amendment_diff") and proposals:
        for p in proposals:  # covenants are matched by metric, not by the name as written
            p.setdefault("_written", p.get("name"))
            p["name"] = _covenant_key({"value": p.get("value"), "name": p.get("name")}) or p.get("name")
        return _vote_covenants(fields, proposals, _same_change if pack.name == "amendment_diff" else _same_test)
    if pack.name == "certificate" and proposals:
        return _vote_figures(fields, proposals)
    return vote(fields, proposals)


def _same_sentence(field: dict, proposal: dict) -> bool:
    """The rule's finding and the agent's proposal quote the same sentence of the same page."""
    page = (field.get("source") or {}).get("page")
    if not page or page != proposal.get("page"):
        return False
    a, b = squash((field.get("source") or {}).get("quote") or ""), squash(proposal.get("quote") or "")
    if not a or not b:
        return False
    return a[:80] in b or b[:80] in a


def _vote_schedule(fields: list[dict], proposals: list[dict] | None) -> list[dict]:
    """Schedule terms are matched by kind AND sentence: an agreement has many reporting deadlines (accounts within
    30 days, insurance certificates, the compliance certificate ...). Matched by kind alone, every one the agent read
    was taken for the rules' single deadline, marked MODELS_DISAGREE and dropped (bulk run, 22 Nov 2026: 43 such)."""
    if proposals is None:
        return fields
    for f in fields:
        same = [p for p in proposals
                if not p.get("_matched") and p.get("name") == f.get("name") and _same_sentence(f, p)]
        if same:
            vote([f], same[:1])
    return fields


def _figure(value) -> float | None:
    try:
        return round(float((value or {}).get("figure")), 4)
    except (TypeError, ValueError):
        return None


def _vote_figures(fields: list[dict], proposals: list[dict]) -> list[dict]:
    """A certificate's figure read by the rules ("DSCR 2.22") and by the AI ("Historic Annual Debt Service Cover
    Ratio 2.22") is one finding, confirmed (bulk run, 22 Nov 2026: it was listed twice). They are the same only
    when they mean the same covenant and give the same figure: "Projected Annual DSCR 1.98" stays its own finding."""
    from dps.skills.covenants.taxonomy import get as taxonomy

    tax = taxonomy()

    def code(name) -> str:
        return tax.code_for(name) or squash(str(name or ""))

    for p in proposals:
        figure = _figure(p.get("value"))
        if figure is None:
            continue
        for f in fields:
            if f.get("group") == "CERTIFIED_FIGURE" and code(f.get("name")) == code(p.get("name")) and \
                    _figure(f.get("value")) == figure:
                p["_matched"] = True
                f["method"] = "RULES+AI_CONFIRMED"
                f["confidence"] = round(max(f.get("confidence") or 0, 0.95), 3)
                break
    return fields


def _same_periods(a: dict, b: dict | None) -> bool:
    def norm(v):
        return sorted((p.get("from"), p.get("to"), p.get("testDate")) for p in (v or {}).get("periods") or []
                      if isinstance(p, dict))

    return norm(a) == norm(b)


def _explain_changes(fields: list[dict], proposals: list[dict] | None) -> None:
    """Amendment: the agent's one-line explanation of each change, next to the change the rules found (text only)."""
    by_key = {p.get("name"): p for p in proposals or [] if p.get("_matched")}
    for f in fields:
        change = (f.get("value") or {}).get("change")
        p = by_key.get(_covenant_key(f))
        if change is None or p is None:
            continue
        text = ((p.get("value") or {}).get("change") or {}).get("explanation")
        if text:
            change["explanation"] = str(text)[:500]
            change["explanationBy"] = "AI"


def approved_base(base: list[dict]) -> list[dict]:
    """G2: Spring's approved covenants (context.baseCovenants) as result fields, keyed by metric. The bank app sends
    its formula ("DSCR >= 1.25") and often nothing else: metric, operator, threshold and unit are read from it when
    not sent, so an unchanged covenant is not reported as changed (CTS integration, 23 Nov 2026)."""
    from dps.extraction.agreement import _best_metric
    from dps.skills.app_formula import normalise_operator, read_formula

    out = []
    for c in base:
        parsed = read_formula(c.get("expression")) or {}
        metric = c.get("metric") or parsed.get("metric") or _best_metric(c.get("name", "")) or c.get("name", "")
        threshold = c.get("threshold") if c.get("threshold") is not None else parsed.get("threshold")
        group = "NON_FINANCIAL_COVENANT" if c.get("category") == "NON_FINANCIAL" else "FINANCIAL_COVENANT"
        out.append({"id": c["appliedCovenantId"], "group": group, "name": c.get("name") or metric,
                    "value": {"metric": metric,
                              "operator": normalise_operator(c.get("operator")) or parsed.get("operator"),
                              "threshold": threshold, "unit": c.get("unit") or parsed.get("unit"),
                              "expression": c.get("expression"),
                              "appliedCovenantId": c["appliedCovenantId"], "clause": c.get("clause")},
                    "source": {"page": None, "quote": None}, "method": "MANUAL", "confidence": 1.0,
                    "issues": [], "status": "CONFIRMED"})
    return out


def base_fields(db: Session, job: Job, base_version: str | None) -> tuple[list[dict], str | None]:
    """Return the fields and database ID from the same saved DPS result."""
    if not base_version:
        return [], None
    base = db.scalars(select(Job).where(Job.tenant_id == job.tenant_id, Job.document_version_id == base_version,
                                        Job.job_type.in_(("EXTRACT_AGREEMENT", "EXTRACT_AMENDMENT", "ONBOARD",
                                                          "AUTO")),
                                        Job.status.in_((jobs.DONE, jobs.NEEDS_REVIEW)))
                      .order_by(Job.finished_at.desc())).first()
    if base is None:
        return [], None
    run = db.scalars(select(Run).where(Run.job_id == base.id).order_by(Run.run_no.desc())).first()
    if run is None:
        return [], None
    result = json.loads(run.result_json)
    return result.get("amendedSet") or result.get("fields", []), run.id


def token_budget(pack: Pack, model: dict | None) -> int | None:
    """The job's token budget: the pack's, raised for long documents by ``token_budget_per_page`` up to
    ``token_budget_max`` (CTS review, 23 Nov 2026: a fixed budget ran out part-way through long agreements, and the
    batches after it were not read). The month's budget still caps every job."""
    base = pack.limits.get("token_budget")
    per_page, ceiling = pack.limits.get("token_budget_per_page"), pack.limits.get("token_budget_max")
    pages = len((model or {}).get("pages") or [])
    if not base or not per_page or not pages:
        return base
    return max(base, min(per_page * pages, ceiling or base))


def run(pack: Pack, job: Job, model: dict | None, gateway=None, tools=None, db: Session | None = None,
        pipeline_version: str = "1", raw: bytes | None = None, scorer_dir: str | None = None,
        statement_deps=None) -> dict:
    started = time.monotonic()
    request = json.loads(job.request_json or "{}")
    ctx = dict(request.get("context") or {})
    budget = gateway.begin_job(token_budget(pack, model)) if gateway is not None else None
    flags = [f for f in (model or {}).get("flags", []) if f in FILE_FLAGS]
    extra: dict = {}
    with obs.span(f"dps.pack {pack.name}", **{"dps.pack": pack.name, "dps.ai": gateway is not None}) as span_obj:
        try:
            if pack.name == "commentary":
                fields, warnings, extra = _commentary(pack, raw or b"{}", gateway)
            elif pack.name == "reconcile_set":
                fields, warnings, extra = _reconcile(job, raw or b"", db, pipeline_version, ctx)
            elif pack.name == "statement_lines":
                fields, warnings, extra = _statement(job, db, statement_deps, flagged=bool(flags))
            else:
                fields, warnings, extra = _extract(pack, job, model or {"pages": []}, ctx, gateway, tools, db)
        finally:
            if gateway is not None:
                gateway.end_job()
        obs.set_attrs(span_obj, **{"dps.fields": len(fields), "dps.ai_fields": sum(
            1 for f in fields if str(f.get("method", "")).startswith("AI")),
            "dps.tokens": getattr(budget, "used", None), "dps.ai_calls": getattr(budget, "calls", None)})
    high = budget_warning(budget)
    if high:
        warnings.append(high)
    if flags:  # G15: tracked changes, hidden sheets, a repaired PDF ... the reviewer must know
        warnings.append(issue("FILE_FLAGS", "the file has: " + ", ".join(f"{f} ({FILE_FLAGS[f]})" for f in flags)))
    if pack.name == "certificate" and model:  # spec Flow 3: the certificate against what the cycle expects
        from dps.analyze import obligations
        from dps.analyze.identify import find_identifiers

        warnings += obligations.check(ctx, find_identifiers(model), model=model)
    from dps.skills.covenants.codes import stamp

    stamp(fields)  # taxonomy codes on every covenant-like field; defaults marked and sent to a person
    built = pack.name == "reconcile_set"  # a set is built from reviewed runs: no new reading, no new score
    if not built:
        from dps.skills.covenants.obligation import enrich

        with obs.span("dps.enrich", **{"dps.fields": len(fields)}):
            enrich(fields, (model or {}).get("pages"), ctx)  # WP6: company, facility, conditions, first test date
    if pack.name not in ("commentary", "reconcile_set", "statement_lines"):
        from dps.skills.app_formula import parse_date

        # library match + the bank app's formula, on the final metric and scope (CTS integration, 23 Nov 2026)
        warnings += verify_with_spring(fields, tools, job.correlation_id, as_of=parse_date(ctx.get("asOfDate")))
    if fields and not built:  # phase 5: every field's confidence from the scorer, routed by the pack's thresholds
        from dps.scoring import Scorer

        quality = {p["no"]: (float(p.get("quality", 1.0)), p.get("method", "NATIVE"))
                   for p in (model or {}).get("pages", [])}
        with obs.span("dps.scoring", **{"dps.fields": len(fields)}) as score_span:
            Scorer(scorer_dir).apply(fields, pack.name, pack.risk, pack.thresholds, quality, job_id=job.id or "")
            obs.set_attrs(score_span, **{"dps.auto_accepted": sum(
                1 for f in fields if f.get("status") == "AUTO_ACCEPTED")})
    check_numbers(fields)  # after scoring: a number not in its own quote always goes to a person
    if pack.name not in ("commentary", "reconcile_set", "statement_lines"):  # the statement reader checks its own
        warnings += document_checks(model, gateway is not None)
    if any(w["code"] == "PROMPT_INJECTION_SUSPECTED" for w in warnings):
        for f in fields:  # text written to steer an AI model: every value waits for a person, the reason on each
            if f.get("status") in ("AUTO_ACCEPTED", "NEEDS_REVIEW"):
                f["status"] = "NEEDS_REVIEW"
                if not any(i.get("code") == "PROMPT_INJECTION_SUSPECTED" for i in f.get("issues") or []):
                    f.setdefault("issues", []).append(issue("PROMPT_INJECTION_SUSPECTED",
                                                            "the document addresses an AI model"))
    if pack.name not in ("commentary", "reconcile_set"):
        extra["_pages"] = page_accounting((model or {}).get("pages", []))
        if not [f for f in fields if f.get("group") != "SCHEDULE_TERM"] \
                and not any(w["code"] == "NOTHING_FOUND" for w in warnings):
            readable = extra["_pages"]["readable"]
            warnings.append(issue("NOTHING_FOUND", "nothing was found in a readable document; a person confirms "
                                                   "it really has none" if readable else
                                  "no readable text was recovered, so nothing could be found; a person reads it"))
    if getattr(job, "rollout", "NORMAL") == "ASSISTED":  # phase 7 pilot: analysts review every field
        for f in fields:
            if f["status"] == "AUTO_ACCEPTED":
                f["status"] = "NEEDS_REVIEW"
                f["signals"] = {**(f.get("signals") or {}),
                                "reasons": [*((f.get("signals") or {}).get("reasons") or []), "ASSISTED_MODE"]}
    to_review = [f for f in fields if f["status"] == "NEEDS_REVIEW"]
    blocking = [w for w in warnings if w["code"] in BLOCKING]
    route = "NEEDS_REVIEW" if (to_review or blocking) else "AUTO_ACCEPTED"
    if route == "NEEDS_REVIEW" and any(w["code"] in ("ENTITY_MISMATCH", "MAPPING_CONFLICT") for w in blocking):
        route = "MAPPING_REVIEW"
    reasons = ([f"{len(to_review)} field(s) need review"] if to_review else []) + [w["code"] for w in blocking]
    result = {
        "schema": SCHEMA, "runId": job.id, "jobId": job.id, "jobType": job.job_type,
        "status": "DONE" if route == "AUTO_ACCEPTED" else "NEEDS_REVIEW",
        "documentId": job.document_id, "documentVersionId": job.document_version_id, "sha256": job.sha256,
        "pipeline": {"version": pipeline_version, "skillPack": pack.ref, "taxonomyVersion": _taxonomy_version(),
                     "models": dict(budget.models) if budget and budget.models else None,
                "modelPins": dict(budget.pins) if budget and getattr(budget, "pins", None) else None,
                     "packs": _packs_used(pack)},
        "classification": None, "identifiers": [], "fields": fields,
        "document": {"confidence": None, "route": route, "reasons": reasons,
                     **({"coverage": extra.pop("_coverage")} if "_coverage" in extra else {}),
                     **({"lines": extra.pop("_lines")} if "_lines" in extra else {}),
                     **({"pages": extra.pop("_pages")} if "_pages" in extra else {})},
        "warnings": warnings, "timingMs": {"total": round((time.monotonic() - started) * 1000, 1)},
        "usage": {"model_calls": budget.calls, "tokens": budget.used} if budget else None,
        "rollout": getattr(job, "rollout", None) or "NORMAL",
        **extra,
    }
    return result


def _packs_used(pack: Pack) -> dict:
    """Each skill pack this result used, with its version and the hash of its prompt (full review G20)."""
    used = [pack] + ([load("schedule_terms")] if pack.name in ("sanction", "covenants", "amendment_diff") else [])
    out = {p.name: {"version": p.version, "promptSha": _sha(p.prompt)} for p in used}
    # Every other prompt text that can reach the model for this result (improvement plan A2, 24 Nov 2026: only the
    # pack prompts were hashed): the four covenant parts' scopes, the residual read and the document-type check.
    from dps.analyze.classify import PROMPT as CLASSIFY_PROMPT
    from dps.skills.covenants import omissions, parts

    if pack.name in ("covenants", "sanction"):
        out["covenants.parts"] = {"version": pack.version, "promptSha": _sha("|".join(
            f"{p.name}:{p.scope}:{p.exclusions}:{','.join(p.categories)}" for p in parts.PARTS.values()))}
        out["covenants.residual"] = {"version": pack.version, "promptSha": _sha(omissions.INSTRUCTIONS)}
    out["classify"] = {"version": "1", "promptSha": _sha(CLASSIFY_PROMPT)}
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def _taxonomy_version() -> str:
    from dps.skills.covenants.taxonomy import get

    return get().version


def _extraction_batches(pack: Pack, pages: list[dict], fields: list[dict], max_pages: int):
    if pack.page_selection.get("rules") == "covenant_section":  # rules, sections, duty sentences, definitions
        from dps.skills.covenants.pages import covenant_batches

        return covenant_batches(pages, fields, max_pages, MAX_PROMPT_CHARS, pack.page_selection.get("max_batches", 1))
    if pack.name == "amendment_diff" and len(pages) <= max_pages:
        return page_batches(pages), None
    selected = select_pages(pages, fields, KEYWORDS.get(pack.name), max_pages)
    return (page_batches(selected) if pack.name == "amendment_diff" else [selected]), None


def _extract(pack: Pack, job: Job, model: dict, ctx: dict, gateway, tools, db):
    pages = model.get("pages", [])
    base_run_id = None
    if pack.name in ("amendment_diff", "waiver"):
        if ctx.get("baseCovenants"):
            ctx["base"], ctx["baseSource"] = approved_base(ctx["baseCovenants"]), "CTS_APPROVED"
        elif db is not None:
            ctx["base"], base_run_id = base_fields(db, job, ctx.get("amendsVersionId"))
            ctx["baseSource"] = "DPS_EXTRACTION"
    fields = pack.rules(pages, ctx)
    max_pages = pack.page_selection.get("max_pages", 25)
    batches, page_note = _extraction_batches(pack, pages, fields, max_pages)
    lookup_ctx = {"cpIds": ctx.get("cpIds") or [], "obligorId": ctx.get("obligorId"),
                  "facilityIds": ctx.get("facilityIds") or []}
    notes: list[dict] = []
    events = None
    if pack.name == "covenants" and pack.page_selection.get("parts"):  # focused parts, one taxonomy slice each
        from dps.skills.covenants import parts

        proposals, events = parts.read(pack, gateway, batches or [[]], notes=notes, spring=tools,
                                       correlation_id=job.correlation_id, lookup_context=lookup_ctx, all_pages=pages)
    else:
        proposals = batched_proposals(pack, gateway, batches or [[]], notes=notes, spring=tools,
                                      correlation_id=job.correlation_id, lookup_context=lookup_ctx, all_pages=pages)
    if pack.name in ("covenants", "amendment_diff"):  # library metric keys; agreements also drop loan mechanics
        from dps.skills.covenants import names

        covenants = pack.name == "covenants"
        proposals = names.clean(proposals, drop_mechanics=covenants, collapse=covenants)
    fields = _vote(pack, fields, proposals)
    if pack.name == "amendment_diff":
        _explain_changes(fields, proposals)
    fields += _agent_only(pack, pages, fields, proposals)
    if events:  # events of default: their own fields, not covenants
        from dps.skills.covenants.codes import event_fields

        fields += event_fields(pack.name, pages, events)
    warnings: list[dict] = []
    unreadable = [p["no"] for p in pages
                  if float(p.get("quality", 1.0)) < UNREADABLE_QUALITY and p.get("sourceMethod") != "BLANK"]
    if unreadable:  # a page OCR cannot read: the vision model may read its image, a person always confirms
        from dps.agent import vision

        seen = vision.read_pages(pack, gateway, unreadable, notes)
        fields += _agent_only(pack, pages, fields, seen, method="AI_VISION")
        warnings.append(issue("UNREADABLE_PAGES", "pages a person must read: " + ", ".join(map(str, unreadable[:30]))
                              + ("; read from their images by the vision model" if seen else "")))
    # the calendar clauses of a sanction letter, an agreement or an amendment (an amendment may change only the
    # schedule: a new test date or reporting deadline is a change too; architecture review P1 #8)
    if pack.name in ("sanction", "covenants", "amendment_diff"):
        sched = load("schedule_terms")
        amount = next((f["value"].get("amount") for f in fields if f["name"] == "SANCTIONED_AMOUNT"), None)
        sched_fields = sched.rules(pages, {**ctx, "sanctionedAmount": amount})
        if gateway is not None:  # Miro AI function 3: the agent reads the schedule terms with the schedule skill
            sched_pages = select_pages(pages, sched_fields, KEYWORDS["schedule_terms"],
                                       sched.page_selection.get("max_pages", 20))
            # in batches that each fit one prompt: one prompt left 4 of the 20 schedule pages out (22 Nov 2026)
            sched_proposals = batched_proposals(sched, gateway, page_batches(sched_pages), notes=notes, spring=tools,
                                                correlation_id=job.correlation_id, all_pages=pages)
            sched_fields = _vote_schedule(sched_fields, sched_proposals)
            sched_fields += _agent_only(sched, pages, sched_fields, sched_proposals)
        warnings += sched.checks(sched_fields, {**ctx, "sanctionedAmount": amount})
        fields += sched_fields
    fields = merge_same_sentence(fields)
    if pack.name in ("sanction", "covenants", "amendment_diff"):
        from dps.skills.schedule_terms.rules import link_terms

        link_terms(fields)  # the app's frequency, and each reporting deadline with the covenants it serves
    entries = None
    # every covenant-shaped sentence of the whole document ends with an outcome; a sanction letter's covenants too
    # (CTS review, 23 Nov 2026: a covenant the rules missed in a sanction letter was lost without a trace)
    if pack.name in ("covenants", "sanction"):
        from dps.skills.covenants import ledger, omissions

        with obs.span("dps.ledger") as ledger_obj:
            entries = ledger.settle(ledger.candidates(pages), fields)
            still_open = ledger.unresolved(entries)
            if gateway is not None and still_open:  # the Document Agent looks again at what nothing explains
                residual, decisions = omissions.read(gateway, still_open, pages, notes,
                                                     pack.routes.get("extract", "strong"))
                from dps.skills.covenants import names

                residual = names.clean(residual, drop_mechanics=True, collapse=False) or []
                # what the agent finds is a covenant to confirm, also in a sanction letter (never a loan term)
                fields += _agent_only(pack if pack.name == "covenants" else load("covenants"), pages, fields,
                                      residual, method="AI_RESIDUAL")
                fields = merge_same_sentence(fields)
                from dps.agent.guard import injection_signs

                # A document that addresses an AI model may have talked it into dismissing duties: none is taken
                injected = bool(injection_signs("\n".join(p.get("text") or "" for p in pages)))
                ledger.settle(entries, fields, decisions, trust_dismissals=not injected)
            ledger.attach(entries, fields)
            fields += ledger.review_items(entries, pages, pack.name)
            obs.set_attrs(ledger_obj, **{"dps.unresolved": len(ledger.unresolved(entries))})
    if gateway is None and pack.routes:  # the switch is off or no model is configured (full review F12)
        warnings.append(issue("AI_NOT_USED", "read by the rules only: the Document Agent is not switched on, so no "
                                             "second reading and no residual check ran; review every field"))
    if gateway is not None:
        if proposals is None and not notes:
            notes.append(issue("AI_UNAVAILABLE", "read by the rules only"))
        seen_codes = set()
        for n in notes:  # one warning per reason, so the reviewer sees why the agent did not help
            if n["code"] not in seen_codes:
                seen_codes.add(n["code"])
                warnings.append(n)
    if page_note and page_note["notSent"] and gateway is not None:
        warnings.append(issue("PAGES_NOT_SENT", "duty pages not in the agent's prompt (it could open them with its "
                                                "tools): " + ", ".join(map(str, page_note["notSent"][:30]))))
    if pack.name == "amendment_diff" and len(pages) > max_pages and gateway is not None:
        count = sum(map(len, batches))
        warnings.append(issue("PAGES_NOT_SENT", f"Amendment prompts included {count} of {len(pages)} pages."))
    # the library match and the app formula run in run(), after the taxonomy stamp and the scope enrichment
    warnings += pack.checks(fields, ctx)
    extra = {}
    if base_run_id:
        extra["_baseRunId"] = base_run_id
    if entries is not None:
        from dps.skills.covenants import ledger

        led = ledger.summary(entries, pages)
        still = [e for e in led["entries"] if e["state"] == ledger.UNRESOLVED]
        # the older coverage keys stay, now counted over the whole document
        extra["_coverage"] = {"dutySentences": led["candidates"], "explained": led["candidates"] - led["unresolved"],
                              "byQuote": led["finding"], "byKind": led["covered"], "percent": led["percentExplained"],
                              "unexplained": [{"page": e["page"], "text": e["text"][:200], "kind": e["kind"]}
                                              for e in still[:10]],
                              "unexplainedCount": led["unresolved"], "ledger": led}
        if led["unresolved"]:
            at = sorted({e["page"] for e in still})
            warnings.append(issue("COVERAGE_GAP", f"{led['unresolved']} duty sentence(s) not explained by any "
                                                  f"covenant ({led['percentExplained']}% explained, 100% accounted); "
                                                  "each is a review item on pages " + ", ".join(map(str, at[:30]))))
    elif page_note:  # every duty sentence on the covenant pages is explained by a field, or listed for the reviewer
        from dps.skills.covenants import coverage

        duty_pages = {s["page"] for s in page_note["sent"] if s["why"] in ("RULES", "SECTION", "DUTY")}
        cov = coverage.check([p for p in pages if p["no"] in duty_pages], fields)
        extra["_coverage"] = cov
        if cov["unexplainedCount"]:
            at = sorted({u["page"] for u in cov["unexplained"]})
            warnings.append(issue("COVERAGE_GAP", f"{cov['unexplainedCount']} duty sentence(s) not explained by any "
                                                  f"field ({cov['percent']}% covered); check pages "
                                                  + ", ".join(map(str, at))))
    if pack.name == "amendment_diff":
        from dps.skills.amendment_diff.rules import amended_set

        extra["amendedSet"] = amended_set(ctx.get("base") or [], fields)
        extra["effectiveDate"] = next((f["value"]["change"].get("effectiveDate") for f in fields
                                       if f["value"].get("change")), None) or next(
            (f["value"].get("date") for f in fields if f["value"].get("key") == "AMENDMENT_PROVISION"), None)
    return fields, warnings, extra


def _statement(job: Job, db: Session | None, deps, *, flagged: bool = False):
    """EXTRACT_STATEMENT (full review WP7: one pipeline): the statement reader of the statement_lines skill, then the
    accounting identities and the restatement diff. Every figure is a proposal; the values table is written only
    when the review completes (statement_lines.reader.publish)."""
    from dps import results
    from dps.models import Document
    from dps.skills.statement_lines.reader import ContentFailed, NeedsHeavyPool, StatementReader, as_result

    if deps is None:
        raise RuntimeError("EXTRACT_STATEMENT needs the statement reader's dependencies")
    if flagged and deps.skip_confident_ai:
        from dataclasses import replace

        deps = replace(deps, skip_confident_ai=False)
    state = StatementReader(deps).read(job.id, job.document_id)
    if state.outcome == "HEAVY":
        raise NeedsHeavyPool("too many pages the standard reader could not read")
    if state.outcome == "FAILED":
        raise ContentFailed(state.error)
    doc = db.get(Document, job.document_id) if db is not None else None
    as_of = doc.as_of_date if doc else None
    if doc is not None and as_of is None and state.period_end:
        # no period date sent: the one the statement's heading names, kept for publishing (CTS integration)
        as_of = doc.as_of_date = date.fromisoformat(state.period_end)
    fields, warnings, extra = as_result(state, as_of, deps.report_derived_values)
    if state.period_source == "DOCUMENT":
        warnings.append(issue("PERIOD_FROM_DOCUMENT", f"period end {as_of.isoformat()} read from the statement's "
                                                      "heading (no period date was sent); check it"))
    elif state.period_source == "MISSING":
        warnings.append(issue("PERIOD_DATE_MISSING", "no period date was sent and the statement's heading names "
                                                     "none: send it with reclassify (asOfDate) before approving, "
                                                     "or the figures cannot be stored"))
    warnings += statement_warnings(fields, results.approved_figures(db, doc) if db is not None else {})
    return fields, warnings, extra


def _reconcile(job: Job, raw: bytes, db: Session | None, pipeline_version: str, ctx: dict):
    """RECONCILE_SET (WP9): the covenants in force at the manifest's date, from the latest run of each document in
    the set (no document is read again)."""
    from dps.skills.reconcile_set.rules import JOB_TYPES, parse_manifest, reconcile

    manifest, problem = parse_manifest(raw)
    if manifest is None:
        return [], [issue("SET_MANIFEST_INVALID", problem)], {"set": None}
    runs: dict[str, dict | None] = {}
    if db is not None:
        from dps import results

        for d in manifest["documents"]:
            query = select(Job).where(Job.tenant_id == job.tenant_id, Job.document_version_id == d["versionId"],
                                      Job.job_type.in_(JOB_TYPES), Job.status.in_((jobs.DONE, jobs.NEEDS_REVIEW)))
            if d.get("jobId"):
                query = query.where(Job.id == str(d["jobId"]))
            found = db.scalars(query.order_by(Job.finished_at.desc())).first()
            runs[d["versionId"]] = results.build(db, found, pipeline_version) if found is not None else None
    base = ctx.get("baseCovenants") or manifest.get("baseCovenants") or []
    if not all(isinstance(c, dict) and c.get("appliedCovenantId") and c.get("name") for c in base):
        return [], [issue("SET_MANIFEST_INVALID", "each baseCovenants item needs appliedCovenantId and name")], \
            {"set": None}
    fields, warnings, report = reconcile(manifest, runs, approved_base(base))
    return fields, warnings, {"set": report}


def _commentary(pack: Pack, raw: bytes, gateway):
    from dps.skills.commentary.rules import check_text, draft

    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], [issue("CORRUPT", "the outcome manifest is not valid JSON")], {"commentary": None}
    paragraphs = None
    method = "RULES"
    if gateway is not None:
        from dps.agent.gateway import GatewayError
        from dps.agent.guard import DATA_NOT_INSTRUCTIONS

        messages = [{"role": "user", "content": f"{DATA_NOT_INSTRUCTIONS}\n{pack.prompt}\n\nOutcome manifest:\n"
                                                f"{json.dumps(manifest, default=str)[:20000]}"}]

        def check(answer) -> str | None:
            text = "\n".join(i["value"]["text"] for i in answer.get("items", []))
            problems = check_text(text, manifest)
            return problems[0]["message"] if problems else None

        try:
            answer = gateway.call_json(pack.routes.get("extract", "strong"), messages, pack.schema, check)
            paragraphs = [i["value"]["text"] for i in answer["items"]]
            method = "AI"
        except GatewayError as ex:
            log.info("commentary model unavailable: %s", ex.code)
    if paragraphs is None:
        paragraphs = draft(manifest)
    problems = check_text("\n".join(paragraphs), manifest)
    commentary = {"paragraphs": paragraphs, "method": method, "draft": True, "issues": problems}
    return [], problems, {"commentary": commentary}


def finish(db: Session, job: Job, result: dict) -> None:
    from dps.analyze.analysis import save_run

    trace_ai_fields(db, job, result)
    save_run(db, job, result)
    status = jobs.NEEDS_REVIEW if result["status"] == "NEEDS_REVIEW" else jobs.DONE
    mapping = (result.get("document") or {}).get("route") == "MAPPING_REVIEW"
    # fields: every field; toReview: the ones waiting for a person; route: why (MAPPING_REVIEW, NEEDS_REVIEW ...)
    jobs.finish(db, job, status, None, "MAPPING_REVIEW" if mapping and status == jobs.NEEDS_REVIEW else None,
                payload={**feed.result_metadata(result),
                         "skillPack": result["pipeline"]["skillPack"], "fields": len(result["fields"]),
                         "toReview": sum(1 for f in result["fields"] if f.get("status") == "NEEDS_REVIEW"),
                         "route": (result.get("document") or {}).get("route"),
                         "documentType": (result.get("classification") or {}).get("type")})


def trace_ai_fields(db: Session | None, job: Job, result: dict) -> None:
    """Moves each AI field's agent-read span id out of the result (Spring's contract has no place for it) into the
    document's history: one FOUND / SHOWN row per AI finding with its trace and span ids, so a finding opens the read
    and the model calls that produced it in Langfuse (observability, 23 Nov 2026)."""
    from dps import history

    trace_id = obs.current_ids()[0]
    for f in result.get("fields") or []:
        span_id = f.pop("_aiSpan", None)
        if not span_id or db is None or not job.document_id:
            continue
        step = "FOUND" if f.get("status") == "AUTO_ACCEPTED" else "SHOWN"
        source = f.get("source") or {}
        history.add(db, job.document_id, step, f"{f.get('name')} (page {source.get('page')}) [{f.get('method')}]",
                    job_id=job.id, trace_id=trace_id, field_id=f.get("id"), method=f.get("method"),
                    ai_span_id=span_id)


def statement_warnings(fields: list[dict], approved: dict | None = None) -> list[dict]:
    """Accounting identities, and (G4) every line that differs from the figure approved earlier for the same
    borrower and period: RESTATED on the line, and one warning listing the affected lines."""
    warnings = load("statement_lines").checks(fields, {"approved": approved or {}})
    restated = [f"{f['name']} {f['value'].get('asOfDate')}" for f in fields
                if any(i.get("code") == "RESTATED" for i in f.get("issues", []))]
    if restated:
        warnings.append(issue("RESTATED", f"{len(restated)} line(s) differ from the approved figures: "
                                          + ", ".join(restated[:20])))
    return warnings


def pack_for(job_type: str) -> Pack | None:
    return for_job_type(job_type)


# G1 (Miro onboarding flow): one job classifies the document, then runs the right pack. Covenants and schedule
# terms come back in one result, with the classification and the identifiers.
ONBOARD_PACKS = {"LOAN_AGREEMENT": "covenants", "SANCTION_LETTER": "sanction", "AMENDMENT": "amendment_diff",
                 # WP9: a guarantee's covenants bind the guarantor (scope says so); an amended and restated
                 # agreement is a full agreement that replaces an earlier one; a waiver is read against the base
                 "GUARANTEE": "covenants", "AMENDED_AND_RESTATED": "covenants", "WAIVER_CONSENT": "waiver",
                 # a debenture trust deed holds the issuer's covenants to the trustee (bulk run, 22 Nov 2026)
                 "TRUST_DEED": "covenants"}
# CTS integration, 23 Nov 2026: one upload for every document type. AUTO classifies like ONBOARD and also reads
# statements and compliance certificates with their own packs; the classification in the result says what was read.
AUTO_PACKS = {**ONBOARD_PACKS, "FINANCIAL_STATEMENT": "statement_lines", "COMPLIANCE_CERTIFICATE": "certificate"}
# packs that check the cycle's expectations (period, basis, entity, currency) themselves
_CHECK_OWN_EXPECTATIONS = ("certificate", "statement_lines")


def run_onboard(job: Job, model: dict, gateway=None, tools=None, db: Session | None = None,
                pipeline_version: str = "1", scorer_dir: str | None = None, raw: bytes | None = None,
                statement_deps=None) -> dict:
    from dps.analyze import obligations
    from dps.analyze.classify import classify
    from dps.analyze.identify import find_identifiers, legibility, match, missing_required
    from dps.analyze.keypages import key_pages

    ctx = json.loads(job.request_json or "{}").get("context") or {}
    expected = ctx.get("expectedType")
    with obs.span("dps.classify", **{"dps.expected_type": expected}) as class_span:
        c = classify(model, expected, gateway)
        obs.set_attrs(class_span, **{"dps.type": c.type, "dps.confidence": round(float(c.confidence or 0), 3),
                                     "dps.mismatch": bool(c.mismatch), "dps.uncertain": bool(c.uncertain)})
    auto = job.job_type == "AUTO"
    pack_name = (AUTO_PACKS if auto else ONBOARD_PACKS).get(c.type)
    if auto and db is not None:  # the document's type, as read, for everything that follows (publish, reuse)
        from dps.models import Document

        doc = db.get(Document, job.document_id)
        if doc is not None and doc.type in ("UNKNOWN", "", None):
            doc.type = c.type[:32]
    if pack_name is None:  # not a type this job reads (a security document, other evidence, an unreadable scan)
        code = "TYPE_NOT_READ" if auto else "TYPE_NOT_ONBOARDING"
        message = (f"the document reads as {c.type}; DPS reads agreements, sanction letters, amendments, waivers, "
                   "statements and compliance certificates: reclassify it if the type is wrong" if auto else
                   f"the document reads as {c.type}; onboarding reads agreements, sanction letters and amendments")
        result = run(load("covenants"), job, {"pages": []}, None, None, db, pipeline_version)
        result["fields"], result["status"] = [], "NEEDS_REVIEW"
        result["warnings"] = [issue(code, message)]
        result["document"] = {"confidence": c.confidence, "route": "NEEDS_REVIEW",
                              "reasons": [code], "pages": page_accounting(model.get("pages", []))}
    else:
        result = run(load(pack_name), job, model, gateway, tools, db, pipeline_version, raw=raw,
                     scorer_dir=scorer_dir, statement_deps=statement_deps)
    with obs.span("dps.identify") as id_span:
        found = find_identifiers(model)
        identifiers, id_warnings = match(found, tools, {"cpIds": ctx.get("cpIds") or [],
                                                         "obligorId": ctx.get("obligorId"),
                                                         "facilityIds": ctx.get("facilityIds") or []},
                                         job.correlation_id)
        obs.set_attrs(id_span, **{"dps.identifiers": len(identifiers), "dps.warnings": len(id_warnings)})
    result["classification"] = c.as_result()
    result["identifiers"] = identifiers
    restates = None
    if c.type == "AMENDED_AND_RESTATED":  # WP9: Spring supersedes the replaced version's covenants on approval
        restates = ctx.get("restatesVersionId") or ctx.get("amendsVersionId")
        result["restates"] = {"versionId": restates, "evidence": c.evidence[:1]}
    result["legibility"] = legibility(model)
    result["keyPages"] = key_pages(model, c.type)
    extra = list(id_warnings)
    if c.type == "AMENDED_AND_RESTATED":
        extra.append(issue("RESTATES", "an amended and restated agreement: its covenants replace those of "
                                       + (f"version {restates}" if restates else "the earlier agreement (version not "
                                                                                  "sent: context.restatesVersionId)")
                                       + " once approved"))
    if c.mismatch:
        extra.append(issue("TYPE_MISMATCH", f"chosen {expected}, the document reads as {c.type}"))
    if c.disputed:
        extra.append(issue("TYPE_DISPUTED", f"the rules read {c.type}; the Document Agent read {c.disputed}"))
    elif c.uncertain:
        extra.append(issue("UNCERTAIN_TYPE", f"best guess {c.type} at {c.confidence:.0%}"))
    missing = missing_required(c.type, found)
    if missing:
        extra.append(issue("REQUIRED_MISSING", f"{c.type} usually shows: " + ", ".join(missing) + " (not found)"))
    conflicts = [i for i in identifiers if i["match"]["status"] == "CONFLICT"]
    if conflicts:
        extra.append(issue("MAPPING_CONFLICT", f"{len(conflicts)} identifier(s) point to another borrower"))
    mismatches = ([] if pack_name in _CHECK_OWN_EXPECTATIONS  # already checked by the pack: not twice
                  else obligations.check(ctx, found, c.basis, model))
    extra += mismatches
    result["warnings"] = extra + result["warnings"]
    if c.mismatch or c.uncertain or conflicts or mismatches:
        result["status"] = "NEEDS_REVIEW"
        result["document"]["route"] = obligations.route_for(extra)
        codes = [w["code"] for w in extra if w["code"] != "REQUIRED_MISSING"]
        result["document"]["reasons"] = codes + result["document"].get("reasons", [])
    return result


def run_job(job: Job, model: dict | None, gateway=None, tools=None, db: Session | None = None,
            pipeline_version: str = "1", raw: bytes | None = None, scorer_dir: str | None = None,
            statement_deps=None) -> dict:
    """The worker's entry point for every job type that reads a document (one pipeline, full review WP7)."""
    if job.job_type in ("ONBOARD", "AUTO"):
        return run_onboard(job, model or {"pages": []}, gateway, tools, db, pipeline_version, scorer_dir, raw,
                           statement_deps)
    return run(pack_for(job.job_type), job, model, gateway, tools, db, pipeline_version, raw, scorer_dir,
               statement_deps)

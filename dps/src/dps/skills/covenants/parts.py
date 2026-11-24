"""The covenants skill in four focused parts (18 Nov 2026): financial, undertakings (affirmative and negative),
reporting, events of default.

One agent, several skills: each part is the covenants skill with its own scope and only its own part of the covenant
taxonomy in the prompt, read over the pages that hold that part's words. Focused reads are more accurate than one
read for every covenant kind, and events of default come back on their own (they are not covenants: they say what
happens on a breach). When no page holds any part's words, one read covers every covenant category, as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from dps.skills.covenants.taxonomy import COVENANT_CATEGORIES
from dps.skills.covenants.taxonomy import get as taxonomy

NOT_COVENANTS = ("Not covenants (another skill reads them; leave them out): drawdown and utilisation, prepayment, "
                 "repayment and instalments, interest payment and rates, fees, tenor, payment default and grace "
                 "periods, cure periods, acceleration, definitions, events of default.")
NOT_EVENTS = ("Only events of default: what lets the lender call the loan or enforce. Leave out the covenants "
              "themselves, loan mechanics and definitions.")


@dataclass(frozen=True)
class Part:
    name: str
    categories: tuple[str, ...]
    scope: str
    words: str  # extra page words beyond the taxonomy's aliases and keywords
    group: str | None = None  # result field group when not a covenant group
    exclusions: str = NOT_COVENANTS


PARTS: dict[str, Part] = {p.name: p for p in (
    Part("financial", ("financial",),
         "the financial covenants: ratios or amounts the borrower must keep above or below a level, tested on a "
         "schedule",
         r"not\s+less\s+than|not\s+(?:more|greater)\s+than|not\s+exceed|at\s+least|ratio|net\s+worth|times|cover"),
    Part("undertakings", ("affirmative", "negative"),
         "the undertakings: what the borrower must do (insurance, taxes, books, use of proceeds, security, accounts) "
         "and must not do without consent (liens, more debt, disposals, mergers, dividends, change of business or "
         "control)",
         r"shall\s+not|shall\s+(?:at\s+all\s+times\s+)?(?:maintain|ensure|procure|keep|comply|obtain|create)|"
         r"undertak|without\s+(?:the\s+)?prior|consent"),
    Part("reporting", ("reporting",),
         "the reporting covenants: what the borrower must deliver to the lender and by when (financial statements, "
         "certificates, stock statements, budgets, notices)",
         r"deliver|furnish|submit|within\s+\d+|statements?|certificate|notify|notice"),
    Part("events_of_default", ("event_of_default",), "the events of default",
         r"events?\s+of\s+default|default", group="EVENT_OF_DEFAULT", exclusions=NOT_EVENTS),
)}
ALL = Part("all", COVENANT_CATEGORIES, "the covenants: duties the borrower must keep for the life of the loan", r".")


def parts_of(pack) -> list[Part]:
    return [PARTS[n] for n in pack.page_selection.get("parts") or [] if n in PARTS]


def pattern(part: Part) -> re.Pattern[str]:
    words = taxonomy().words_for(part.categories)
    extra = re.compile(part.words, re.I)
    if words is None:
        return extra
    return re.compile(f"(?:{words.pattern})|(?:{part.words})", re.I)


def part_pack(pack, part: Part):
    """The covenants pack with this part's scope and taxonomy codes in its prompt."""
    prompt = (pack.prompt.replace("{scope}", part.scope).replace("{exclusions}", part.exclusions)
              .replace("{types}", taxonomy().prompt_lines(part.categories)))
    return replace(pack, prompt=prompt)


def read(pack, gateway, batches: list[list[dict]], notes: list | None = None, **kw) -> tuple[list | None, list | None]:
    """(covenant proposals, event-of-default proposals), each None when no read gave an answer."""
    from dps.agent.parallel import run_all
    from dps.skills.common import batched_proposals

    covenants: list | None = None
    events: list | None = None
    todo = []
    for part in parts_of(pack) or [ALL]:
        pat = pattern(part)
        if pack.page_selection.get("same_batch_for_parts"):
            # Same pages for every part, pages first in the prompt: parts 2-4 reuse part 1's cached prefix (G32).
            # A part with no word on a batch still skips it.
            part_batches = [b for b in batches if any(pat.search(p.get("text") or "") for p in b)]
        else:
            part_batches = [kept for kept in ([p for p in b if pat.search(p.get("text") or "")] for b in batches)
                            if kept]
        if part_batches:
            todo.append((part, part_batches))
    ran = bool(todo)

    def read_part(part, part_batches):
        part_notes: list = []
        got = batched_proposals(part_pack(pack, part), gateway, part_batches, notes=part_notes, **kw)
        return got, part_notes

    # the parts are read in parallel too (S1); results and notes are merged in part order, as before
    results = run_all(gateway, [lambda p=p, b=b: read_part(p, b) for p, b in todo])
    for (part, _), (got, part_notes) in zip(todo, results, strict=True):
        if notes is not None:
            notes.extend(part_notes)
        if got is None:
            continue
        for p in got:
            p["_part"] = part.name
        if part.group:
            events = [*(events or []), *got]
        else:
            covenants = [*(covenants or []), *got]
    if not ran:  # no page holds any part's words: one read for every covenant category, as before the parts
        covenants = batched_proposals(part_pack(pack, ALL), gateway, batches or [[]], notes=notes, **kw)
    return covenants, events

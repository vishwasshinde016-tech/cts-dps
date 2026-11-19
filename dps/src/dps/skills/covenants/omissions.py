"""Residual read (19 Nov 2026): the Document Agent looks again at every duty sentence nothing explains yet.

The four focused parts read the covenant pages once. On the long live agreements about a fifth of the duty sentences
were still unexplained after that read. Here each unexplained candidate from the ledger (``ledger.py``) goes back to
the model with the sentence before and after it and, when the sentence points to a schedule or clause, the start of
that part. The model answers once per candidate:

- ``COVENANT``: a covenant, with name, value, page and verbatim quote (grounded and voted like any other answer);
- ``NOT_A_COVENANT``: why not (a definition, a representation, loan mechanics, a lender right ...), kept in the
  ledger so the reviewer can see the reason;
- ``UNSURE``: left for a person.

Bounded: at most ``MAX_CALLS`` calls of ``MAX_ITEMS`` candidates, within the job's token budget. A candidate the
model did not answer stays unresolved, so a model problem never hides a duty.
"""

from __future__ import annotations

import json
import logging

from dps.skills.common import HEADER, grounded, issue

log = logging.getLogger(__name__)

MAX_ITEMS = 25
MAX_CALLS = 4
MAX_CONTEXT = 360
DECISIONS = ("COVENANT", "NOT_A_COVENANT", "UNSURE")

INSTRUCTIONS = """Task: a first reading of this agreement extracted its covenants, but the sentences below were not \
explained by any extracted covenant. Look at each one again, using the text around it.

For every candidate id answer exactly once:
- "COVENANT" when the sentence (with its context) sets an ongoing duty of the borrower or an obligor that a lender \
would monitor: a financial ratio or amount to keep, an undertaking to do or not to do something, information to \
deliver. Give name, value, page and quote as in the schema; the quote must be copied verbatim from that page.
- "NOT_A_COVENANT" when it is not one, and say why in a few words: a definition, a representation or warranty, a \
condition precedent, loan mechanics (drawdown, interest, fees, repayment, prepayment), a lender right or remedy, an \
event of default, boilerplate.
- "UNSURE" when you cannot tell; a person will decide.

Never invent a number: use null when the text does not state it. Keep conditions and exceptions in the quote."""

SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["candidate", "decision"],
    "properties": {
        "candidate": {"type": "string"}, "decision": {"type": "string", "enum": list(DECISIONS)},
        "reason": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]},
        "page": {"type": ["integer", "null"]}, "quote": {"type": ["string", "null"]},
        "value": {"type": ["object", "null"], "properties": {
            "metric": {"type": ["string", "null"]}, "basis": {"type": ["string", "null"]},
            "operator": {"type": ["string", "null"]}, "threshold": {"type": ["number", "null"]},
            "unit": {"type": ["string", "null"]}, "frequency": {"type": ["string", "null"]},
            "cureDays": {"type": ["integer", "null"]}}}}}}}}


def _cut(text: str, limit: int = MAX_CONTEXT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _block(entry: dict, pages_by_no: dict[int, dict]) -> str:
    lines = [f'[{entry["id"]}] page {entry["page"]}',
             f"before: {_cut(entry.get('_before', ''))}",
             f"SENTENCE: {_cut(entry['_full'], 900)}",
             f"after: {_cut(entry.get('_after', ''))}"]
    for ref in entry.get("references") or []:
        page = pages_by_no.get(ref["page"]) if ref.get("page") else None
        if page is not None and ref["page"] != entry["page"]:
            lines.append(f"{ref['ref']} (page {ref['page']}): {_cut(page.get('text') or '', 600)}")
    return "\n".join(lines)


def _types_text() -> str:
    from dps.skills.covenants.taxonomy import COVENANT_CATEGORIES
    from dps.skills.covenants.taxonomy import get as taxonomy

    return taxonomy().prompt_lines(COVENANT_CATEGORIES)


def read(gateway, entries: list[dict], pages: list[dict], notes: list | None = None,
         route: str = "strong") -> tuple[list[dict], dict[str, dict]]:
    """(covenant proposals in the covenants pack's shape, {candidate id: decision}) for the unresolved entries."""
    if gateway is None or not entries:
        return [], {}
    from dps.agent.gateway import AiBudgetExceeded, GatewayError, ModelOutputInvalid
    from dps.ai_guard import DATA_NOT_INSTRUCTIONS

    pages_by_no = {int(p["no"]): p for p in pages}
    proposals: list[dict] = []
    decisions: dict[str, dict] = {}
    types = _types_text()
    batches = [entries[i:i + MAX_ITEMS] for i in range(0, len(entries), MAX_ITEMS)]
    for n, batch in enumerate(batches):
        if n >= MAX_CALLS:
            if notes is not None:
                notes.append(issue("RESIDUAL_BUDGET", f"{sum(len(b) for b in batches[n:])} unexplained duty "
                                                      "sentence(s) were not read again; they are listed for review"))
            break
        ids = {e["id"] for e in batch}

        def check(answer, ids=ids) -> str | None:
            for item in (answer or {}).get("items", []):
                if item.get("candidate") not in ids:
                    return f"unknown candidate id {item.get('candidate')!r}"
                if item.get("decision") == "COVENANT" and not grounded(pages, item.get("page"), item.get("quote")):
                    return f"quote not on page {item.get('page')}: {str(item.get('quote'))[:60]!r}"
            return None

        content = "\n\n".join([HEADER.format(guard=DATA_NOT_INSTRUCTIONS), INSTRUCTIONS,
                               "Covenant types (use the code as value.metric when one fits, else OTHER):\n" + types,
                               "Schema:\n" + json.dumps(SCHEMA),
                               "Candidates:\n\n" + "\n\n".join(_block(e, pages_by_no) for e in batch)])
        try:
            answer = gateway.call_json(route, [{"role": "user", "content": content}], SCHEMA, check)
        except AiBudgetExceeded:
            if notes is not None:
                notes.append(issue("RESIDUAL_BUDGET", "the token budget ran out before the residual read finished; "
                                                      "the unexplained sentences are listed for review"))
            break
        except (ModelOutputInvalid, GatewayError) as ex:
            log.info("residual read batch %d failed: %s", n + 1, type(ex).__name__)
            if notes is not None:
                notes.append(issue("RESIDUAL_INCOMPLETE", f"one residual read failed ({type(ex).__name__}); its "
                                                          "sentences are listed for review"))
            continue
        for item in (answer or {}).get("items", []):
            cid = item.get("candidate")
            if cid not in ids or cid in decisions:
                continue
            decisions[cid] = item
            if item.get("decision") == "COVENANT" and item.get("quote") and item.get("page"):
                value = {k: v for k, v in (item.get("value") or {}).items() if v is not None}
                for key in ("metric", "operator", "threshold", "unit", "expression"):
                    value.setdefault(key, "OTHER" if key == "metric" else None)
                proposals.append({"name": item.get("name") or value.get("metric") or "OTHER", "value": value,
                                  "page": item["page"], "quote": item["quote"], "_residual": cid})
    return proposals, decisions

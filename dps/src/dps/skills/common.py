"""Shared parts of every pack: fields, grounding, page selection, the agent step and voting."""

from __future__ import annotations

import hashlib
import json
import logging
import re

log = logging.getLogger(__name__)

HEADER = ("You read one bank lending document for a covenant tracking system. {guard} Answer only with JSON that "
          "matches the schema below. Copy every quote verbatim from the page you name. Use null when the document "
          "does not say. Never guess a number.")
MAX_PROMPT_CHARS = 60_000


def squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def line_at(text: str, start: int, end: int | None = None) -> str:
    end = start if end is None else end
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a:b if b != -1 else len(text)].strip()


def page_of(pages: list[dict], no: int | None) -> dict | None:
    return next((p for p in pages if p["no"] == no), None)


def locate(pages: list[dict], page_no: int | None, quote: str | None) -> tuple[str | None, list | None]:
    """The block id and box of the line that holds ``quote`` on ``page_no``."""
    page = page_of(pages, page_no)
    if page is None or not quote:
        return None, None
    q = squash(quote)
    for b in page.get("blocks", []):
        t = squash(b.get("text"))
        if t and (t in q or q[:60] in t):
            return b.get("id"), b.get("bbox")
    return None, None


_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")


def _flat(text: str | None) -> str:
    return squash(_HYPHEN_BREAK.sub(r"\1\2", text or ""))


def grounded(pages: list[dict], page_no: int | None, quote: str | None) -> bool:
    """The quote is on the cited page, on the page just before or after it (a page number one off), or runs across
    the break between the cited page and a neighbour. Line breaks and hyphenated line ends are ignored."""
    q = _flat(quote)
    page = page_of(pages, page_no)
    if page is None or len(q) < 4:
        return False
    here = _flat(page.get("text"))
    if q in here:
        return True
    before, after = page_of(pages, (page_no or 0) - 1), page_of(pages, (page_no or 0) + 1)
    for other in (before, after):
        if other is not None and q in _flat(other.get("text")):
            return True
    if after is not None and q in f"{here} {_flat(after.get('text'))}":
        return True
    return before is not None and q in f"{_flat(before.get('text'))} {here}"


def field_id(pack: str, group: str, name: str, page: int | None, key: str = "") -> str:
    digest = hashlib.sha256(f"{pack}|{group}|{name}|{page}|{key}".encode()).hexdigest()[:16]
    return f"{pack[:3]}-{digest}"


def make_field(pack: str, pages: list[dict], group: str, name: str, value: dict, page: int | None, quote: str | None,
               method: str = "RULES", confidence: float | None = 0.85, issues: list | None = None,
               cell_ref: str | None = None, test: dict | None = None, key: str = "",
               library: dict | None = None) -> dict:
    block, bbox = locate(pages, page, quote)
    return {"id": field_id(pack, group, name, page, key), "group": group, "name": name, "value": value,
            "source": {"page": page, "quote": (quote or "")[:500] or None, "bbox": bbox, "blockId": block,
                       "cellRef": cell_ref},
            "test": test, "libraryMatch": library, "method": method,
            "confidence": None if confidence is None else round(float(confidence), 3), "signals": None,
            "issues": list(issues or []), "status": "AUTO_ACCEPTED"}


def issue(code: str, message: str | None = None) -> dict:
    return {"code": code, "message": message}


def select_pages(pages: list[dict], candidates: list[dict], keywords: re.Pattern | None,
                 max_pages: int = 25) -> list[dict]:
    """Pages the model needs: those holding rule candidates, then keyword pages, in page order."""
    wanted: list[int] = []
    for f in candidates:
        no = f["source"]["page"]
        if no and no not in wanted:
            wanted.append(no)
    if keywords is not None:
        for p in pages:
            if p["no"] not in wanted and keywords.search(p.get("text") or ""):
                wanted.append(p["no"])
    if not wanted:
        wanted = [p["no"] for p in pages[:max_pages]]
    keep = set(wanted[:max_pages])
    return [p for p in pages if p["no"] in keep]


def fit_pages(pages: list[dict], limit: int = MAX_PROMPT_CHARS) -> tuple[str, list[int]]:
    """The page blocks that fit in ``limit`` characters, whole pages only, and the pages left out. A page is never
    cut in the middle: the agent would read half a clause and nobody would know (19 Nov 2026 review)."""
    blocks, left_out, used = [], [], 0
    for p in pages:
        block = f"<<<page {p['no']}>>>\n{p.get('text') or ''}\n<<<end page {p['no']}>>>"
        if used + len(block) + 1 > limit:
            left_out.append(p["no"])
            continue
        blocks.append(block)
        used += len(block) + 1
    return "\n".join(blocks), left_out


def prompt_for(pack, pages: list[dict], extra: str = "", notes: list | None = None) -> list[dict]:
    from dps.agent.guard import DATA_NOT_INSTRUCTIONS

    blocks, left_out = fit_pages(pages)
    if left_out and notes is not None:
        listed = ", ".join(map(str, left_out[:30]))
        notes.append(issue("PROMPT_LIMIT", f"{pack.name}: pages {listed} did not fit in the agent's prompt (its tools "
                                           "can still open them)"))
    # The pages come right after the shared header and before the task: reads of the same pages with different
    # tasks (the four covenant parts) share a long identical prefix, which Azure's prompt cache serves at a discount
    # (full review G32). The task, schema and batch note follow the pages.
    content = "\n\n".join([HEADER.format(guard=DATA_NOT_INSTRUCTIONS), blocks, "Task:\n" + pack.prompt.strip(),
                           "Schema:\n" + json.dumps(pack.schema), extra])
    return [{"role": "user", "content": content}]


UNGROUNDED_PREFIX = "quote not on page"


def agent_proposals(pack, gateway, pages: list[dict], extra: str = "", route: str | None = None, spring=None,
                    correlation_id: str | None = None, lookup_context: dict | None = None,
                    notes: list | None = None, all_pages: list[dict] | None = None) -> list[dict] | None:
    """The Document Agent's grounded proposals ({name, value, page, quote}); None when no model is available.

    With a live gateway and a pack that lists tools, a CrewAI Document Agent reads with the pack's prompt, schema and
    tools (agent/document_agent.py). Otherwise one schema-checked model call reads the same pages. Either way every
    quote must be on the page it names. Why a read failed goes into ``notes`` (AI_UNAVAILABLE, AI_BUDGET_EXCEEDED,
    AI_UNGROUNDED, MODEL_OUTPUT_INVALID) for the result's warnings.

    ``pages`` go into the prompt; ``all_pages`` (the whole document, default ``pages``) are what the agent's tools
    search and what quotes are checked against, so the agent can cite a page it found with a tool. When a repaired
    answer still has quotes that are not on their pages, the grounded items are kept and the others dropped."""
    if gateway is None:
        return None
    from dps.agent.gateway import GatewayError, ModelOutputInvalid

    every = all_pages or pages

    def bad(item) -> bool:
        return bool(item.get("page") is not None and item.get("quote") and not grounded(every, item["page"],
                                                                                         item["quote"]))

    def check(answer) -> str | None:
        for item in (answer or {}).get("items", []):
            if bad(item):
                return f"{UNGROUNDED_PREFIX} {item['page']}: {str(item['quote'])[:60]!r}"
        return None

    route = route or pack.routes.get("extract", "strong")
    messages = prompt_for(pack, pages, extra, notes)
    try:
        if pack.tools and callable(getattr(gateway, "call", None)):
            from dps.agent import document_agent

            try:
                answer, ctx = document_agent.read(pack, gateway, pages, messages[0]["content"], route, spring,
                                                  correlation_id, lookup_context, check, all_pages=every)
                log.info("%s agent read with %d tool call(s): %s", pack.name, ctx.used, ",".join(ctx.calls))
            except GatewayError:
                raise
            except Exception as ex:  # noqa: BLE001 - an agent runtime problem falls back to one plain model call
                log.warning("%s agent run failed (%s); one plain model call instead", pack.name, type(ex).__name__)
                answer = gateway.call_json(route, messages, pack.schema, check)
        else:
            answer = gateway.call_json(route, messages, pack.schema, check)
    except ModelOutputInvalid as ex:
        kept = [i for i in ((ex.data or {}).get("items") or []) if isinstance(i, dict) and not bad(i)]
        if not str(ex).startswith(UNGROUNDED_PREFIX) or not kept:
            return _failed(pack, ex, notes)
        dropped = len((ex.data or {}).get("items") or []) - len(kept)
        log.info("%s agent: kept %d grounded item(s), dropped %d", pack.name, len(kept), dropped)
        if notes is not None:
            notes.append(issue("AI_UNGROUNDED", f"{pack.name}: {dropped} answer(s) quoted text not on their page "
                                                 "and were dropped"))
        answer = {"items": kept}
    except GatewayError as ex:
        return _failed(pack, ex, notes)
    return list(answer.get("items", []))


def page_batches(pages: list[dict], limit: int = MAX_PROMPT_CHARS) -> list[list[dict]]:
    """Whole pages, in order, packed into batches that each fit one prompt (``fit_pages``' measure). A page larger
    than the limit is a batch of its own, and fit_pages says it did not fit. The schedule read cut 4 of its 20 pages
    before (review of 22 Nov 2026)."""
    batches: list[list[dict]] = []
    used = 0
    for p in pages:
        size = len(f"<<<page {p['no']}>>>\n{p.get('text') or ''}\n<<<end page {p['no']}>>>") + 1
        if not batches or used + size > limit:
            batches.append([])
            used = 0
        batches[-1].append(p)
        used += size
    return batches


def batched_proposals(pack, gateway, batches: list[list[dict]], notes: list | None = None, **kw) -> list[dict] | None:
    """One agent read per batch of pages (long agreements), merged; None only when no batch gave an answer. A spent
    token budget stops the remaining batches, and says which were not read."""
    out: list[dict] | None = None
    seen: set[tuple] = set()
    from dps.jobs import stop_if_cancelled

    for i, batch in enumerate(batches):
        stop_if_cancelled()  # a cancelled or replaced job stops before its next AI call (S10)
        what = "the agreement's covenant pages: list the covenants" if pack.name == "covenants" else \
            "the pages for this task: list what is"
        extra = f"These are part {i + 1} of {len(batches)} of {what} on these pages." if len(batches) > 1 else ""
        before = len(notes) if notes is not None else 0
        from dps.ops import observability as obs

        numbers = [p.get("no") for p in batch if p.get("no") is not None]
        # One trace span per agent read (observability, 23 Nov 2026): its model calls nest under it, and each
        # proposal keeps the span id, so a finding opens the read (and the calls) that produced it.
        with obs.span(f"dps.ai.read {pack.name}", **{
                "dps.pack": pack.name, "dps.part": i + 1, "dps.parts": len(batches), "dps.pages": len(batch),
                "dps.first_page": min(numbers) if numbers else None,
                "dps.last_page": max(numbers) if numbers else None}) as read_span:
            got = agent_proposals(pack, gateway, batch, extra=extra, notes=notes, **kw)
            new_notes = [n["code"] for n in (notes or [])[before:]]
            obs.set_attrs(read_span, **{"dps.proposals": len(got) if got is not None else 0,
                                        "dps.notes": ",".join(new_notes) or None})
            if got is None and new_notes:  # no usable answer (AI_UNAVAILABLE, AI_BUDGET_EXCEEDED ...): a failed read
                obs.mark_failed(read_span, new_notes[0])
            span_id = obs.current_ids()[1]
            for p in got or []:
                if span_id and isinstance(p, dict):
                    p["_span"] = span_id
        if got is None:
            spent = next((n["code"] for n in (notes or [])[before:]
                          if n["code"] in ("AI_BUDGET_EXCEEDED", "AI_MONTHLY_BUDGET_SPENT",
                                           "AI_DAILY_BUDGET_SPENT")), None)
            if spent and i + 1 < len(batches):
                notes.append(issue(spent, f"{pack.name}: batches {i + 2} to {len(batches)} of "
                                          f"{len(batches)} not read by the agent"))
                break
            continue
        out = out or []
        for p in got:
            key = (str((p.get("value") or {}).get("metric") or p.get("name") or "").lower(), p.get("page"),
                   squash(p.get("quote"))[:120])
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


def _failed(pack, ex, notes: list | None) -> None:
    """Why the agent gave nothing usable, for the result's warnings; the rules' answer stands."""
    from dps.agent.gateway import AiBudgetExceeded

    code = ex.code  # AI_BUDGET_EXCEEDED, AI_MONTHLY_BUDGET_SPENT, AI_REFUSED, AI_UNAVAILABLE ...
    if not isinstance(ex, AiBudgetExceeded) and str(ex).startswith(UNGROUNDED_PREFIX):
        code = "AI_UNGROUNDED"
    log.info("%s agent unavailable: %s", pack.name, code)
    if notes is not None:
        notes.append(issue(code, f"{pack.name}: {str(ex)[:200]}"))
    return None


def normalise(value):
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, str):
        return squash(value)
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


AI_HINTS = ("obligor", "facilityRefs", "conditions", "firstTestDate", "equityCure", "baskets")


def vote(fields: list[dict], proposals: list[dict] | None, key=lambda f: f["name"], compare=None) -> list[dict]:
    """Rules vs agent per field: agree -> RULES+AI_CONFIRMED and higher confidence; disagree -> review with both
    values; agent-only (grounded) -> AI, sent to review when high-risk is decided later by the runner."""
    if proposals is None:
        return fields
    compare = compare or (lambda a, b: normalise(a) == normalise(b))
    by_key = {key(f): f for f in fields}
    for p in proposals:
        f = by_key.get(p.get("name"))
        if f is None:
            continue
        p["_matched"] = True
        hints = {k: v for k, v in (p.get("value") or {}).items() if k in AI_HINTS and v not in (None, "", [], {})}
        if hints:  # WP6: the agent's company, facility, conditions ... for obligation.enrich (checked there)
            f["value"].setdefault("_ai", {}).update(hints)
        if compare(f["value"], p.get("value")):
            f["method"] = "RULES+AI_CONFIRMED"
            f["confidence"] = round(max(f["confidence"] or 0, 0.95), 3)
        else:
            f["issues"].append(issue("MODELS_DISAGREE", f"rules read {json.dumps(f['value'], default=str)[:120]}; "
                                                        f"AI read {json.dumps(p.get('value'), default=str)[:120]} "
                                                        f"(page {p.get('page')})"))
            f["status"] = "NEEDS_REVIEW"
    return fields


NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def amount(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


SCALE = {"crore": 1e7, "cr": 1e7, "lakh": 1e5, "lac": 1e5, "million": 1e6, "mn": 1e6, "billion": 1e9,
         "thousand": 1e3, "k": 1e3}


def scaled(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    if not unit:
        return value
    u = unit.lower().rstrip("s.")
    return value * SCALE.get(u, 1)

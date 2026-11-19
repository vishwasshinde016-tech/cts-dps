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


def prompt_for(pack, pages: list[dict], extra: str = "") -> list[dict]:
    from dps.agent.guard import DATA_NOT_INSTRUCTIONS

    blocks = "\n".join(f"<<<page {p['no']}>>>\n{p.get('text') or ''}\n<<<end page {p['no']}>>>" for p in pages)
    content = "\n\n".join([HEADER.format(guard=DATA_NOT_INSTRUCTIONS), pack.prompt.strip(),
                           "Schema:\n" + json.dumps(pack.schema), extra, blocks[:MAX_PROMPT_CHARS]])
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
    messages = prompt_for(pack, pages, extra)
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


def batched_proposals(pack, gateway, batches: list[list[dict]], notes: list | None = None, **kw) -> list[dict] | None:
    """One agent read per batch of pages (long agreements), merged; None only when no batch gave an answer. A spent
    token budget stops the remaining batches, and says which were not read."""
    out: list[dict] | None = None
    seen: set[tuple] = set()
    for i, batch in enumerate(batches):
        extra = (f"These are part {i + 1} of {len(batches)} of the agreement's covenant pages: list the covenants on "
                 "these pages.") if len(batches) > 1 else ""
        before = len(notes) if notes is not None else 0
        got = agent_proposals(pack, gateway, batch, extra=extra, notes=notes, **kw)
        if got is None:
            spent = notes is not None and any(n["code"] == "AI_BUDGET_EXCEEDED" for n in notes[before:])
            if spent and i + 1 < len(batches):
                notes.append(issue("AI_BUDGET_EXCEEDED", f"{pack.name}: batches {i + 2} to {len(batches)} of "
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

    code = ex.code
    if isinstance(ex, AiBudgetExceeded):
        code = "AI_BUDGET_EXCEEDED"
    elif str(ex).startswith(UNGROUNDED_PREFIX):
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

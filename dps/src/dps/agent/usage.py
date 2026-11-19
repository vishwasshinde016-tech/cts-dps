"""AI usage and traceability: every model call, recorded per request (review GAP-05, docs/observability.md).

The completion notes each call it makes in a context variable: tokens, and for traceability the messages sent,
the answer, the time taken and whether it failed. The endpoint that asked for the answer (or the worker, for the
AI readers of a document) then stores one ``dps_ai_usage`` row with the totals and one ``dps_ai_call`` row per
call. A request that made no model call (rules answers) stores nothing. Cost uses the per-1,000-token rates in
settings; with no rates set it is left empty.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dps.models import AiCall, AiUsage, utcnow
from dps.ops import observability as obs
from dps.settings import Settings

# AGREEMENT_READ / STATEMENT_READ: the AI readers that check a document in the background (one call per document).
PURPOSES = ("ASK_AI", "AGREEMENT_QA", "SUGGESTIONS", "NARRATIVE", "AGREEMENT_READ", "STATEMENT_READ")
# Longest text kept per call: enough for the pages of a long agreement, small enough for the database.
MAX_TEXT = 60_000


@dataclass
class Call:
    model: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float = 0.0
    messages: list[dict] | None = None
    answer: str | None = None
    error: str | None = None


@dataclass
class Tokens:
    model: str
    prompt: int = 0
    completion: int = 0
    calls: int = 0
    detail: list[Call] = field(default_factory=list)


_noted: ContextVar[Tokens | None] = ContextVar("dps_ai_tokens", default=None)
# Tokens of the most recent call only (the gateway charges them to the job's budget).
_last: ContextVar[int] = ContextVar("dps_ai_last_call_tokens", default=0)


def begin_call() -> None:
    _last.set(0)


def last_call_tokens() -> int:
    return _last.get()


def note(model: str, prompt_tokens: int, completion_tokens: int, *, seconds: float = 0.0,
         messages: list[dict] | None = None, answer: str | None = None, error: str | None = None) -> None:
    """Called by the completion after each model call (or failed call); several calls in one request add up."""
    _last.set(_last.get() + int(prompt_tokens or 0) + int(completion_tokens or 0))
    current = _noted.get()
    if current is None:
        current = Tokens(model)
        _noted.set(current)
    current.model = current.model or model
    current.prompt += int(prompt_tokens or 0)
    current.completion += int(completion_tokens or 0)
    current.calls += 1
    current.detail.append(Call(model, int(prompt_tokens or 0), int(completion_tokens or 0), seconds, messages,
                               answer, error))


def begin() -> None:
    """Starts counting for one background job. The flow may run its steps in another context: the counter set here
    is the object they all add to, so the worker reads the job's calls afterwards with ``take``."""
    _noted.set(Tokens(""))


def take() -> Tokens | None:
    """The tokens noted so far in this request, and a fresh start."""
    current = _noted.get()
    _noted.set(None)
    return current


def cost(settings: Settings, tokens: Tokens) -> float | None:
    return _cost(settings, tokens.prompt, tokens.completion)


def _cost(settings: Settings, prompt: int, completion: int) -> float | None:
    if settings.ai_prompt_cost_per_1k is None and settings.ai_completion_cost_per_1k is None:
        return None
    return round(prompt / 1000 * (settings.ai_prompt_cost_per_1k or 0.0)
                 + completion / 1000 * (settings.ai_completion_cost_per_1k or 0.0), 6)


def prompt_version(messages: list[dict] | None) -> str:
    """A short fingerprint of the system prompt: a changed prompt shows as a new version in the trace."""
    system = next((str(m.get("content", "")) for m in messages or [] if m.get("role") == "system"), "")
    return hashlib.sha256(system.encode()).hexdigest()[:8] if system else "-"


def _text(value: object, keep: bool) -> str | None:
    if not keep or value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return obs.redact(text)[:MAX_TEXT]


def record(db: Session, settings: Settings, purpose: str, user: str, *, cp_id: str | None = None,
           document_id: str | None = None, job_id: str | None = None) -> AiUsage | None:
    """Stores the request's model usage, if it made any model call."""
    tokens = take()
    if tokens is None or not tokens.calls:
        return None
    row = AiUsage(purpose=purpose, user=user, cp_id=cp_id, document_id=document_id, job_id=job_id,
                  model=tokens.model, calls=tokens.calls, prompt_tokens=tokens.prompt,
                  completion_tokens=tokens.completion, cost=cost(settings, tokens))
    db.add(row)
    request_id = obs.request_id.get()
    for call in tokens.detail:
        status = "ERROR" if call.error else "OK"
        call_cost = _cost(settings, call.prompt_tokens, call.completion_tokens)
        db.add(AiCall(purpose=purpose, user=user, cp_id=cp_id, document_id=document_id, job_id=job_id,
                      request_id=None if request_id == "-" else request_id, model=call.model,
                      prompt_version=prompt_version(call.messages), status=status,
                      error=(call.error or "")[:500] or None, seconds=call.seconds,
                      prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens, cost=call_cost,
                      request_text=_text(call.messages, settings.ai_trace_text),
                      response_text=_text(call.answer, settings.ai_trace_text)))
        price = f", cost {call_cost}" if call_cost is not None else ""
        obs.event("AI_CALL", f"{purpose} {call.model} {status} {call.seconds:.1f} s, "
                  f"{call.prompt_tokens} in / {call.completion_tokens} out tokens{price}"
                  + (f": {call.error}" if call.error else ""),
                  purpose=purpose, model=call.model, status=status, seconds=call.seconds,
                  prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens, cost=call_cost,
                  cp_id=cp_id, prompt_version=prompt_version(call.messages))
        labels = {"purpose": purpose, "model": call.model}
        obs.count("dps.ai.calls", 1, status=status, **labels)
        obs.count("dps.ai.tokens", call.prompt_tokens, kind="prompt", **labels)
        obs.count("dps.ai.tokens", call.completion_tokens, kind="completion", **labels)
        if call_cost is not None:
            obs.count("dps.ai.cost", call_cost, **labels)
        obs.observe("dps.ai.call.duration", call.seconds, **labels)
    db.commit()
    return row


def summary(db: Session, days: int) -> dict:
    """Totals per purpose over the last ``days`` days, for support and administrators."""
    since = utcnow() - timedelta(days=days)
    rows = db.execute(select(AiUsage.purpose, func.count(), func.sum(AiUsage.calls), func.sum(AiUsage.prompt_tokens),
                             func.sum(AiUsage.completion_tokens), func.sum(AiUsage.cost))
                      .where(AiUsage.at >= since).group_by(AiUsage.purpose)).all()
    by_purpose = [{"purpose": p, "requests": n, "calls": int(c or 0), "promptTokens": int(pt or 0),
                   "completionTokens": int(ct or 0), "cost": None if cost_sum is None else round(float(cost_sum), 4)}
                  for p, n, c, pt, ct, cost_sum in rows]
    costs = [r["cost"] for r in by_purpose if r["cost"] is not None]
    return {"days": days, "byPurpose": sorted(by_purpose, key=lambda r: r["purpose"]),
            "promptTokens": sum(r["promptTokens"] for r in by_purpose),
            "completionTokens": sum(r["completionTokens"] for r in by_purpose),
            "cost": round(sum(costs), 4) if costs else None}

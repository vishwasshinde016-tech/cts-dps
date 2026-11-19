"""The Document Agent: one CrewAI ``Agent`` per job, with the job's skill, tools and prompt instructions.

The architect's design (Teams, points 4 and 5): "for each job an isolated execution context with a small subset of
skills, tools and prompt instructions" and "1 agent with multiple skills and tools". Here:

- **one agent**: ``Agent`` is built fresh for every read, with no memory and no delegation, so nothing from one job
  reaches another;
- **the skill**: the job's skill pack gives the role, the prompt instructions (``prompt.md``), the answer schema
  (``schema.json``) and the limits (``pack.yaml``);
- **the tools**: only the tools the pack lists, as CrewAI ``BaseTool`` objects: the page tools read the pages this
  job selected (never the whole file), the Spring tools call Java business rules (formula check, covenant library,
  identifier lookups) in batches;
- **the model**: every call goes through the DPS model gateway (``GatewayLLM``), so the gateway's retries, circuit
  breaker, rate limits, per-job token budget, masking and call log apply to agent calls too.

The agent proposes; it never decides. Its answer must match the pack's schema and every quote must be on the page it
names (grounding), or the answer is repaired once and then dropped. Rules, voting, Java checks and a person
decide what is accepted.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from crewai import Agent
from crewai.llms.base_llm import BaseLLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from dps.agent.gateway import Gateway, ModelOutputInvalid, parse_json

log = logging.getLogger(__name__)

ROLE = "Document Agent"
GOAL = ("Read one bank lending document page by page and propose the values the {skill} skill asks for, each with "
        "the page and the exact quote it comes from.")
BACKSTORY = ("You work inside a covenant tracking system. You propose values; rules, a second model, Java business "
             "rules and a person decide. {guard} Never guess a number; use null when the document does not say.")
FINAL = ("Use the tools when you need a page, a definition, the covenant library or a formula check. When you are "
         "done, give the Final Answer as the JSON object only, matching the schema above.")
PAGE_LIMIT = 12_000
SEARCH_HITS = 15


class GatewayLLM(BaseLLM):
    """A CrewAI LLM whose every call goes through ``Gateway.call`` on one route (strong, small, vision)."""

    llm_type: str = "dps-gateway"
    _gateway: Gateway = PrivateAttr()
    _route: str = PrivateAttr()
    _budget: object = PrivateAttr(default=None)
    _context: contextvars.Context = PrivateAttr()

    def __init__(self, gateway, route: str, **kwargs) -> None:
        super().__init__(model=f"dps-gateway/{route}", **kwargs)
        self._gateway = gateway
        self._route = route
        # Taken on the job's thread: CrewAI calls the model from its own threads, and the job's token budget and its
        # call log (usage: cost per job) must still see every call. One agent makes one call at a time.
        current = getattr(gateway, "current_budget", None)
        self._budget = current() if callable(current) else None
        self._context = contextvars.copy_context()

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None):
        if isinstance(messages, str):
            msgs = [{"role": "user", "content": messages}]
        else:
            msgs = [{"role": m.get("role", "user"), "content": m.get("content") or ""} for m in messages]
        bound = getattr(self._gateway, "call_with_budget", None)
        if callable(bound):
            text = self._context.copy().run(bound, self._budget, self._route, msgs)
        else:
            text = self._gateway.call(self._route, msgs)
        return self._apply_stop_words(text) if self.stop else text

    def supports_function_calling(self) -> bool:
        return False  # tools run through CrewAI's text protocol, so any chat deployment works

    def supports_stop_words(self) -> bool:
        return True


@dataclass
class ToolContext:
    """What one job's tools may see and call. ``limit`` caps the tool calls of one read (pack ``max_tool_calls``)."""

    pages: list[dict]
    spring: object | None = None
    correlation_id: str | None = None
    lookup_context: dict | None = None
    limit: int = 8
    used: int = 0
    calls: list[str] = field(default_factory=list)

    def spend(self, name: str) -> str | None:
        if self.used >= self.limit:
            return f"Tool limit of {self.limit} calls reached. Give the Final Answer now."
        self.used += 1
        self.calls.append(name)
        return None

    def page(self, no: int) -> dict | None:
        return next((p for p in self.pages if p.get("no") == no), None)


class PageArgs(BaseModel):
    page: int = Field(..., description="Page number, starting at 1")


class QueryArgs(BaseModel):
    query: str = Field(..., min_length=2, max_length=200, description="Words to look for")


class TermArgs(BaseModel):
    term: str = Field(..., min_length=2, max_length=120, description="The defined term, for example EBITDA")


class NameArgs(BaseModel):
    name: str = Field(..., min_length=2, max_length=200, description="Covenant or metric name")


class ExpressionArgs(BaseModel):
    expression: str = Field(..., min_length=3, max_length=500, description="Covenant formula, e.g. DSCR >= 1.2")


class IdentifierArgs(BaseModel):
    kind: str = Field(..., description="PAN, CIN, GSTIN, LEI, LOAN_ACCOUNT or FACILITY_ID")
    value: str = Field(..., min_length=2, max_length=64)


class _DpsTool(BaseTool):
    _ctx: ToolContext = PrivateAttr()

    def __init__(self, ctx: ToolContext, **kwargs) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx


class PageTextTool(_DpsTool):
    name: str = "page_text"
    description: str = "The text of any page of the document, by page number."
    args_schema: type[BaseModel] = PageArgs

    def _run(self, page: int) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        p = self._ctx.page(int(page))
        if p is None:
            numbers = [int(x.get("no")) for x in self._ctx.pages if x.get("no") is not None]
            span = f"{min(numbers)} to {max(numbers)}" if numbers else "none"
            return f"Page {page} is not in the document. Pages: {span}."
        return (p.get("text") or "")[:PAGE_LIMIT] or "(this page has no readable text)"


class SearchTextTool(_DpsTool):
    name: str = "search_text"
    description: str = "Lines of the whole document that contain the words, with their page numbers."
    args_schema: type[BaseModel] = QueryArgs

    def _run(self, query: str) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        needle = re.sub(r"\s+", " ", query).strip().lower()
        hits = []
        for p in self._ctx.pages:
            for line in (p.get("text") or "").splitlines():
                if needle in re.sub(r"\s+", " ", line).lower():
                    hits.append(f"page {p.get('no')}: {line.strip()[:300]}")
                    if len(hits) >= SEARCH_HITS:
                        return "\n".join(hits)
        return "\n".join(hits) or f"No line contains {query!r}."


class FindDefinitionTool(_DpsTool):
    name: str = "find_definition"
    description: str = "Where the document defines a term (\"X\" means ..., X shall mean ...)."
    args_schema: type[BaseModel] = TermArgs

    def _run(self, term: str) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        pattern = re.compile(r"[\"“']?" + re.escape(term.strip()) + r"[\"”']?\s*(?:shall\s+)?mean", re.I)
        for p in self._ctx.pages:
            text = p.get("text") or ""
            m = pattern.search(text)
            if m:
                end = text.find("\n\n", m.end())
                return f"page {p.get('no')}: {text[m.start(): end if end != -1 else m.start() + 800][:800]}"
        return f"No definition of {term!r} in the document."


class CovenantLibraryTool(_DpsTool):
    name: str = "covenant_library"
    description: str = "Find a covenant in the bank's covenant library (Java, in the Spring app) by name or metric."
    args_schema: type[BaseModel] = NameArgs

    def _run(self, name: str) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        if self._ctx.spring is None:
            return "The covenant library is not available."
        items, fresh = self._ctx.spring.covenant_library()
        if not items:
            return "The covenant library is not reachable now." if not fresh else "The library is empty."
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        for item in items:
            keys = [item.get("name", ""), *item.get("aliases", []), *item.get("metricKeys", [])]
            if any(key and key == re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys if k):
                return json.dumps({"id": item.get("id"), "name": item.get("name"),
                                   "expression": item.get("expression")})
        return f"No library covenant matches {name!r}."


class ValidateFormulaTool(_DpsTool):
    name: str = "validate_formula"
    description: str = "Check a covenant formula with the Spring app's formula engine (Java)."
    args_schema: type[BaseModel] = ExpressionArgs

    def _run(self, expression: str) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        if self._ctx.spring is None:
            return "Formula checks are not available."
        check = self._ctx.spring.validate_formulas([("agent", expression)], self._ctx.correlation_id).get("agent")
        if check is None or not check.available:
            return "The formula engine is not reachable now."
        if check.valid:
            return "valid"
        return "invalid: " + "; ".join(str(e.get("message") or e.get("code")) for e in check.errors)[:300]


class LookupIdentifierTool(_DpsTool):
    name: str = "lookup_identifier"
    description: str = "Check a borrower or facility identifier against the bank's master data (Spring app)."
    args_schema: type[BaseModel] = IdentifierArgs

    def _run(self, kind: str, value: str) -> str:
        stop = self._ctx.spend(self.name)
        if stop:
            return stop
        if self._ctx.spring is None:
            return "Identifier lookups are not available."
        try:
            hit = self._ctx.spring.lookup_identifiers([("agent", kind.upper(), value)],
                                                     context=self._ctx.lookup_context or {},
                                                     correlation_id=self._ctx.correlation_id).get("agent")
        except ValueError as ex:
            return f"Not a supported identifier: {ex}"
        if hit is None or not hit.available:
            return "Master data is not reachable now."
        return f"{hit.status}: " + json.dumps(hit.candidates[:3], default=str)


TOOLS: dict[str, type[_DpsTool]] = {
    "page_text": PageTextTool, "search_text": SearchTextTool, "find_definition": FindDefinitionTool,
    "covenant_library": CovenantLibraryTool, "validate_formula": ValidateFormulaTool,
    "lookup_identifier": LookupIdentifierTool,
}


def build_tools(names, ctx: ToolContext) -> list[BaseTool]:
    """The pack's tools (unknown names are ignored, so a pack file cannot reach anything else)."""
    return [TOOLS[n](ctx) for n in names if n in TOOLS]


def make_agent(gateway, route: str, skill: str, tool_names, ctx: ToolContext, max_turns: int = 3,
               max_seconds: int = 180) -> Agent:
    """A fresh agent for one job: no memory, no delegation, the skill's tools only."""
    from dps.agent.guard import DATA_NOT_INSTRUCTIONS

    tools = build_tools(tool_names, ctx)
    return Agent(role=f"{ROLE} ({skill} skill)", goal=GOAL.format(skill=skill),
                 backstory=BACKSTORY.format(guard=DATA_NOT_INSTRUCTIONS), llm=GatewayLLM(gateway, route),
                 tools=tools, allow_delegation=False, max_iter=max(1, max_turns) + ctx.limit,
                 max_execution_time=max_seconds, max_retry_limit=0, verbose=False)


def _raw(output) -> str:
    return str(getattr(output, "raw", output) or "")


def read(pack, gateway, pages: list[dict], task: str, route: str | None = None, spring=None,
         correlation_id: str | None = None, lookup_context: dict | None = None,
         check: Callable[[object], str | None] | None = None,
         all_pages: list[dict] | None = None) -> tuple[dict, ToolContext]:
    """Runs the job's agent on ``task`` (the pack prompt, schema and pages). Returns the parsed answer and the tool
    log. A wrong answer is repaired once on the same route; then ``ModelOutputInvalid``. Gateway errors (model down,
    budget used) pass through unchanged so the caller degrades to rules only."""
    route = route or pack.routes.get("extract", "strong")
    limits = pack.limits or {}
    # The prompt holds ``pages``; the tools reach every page of the document (``all_pages``).
    ctx = ToolContext(all_pages or pages, spring, correlation_id, lookup_context, int(limits.get("max_tool_calls", 6)))
    agent = make_agent(gateway, route, pack.name, pack.tools, ctx, int(limits.get("max_model_turns", 3)),
                       int(limits.get("max_seconds", 180)))
    prompt = f"{task}\n\n{FINAL}"
    answer = _raw(agent.kickoff(prompt))
    error = Gateway._invalid(answer, pack.schema, check)
    if error is None:
        return parse_json(answer), ctx
    repair = [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer},
              {"role": "user", "content": f"Your answer was not valid: {error}. Reply again with only the corrected "
                                          "JSON."}]
    answer = gateway.call(route, repair)
    error = Gateway._invalid(answer, pack.schema, check)
    if error is None:
        return parse_json(answer), ctx
    raise ModelOutputInvalid(error, Gateway.valid_data(answer, pack.schema))


def completion(gateway, route: str, skill: str, tool_names=(), spring=None,
               correlation_id: str | None = None) -> Callable[[list[dict]], str]:
    """A plain ``Completion`` (messages -> text) answered by a fresh Document Agent on each call, for the agreement and
    statement readers that build their own prompt."""

    def call(messages: list[dict]) -> str:
        text = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")
        ctx = ToolContext([], spring, correlation_id, None, 4)
        agent = make_agent(gateway, route, skill, tool_names, ctx)
        return _raw(agent.kickoff(f"{text}\n\nGive the Final Answer exactly in the format asked above."))

    return call

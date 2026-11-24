"""Model gateway (Phase 3): the one place a DPS model call goes through.

A route file maps names (small, strong, vision) to deployments. Every call gets:
- the existing guards (``ai_guard.guarded``): masking, cache, monthly budget, call log via ``usage.note``;
- retries on timeout, 429 and 5xx with back-off 2 s, 8 s, 30 s and jitter;
- a circuit breaker per route (5 failures in 60 s, half-open after 30 s) -> ``AiUnavailable`` (AI_UNAVAILABLE);
- a token bucket per route sized to the deployment's requests per minute;
- a per-job token budget -> ``AiBudgetExceeded`` (AI_BUDGET_EXCEEDED); the month's budget spent ->
  ``AiMonthlyBudgetSpent`` (AI_MONTHLY_BUDGET_SPENT) and a content-filter refusal -> ``AiRefused`` (AI_REFUSED),
  neither of which counts against the circuit breaker (the model is up);
- structured output: the answer must parse as JSON and match the given JSON schema; one repair call, then
  ``ModelOutputInvalid`` (MODEL_OUTPUT_INVALID).

Callers degrade on these errors (rules only, fields to review); a model problem never fails a job by itself.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

from dps.agent import usage
from dps.integrations.spring_tools import CircuitBreaker
from dps.ops import observability as obs

log = logging.getLogger(__name__)

Completion = Callable[[list[dict]], str]
BACKOFF = (2.0, 8.0, 30.0)


class GatewayError(RuntimeError):
    code = "AI_UNAVAILABLE"


class AiUnavailable(GatewayError):
    code = "AI_UNAVAILABLE"


class AiBudgetExceeded(GatewayError):
    code = "AI_BUDGET_EXCEEDED"


class AiMonthlyBudgetSpent(AiBudgetExceeded):
    """The month's token budget (``guard.check_budget``) is spent: not an outage, so the circuit stays closed
    (Phase 3 fix 3, 23 Nov 2026). Before, it counted as a model failure and opened the breaker for the next jobs."""

    code = "AI_MONTHLY_BUDGET_SPENT"


class AiDailyBudgetSpent(AiMonthlyBudgetSpent):
    """The day's token budget is spent (improvement plan S11): not an outage either."""

    code = "AI_DAILY_BUDGET_SPENT"


class AiRefused(GatewayError):
    """The deployment answered but refused (Azure content filter): the model is up, so the circuit stays closed."""

    code = "AI_REFUSED"


class ModelOutputInvalid(GatewayError):
    code = "MODEL_OUTPUT_INVALID"

    def __init__(self, message: str = "", data: object | None = None) -> None:
        super().__init__(message)
        self.data = data  # the parsed, schema-valid answer when only the caller's check failed


class TransientModelError(RuntimeError):
    """Raised by providers (or mapped from their errors) for timeouts, 429 and 5xx."""


class ModelRefused(RuntimeError):
    """Raised by providers (or mapped from their errors) when the model refuses the request (content filter)."""


# Provider error text that means a refusal, not an outage (LiteLLM's ContentPolicyViolationError, Azure's
# content_filter / ResponsibleAIPolicyViolation).
_REFUSAL = ("contentpolicyviolation", "content_filter", "content management policy", "responsibleaipolicyviolation")


@dataclass(frozen=True)
class Route:
    name: str
    provider: str
    deployment: str
    max_tokens: int = 3000
    timeout_s: float = 60.0
    temperature: float | None = None  # None: the model's default (newer Azure OpenAI models refuse any other)
    retries: int = 3
    rpm: int = 300


@dataclass
class Routes:
    routes: dict[str, Route]
    per_job_tokens: int = 150_000
    per_tenant_day_tokens: int = 20_000_000

    @classmethod
    def load(cls, path: str | Path | None, default_model: str = "", share: int = 1) -> Routes:
        """The route file, or (no file) every route on ``default_model`` so today's single-model set-up works.

        ``share``: how many worker processes call the same deployments. Each process keeps its own token bucket, so
        each gets ``rpm / share`` and together they stay within the deployment's limit (full review G16)."""
        if path and Path(path).is_file():
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        else:
            raw = {"routes": {n: {"provider": "default", "deployment": default_model}
                              for n in ("small", "strong", "vision")}}
        defaults = raw.get("defaults") or {}
        routes = {}
        for name, spec in (raw.get("routes") or {}).items():
            merged = {**defaults, **(spec or {})}
            routes[name] = Route(name, str(merged.get("provider", "default")), str(merged.get("deployment", "")),
                                 int(merged.get("max_tokens", 3000)), float(merged.get("timeout_s", 60)),
                                 None if merged.get("temperature") is None else float(merged["temperature"]),
                                 int(merged.get("retries", 3)),
                                 max(1, int(merged.get("rpm", 300)) // max(1, share)))
        budgets = raw.get("budgets") or {}
        return cls(routes, int(budgets.get("per_job_tokens", 150_000)),
                   int(budgets.get("per_tenant_day_tokens", 20_000_000)))


def temperature_for(route: Route, settings) -> float | None:
    """The route's temperature from the routes file when it sets one (e.g. 0 for steady answers where the model allows
    it), else DPS_AI_TEMPERATURE; None keeps the model's default. The routes file's value was ignored before (review of
    22 Nov 2026)."""
    return route.temperature if route.temperature is not None else getattr(settings, "ai_temperature", None)


class TokenBucket:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            self._sleep(wait)


@dataclass
class JobBudget:
    limit: int
    used: int = 0
    calls: int = 0
    models: dict[str, str] = field(default_factory=dict)


_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.S)


def parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise
        return json.loads(match.group(0))


class Gateway:
    def __init__(self, routes: Routes, provider: Callable[[Route], Completion],
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 guard: Callable[[Completion, str], Completion] | None = None, trace_text: bool = False) -> None:
        self.routes = routes
        self._provider = provider
        # The text sent and the answer on the model call's trace span (masked), only when ``ai_trace_text`` is on.
        self.trace_text = trace_text
        self._sleep, self._clock = sleep, clock
        self._guard = guard
        self._breakers = {n: CircuitBreaker(clock=clock) for n in routes.routes}
        self._buckets = {n: TokenBucket(r.rpm, clock, sleep) for n, r in routes.routes.items()}
        self._completions: dict[str, Completion] = {}
        self._local = threading.local()

    # -- per-job budget -----------------------------------------------------------------------------------------

    def begin_job(self, limit: int | None = None) -> JobBudget:
        self._local.budget = JobBudget(limit or self.routes.per_job_tokens)
        return self._local.budget

    def end_job(self) -> JobBudget | None:
        budget = getattr(self._local, "budget", None)
        self._local.budget = None
        return budget

    def _budget(self) -> JobBudget | None:
        return getattr(self._local, "budget", None)

    def current_budget(self) -> JobBudget | None:
        """The job budget of the calling thread (handed to agents that call the model from other threads)."""
        return self._budget()

    def call_with_budget(self, budget: JobBudget | None, route_name: str, messages: list[dict]) -> str:
        """``call`` on this thread with the job's budget: CrewAI runs an agent's model calls on its own threads, and
        the per-job token budget and usage must still apply to them."""
        previous = getattr(self._local, "budget", None)
        self._local.budget = budget
        try:
            return self.call(route_name, messages)
        finally:
            self._local.budget = previous

    # -- calls --------------------------------------------------------------------------------------------------

    def _completion(self, route: Route) -> Completion:
        if route.name not in self._completions:
            raw = self._provider(route)
            self._completions[route.name] = self._guard(raw, route.deployment) if self._guard else raw
        return self._completions[route.name]

    def available(self, route_name: str) -> bool:
        route = self.routes.routes.get(route_name)
        return bool(route and route.deployment) and self._breakers[route_name].allow()

    def call(self, route_name: str, messages: list[dict]) -> str:
        route = self.routes.routes.get(route_name)
        if route is None or not route.deployment:
            raise AiUnavailable(f"No model configured for route {route_name}")
        budget = self._budget()
        if budget is not None and budget.used >= budget.limit:
            raise AiBudgetExceeded(f"Job token budget of {budget.limit} used")
        breaker = self._breakers[route_name]
        if not breaker.allow():
            obs.count("dps.ai.unavailable", 1, route=route_name)
        completion = self._completion(route)
        last: Exception | None = None
        for attempt in range(route.retries + 1):
            if not breaker.allow():
                raise AiUnavailable(f"Route {route_name} circuit open")
            self._buckets[route_name].take()
            usage.begin_call()
            retry = False
            # One trace span per model call (observability, 23 Nov 2026): Langfuse shows it as a generation with its
            # model, tokens and time; a retried attempt is its own span, marked failed.
            with obs.span(f"chat {route.deployment}", kind="client", **{
                    "gen_ai.operation.name": "chat", "gen_ai.request.model": route.deployment,
                    "dps.route": route_name, "dps.attempt": attempt + 1,
                    "dps.prompt_version": usage.prompt_version(messages)}) as model_span:
                try:
                    answer = completion(messages)
                except TransientModelError as ex:
                    breaker.failure()
                    last, retry = ex, True
                    obs.mark_failed(model_span, ex)
                except ModelRefused as ex:  # the model is up and said no: not a breaker failure (Phase 3 fix 3)
                    obs.mark_failed(model_span, ex)
                    obs.count("dps.ai.refused", 1, route=route_name)
                    raise AiRefused(f"{route.deployment} refused the request: {str(ex)[:200]}") from ex
                except Exception as ex:  # noqa: BLE001 - provider bugs and auth errors count as an outage
                    from dps.agent.guard import AiBudgetExhausted, AiDailyBudgetExhausted

                    obs.mark_failed(model_span, ex)
                    if isinstance(ex, AiBudgetExhausted):  # a spent month is not an outage (Phase 3 fix 3)
                        obs.count("dps.ai.budget_spent", 1, route=route_name)
                        spent = AiDailyBudgetSpent if isinstance(ex, AiDailyBudgetExhausted) else AiMonthlyBudgetSpent
                        raise spent(str(ex)[:200]) from ex
                    breaker.failure()
                    raise AiUnavailable(f"{type(ex).__name__}: {str(ex)[:200]}") from ex
                else:
                    self._on_span(model_span, messages, answer)
            if retry:  # the back-off wait is outside the span: the span shows the call's own time
                if attempt < route.retries:
                    delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    self._sleep(delay * random.uniform(0.8, 1.2))  # retry jitter  # noqa: S311  # nosec B311
                continue
            breaker.success()
            if budget is not None:
                budget.used += usage.last_call_tokens()
                budget.calls += 1
                budget.models[route_name] = route.deployment
            return answer
        raise AiUnavailable(f"Route {route_name} failed after {route.retries + 1} attempts: {last}")

    def _on_span(self, model_span, messages: list[dict], answer: str) -> None:
        """Tokens of the call that just ended, and (with ``trace_text``) the masked text, on its trace span."""
        if model_span is None or not model_span.is_recording():
            return
        span_id = format(model_span.get_span_context().span_id, "016x")
        call = usage.last_call(span_id)
        if call is not None:
            # The current names and the older ones (still read by Langfuse): the GenAI conventions are in Development.
            obs.set_attrs(model_span, **{
                "gen_ai.usage.input_tokens": call.prompt_tokens, "gen_ai.usage.output_tokens": call.completion_tokens,
                "gen_ai.usage.prompt_tokens": call.prompt_tokens,
                "gen_ai.usage.completion_tokens": call.completion_tokens,
                "gen_ai.response.model": call.model})
        else:
            obs.set_attrs(model_span, **{"dps.cache_hit": True})  # answered from dps_ai_cache: no model call made
        if self.trace_text:
            from dps.agent.guard import _masked

            safe = [_masked(m) for m in messages]
            obs.set_attrs(model_span, **{
                "langfuse.observation.input": obs.redact(json.dumps(safe, ensure_ascii=False))[:usage.MAX_TEXT],
                "langfuse.observation.output": obs.redact(answer or "")[:usage.MAX_TEXT]})

    def call_json(self, route_name: str, messages: list[dict], schema: dict | None = None,
                  check: Callable[[object], str | None] | None = None):
        """A JSON answer that matches ``schema`` and passes ``check`` (which returns an error text or None).
        One repair call with the error; then ModelOutputInvalid."""
        answer = self.call(route_name, messages)
        error = self._invalid(answer, schema, check)
        if error is None:
            return parse_json(answer)
        repair = [*messages, {"role": "assistant", "content": answer},
                  {"role": "user", "content": f"Your answer was not valid: {error}. Reply again with only the "
                                              "corrected JSON."}]
        answer = self.call(route_name, repair)
        error = self._invalid(answer, schema, check)
        if error is None:
            return parse_json(answer)
        raise ModelOutputInvalid(error, self.valid_data(answer, schema))

    @staticmethod
    def valid_data(answer: str, schema: dict | None):
        """The parsed answer when it is JSON and matches ``schema``; else None."""
        if Gateway._invalid(answer, schema, None) is not None:
            return None
        return parse_json(answer)

    @staticmethod
    def _invalid(answer: str, schema: dict | None, check) -> str | None:
        try:
            data = parse_json(answer)
        except (json.JSONDecodeError, TypeError):
            return "not JSON"
        if schema is not None:
            try:
                jsonschema.validate(data, schema)
            except jsonschema.ValidationError as ex:
                return f"schema: {ex.message[:200]}"
        if check is not None:
            return check(data)
        return None

    def completion(self, route_name: str) -> Completion:
        """A plain ``Completion`` for the older readers (agreement, statement, advisor) on this gateway."""
        return lambda messages: self.call(route_name, messages)


class LlmDisabled(RuntimeError):
    """No model may be used: the switch is off, no model is configured, or the endpoint is not an allowed host."""


# The only model provider DPS may call: LiteLLM sends an "azure/..." model to AZURE_API_BASE, which
# ``endpoint_problem`` checks against DPS_AI_ALLOWED_HOSTS. Any other prefix ("openai/", "anthropic/", none at all)
# goes to that provider's own public host, which the host check never sees (Phase 3 fix 4, 23 Nov 2026).
ALLOWED_PROVIDERS = ("azure/",)


def route_problem(routes: Routes) -> str | None:
    """Why the routes may not be used (None when they may): a route whose model is not on an allowed provider."""
    for name, route in sorted(routes.routes.items()):
        model = (route.deployment or "").strip().lower()
        if model and not model.startswith(ALLOWED_PROVIDERS):
            return (f"route {name} uses model {route.deployment!r}; only {', '.join(ALLOWED_PROVIDERS)} models may "
                    "be called (the bank's own deployment on an allowed host)")
    return None


def from_settings(settings, session_factory=None, env: dict | None = None) -> Gateway:
    from dps.settings import endpoint_problem, llm_allowed

    if not llm_allowed(settings):
        raise LlmDisabled("LLM use is off (DPS_LLM_ENABLED=false or no DPS_AI_MODEL / routes file)")
    problem = endpoint_problem(settings, env) or route_problem(
        Routes.load(getattr(settings, "ai_routes_file", "") or None, settings.ai_model))
    if problem:
        raise LlmDisabled(f"LLM refused: {problem}")
    return _live_gateway(settings, session_factory)  # pragma: no cover - live model wiring


def _live_gateway(settings, session_factory=None) -> Gateway:  # pragma: no cover - live wiring
    from dps.advisory.advisor import crewai_completion
    from dps.agent.guard import guarded

    routes = Routes.load(getattr(settings, "ai_routes_file", "") or None, settings.ai_model,
                         int(getattr(settings, "ai_rpm_share", 1) or 1))

    def provider(route: Route) -> Completion:
        raw = crewai_completion(route.deployment, temperature_for(route, settings), route.timeout_s)

        def call(messages: list[dict]) -> str:
            try:
                return raw(messages)
            except Exception as ex:
                text = f"{type(ex).__name__} {ex}".lower()
                if any(s in text for s in _REFUSAL):  # checked first: a refusal's text can hold "400" or "500"
                    raise ModelRefused(str(ex)[:300]) from ex
                if any(s in text for s in ("timeout", "timed out", "429", "rate limit", "503", "502", "500",
                                           "overloaded", "connection")):
                    raise TransientModelError(str(ex)[:300]) from ex
                raise

        return call

    daily = routes.per_tenant_day_tokens if getattr(settings, "ai_routes_file", "") else None
    guard = (lambda c, model: guarded(c, model, session_factory, settings, daily=daily)) if session_factory else None
    return Gateway(routes, provider, guard=guard, trace_text=bool(getattr(settings, "ai_trace_text", False)))

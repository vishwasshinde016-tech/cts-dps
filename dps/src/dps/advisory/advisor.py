"""C3 and O8: advisory AI. It never decides compliance: Core does. Output is labelled advisory everywhere.

Without a model configured (``DPS_AI_MODEL`` empty) the advisor gives deterministic suggestions that just
mirror the assessed result, so the feature degrades to "no AI" rather than failing. With a model (e.g.
``azure/<deployment>``; credentials from AZURE_API_KEY / AZURE_API_BASE / AZURE_API_VERSION) it asks the
LLM through the DPS gateway and falls back to the deterministic answer on any error.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

log = logging.getLogger(__name__)

STATUSES = {"COMPLIED", "BREACHED", "WAIVER", "EXTENSION"}
ADVISORY = "Advisory only (AI); the covenant status is decided in Core."

SYSTEM_PROMPT = (
    "You review loan covenant results for a credit officer. For each covenant, suggest one status: "
    "COMPLIED, BREACHED, WAIVER or EXTENSION, a one-sentence rationale grounded only in the data given, "
    "and a confidence 0-100. Suggest WAIVER or EXTENSION only for a breach the data shows is small or "
    "temporary. Never invent numbers. Reply with a JSON array of objects with keys appliedCovenantId, "
    "recommendedStatus, rationale, confidence and nothing else."
)

Completion = Callable[[list[dict]], str]


def deterministic(covenants: list[dict]) -> list[dict]:
    out = []
    for c in covenants:
        result = (c.get("latestResult") or "").upper()
        if result == "COMPLIANT":
            status, why = "COMPLIED", "Latest assessment is compliant."
        elif result == "BREACHED":
            value = c.get("computedValue")
            status = "BREACHED"
            why = f"Latest assessment breached ({c.get('conditionExpression')}, actual {value})." if value \
                else "Latest assessment breached."
        else:
            continue  # ERROR / pending: nothing sound to suggest
        out.append({"appliedCovenantId": c["appliedCovenantId"], "recommendedStatus": status,
                    "rationale": f"{why} {ADVISORY}", "confidence": None})
    return out


def _parse(text: str, allowed_ids: set[str]) -> list[dict]:
    match = re.search(r"\[.*\]", text or "", re.S)
    if not match:
        raise ValueError("no JSON array in model output")
    rows = json.loads(match.group(0))
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("appliedCovenantId") not in allowed_ids:
            continue
        status = str(r.get("recommendedStatus", "")).upper()
        if status not in STATUSES:
            continue
        conf = r.get("confidence")
        conf = max(0.0, min(100.0, float(conf))) if isinstance(conf, (int, float)) else None
        rationale = str(r.get("rationale") or "")[:1000]
        out.append({"appliedCovenantId": r["appliedCovenantId"], "recommendedStatus": status,
                    "rationale": f"{rationale} {ADVISORY}".strip(), "confidence": conf})
    return out


def azure_completion(model: str, temperature: float | None = None, timeout: float | None = None,
                     max_tokens: int | None = None) -> Completion:
    import os
    from urllib.parse import urlsplit

    from openai import AzureOpenAI, BadRequestError

    from dps.agent import usage
    from dps.ops import observability

    base = os.getenv("AZURE_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_API_BASE", "")
    endpoint = urlsplit(base)
    deployment = model.split("/", 1)[1]
    limit_parameter = "max_completion_tokens"
    api_key = os.getenv("AZURE_API_KEY")
    token_provider = None
    if not api_key:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        scope = os.getenv("AZURE_CREDENTIAL_SCOPES", "https://cognitiveservices.azure.com/.default")
        token_provider = get_bearer_token_provider(DefaultAzureCredential(), *scope.split(","))

    def call(messages: list[dict]) -> str:
        nonlocal limit_parameter
        options = {} if temperature is None else {"temperature": temperature}
        if max_tokens:
            options[limit_parameter] = max_tokens
        timer = observability.Timer()
        try:
            # The Azure client sets the deployment URL from the vetted resource and route's deployment.
            with AzureOpenAI(azure_endpoint=f"{endpoint.scheme}://{endpoint.netloc}",
                             api_version=os.getenv("AZURE_API_VERSION") or "2024-06-01",
                             api_key=api_key, azure_ad_token_provider=token_provider, timeout=timeout,
                             max_retries=0) as client:
                try:
                    response = client.chat.completions.create(model=deployment, messages=messages, **options)
                except BadRequestError as ex:
                    if ("max_completion_tokens" not in options or ex.code != "unsupported_parameter"
                            or "max_completion_tokens" not in str(ex)):
                        raise
                    # Older Azure deployments can still require the legacy field.
                    options["max_tokens"] = options.pop("max_completion_tokens")
                    response = client.chat.completions.create(model=deployment, messages=messages, **options)
                    limit_parameter = "max_tokens"
        except Exception as ex:
            usage.note(model, 0, 0, seconds=timer.seconds, messages=messages,
                       error=f"{type(ex).__name__}: {str(ex)[:300]}")
            raise
        text = (response.choices[0].message.content or "") if response.choices else ""
        spent = response.usage
        details = spent.prompt_tokens_details if spent else None
        usage.note(model, spent.prompt_tokens if spent else 0, spent.completion_tokens if spent else 0,
                   seconds=timer.seconds, messages=messages, answer=text,
                   cached_tokens=(details.cached_tokens or 0) if details else 0)
        return text

    return call


def describe_unit(unit: str | None) -> str:
    """"INR_CR" is INR crore, "LKR_MN" LKR million; anything else as written."""
    from dps.money import currency_of

    code = currency_of(unit)
    return unit or "" if code is None else ("INR crore" if unit == "INR_CR" else f"{code} million")


class Advisor:
    def __init__(self, completion: Completion | None = None) -> None:
        self._completion = completion

    @property
    def completion(self) -> Completion | None:
        return self._completion

    @property
    def ai_enabled(self) -> bool:
        return self._completion is not None

    def suggest(self, cp_id: str, covenants: list[dict]) -> list[dict]:
        """C3: one suggestion per covenant it can say something about."""
        covenants = [c for c in covenants if isinstance(c, dict) and c.get("appliedCovenantId")]
        if not covenants:
            return []
        if self._completion is None:
            return deterministic(covenants)
        facts = [{k: c.get(k) for k in ("appliedCovenantId", "covenantName", "conditionExpression",
                                         "computedValue", "latestResult")} for c in covenants]
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"cpId": cp_id, "covenants": facts})}]
        try:
            return _parse(self._completion(messages), {c["appliedCovenantId"] for c in covenants})
        except Exception as ex:  # model down, rate-limited or off-format: fall back, never fail Core
            log.warning("AI suggestions fell back to deterministic: %s", type(ex).__name__)
            return deterministic(covenants)

    def narrative(self, values: list[dict]) -> dict:
        """O8: a short reading of one statement's line items, for the reviewer (advisory)."""
        by_key = {v["key"]: v["value"] for v in values}
        lines = []
        if "EBITDA" in by_key and by_key.get("INTEREST_PAID"):
            lines.append(f"EBITDA covers interest {by_key['EBITDA'] / by_key['INTEREST_PAID']:.2f} times.")
        if "TOTAL_DEBT" in by_key and by_key.get("EBITDA"):
            lines.append(f"Debt is {by_key['TOTAL_DEBT'] / by_key['EBITDA']:.2f} times EBITDA.")
        if "TOTAL_DEBT" in by_key and by_key.get("NET_WORTH"):
            lines.append(f"Debt to net worth is {by_key['TOTAL_DEBT'] / by_key['NET_WORTH']:.2f}.")
        if "CURRENT_ASSETS" in by_key and by_key.get("CURRENT_LIABILITIES"):
            lines.append(f"Current ratio is {by_key['CURRENT_ASSETS'] / by_key['CURRENT_LIABILITIES']:.2f}.")
        text = " ".join(lines) or "Not enough line items to comment on."
        method = "RULES"
        if self._completion is not None and values:
            try:
                # Money for any country: the figures are in their own money unit (INR crore, LKR million ...).
                units = sorted({describe_unit(v.get("unit")) for v in values if v.get("unit")}) or ["INR crore"]
                text = self._completion([
                    {"role": "system", "content": f"Summarise these financial statement figures ({', '.join(units)}) "
                                                  "for a credit officer in at most four sentences. Use only "
                                                  "the numbers given."},
                    {"role": "user", "content": json.dumps(by_key)},
                ]).strip()[:2000]
                method = "AI"
            except Exception as ex:
                log.warning("Narrative fell back to rules: %s", type(ex).__name__)
        return {"text": text, "method": method, "advisory": ADVISORY}

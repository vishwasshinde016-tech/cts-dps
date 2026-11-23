"""The architect's agent design: one CrewAI Document Agent per job with the job's skill, tools and prompt instructions,
every call through the model gateway. CI never calls a model: the gateway's provider is scripted here."""

from __future__ import annotations

import json
import os

import pytest
from test_phase2_ingest import text_pdf
from test_phase3_analyze import model_of
from test_phase4_skills import AMENDMENT, SANCTION, Tools, fake_job

from dps.agent import document_agent, vision
from dps.agent.document_agent import (
    CovenantLibraryTool,
    FindDefinitionTool,
    GatewayLLM,
    LookupIdentifierTool,
    PageTextTool,
    SearchTextTool,
    ToolContext,
    ValidateFormulaTool,
    build_tools,
)
from dps.agent.gateway import Gateway, LlmDisabled, Route, Routes, from_settings
from dps.agent.guard import _masked
from dps.integrations.spring_tools import FormulaCheck, LookupMatch
from dps.settings import Settings, endpoint_problem
from dps.skills import load
from dps.skills import runner as skills
from dps.skills.common import agent_proposals

AMOUNT_QUOTE = "Sanctioned limit: Rs. 25.50 crore"
AMOUNT = {"key": "SANCTIONED_AMOUNT", "text": AMOUNT_QUOTE, "amount": 255000000.0, "currency": "INR", "date": None}


def text_of(messages) -> str:
    if isinstance(messages, str):
        return messages
    return "\n".join(str(m.get("content")) for m in messages)


class Script:
    """A provider for Gateway: answers by route and skill, records every call. ``final`` maps (route, skill) to the
    agent's Final Answer; ``tool`` maps (route, skill) to one tool call made before it."""

    def __init__(self, final: dict, tool: dict | None = None, error: Exception | None = None):
        self.final, self.tool, self.error = final, tool or {}, error
        self.calls: list[tuple[str, str]] = []

    def skill(self, text: str) -> str:
        for name in ("schedule_terms", "amendment_diff", "certificate", "covenants", "statement_lines", "sanction"):
            if f"({name} skill)" in text:
                return name
        return "plain"

    def __call__(self, route: Route):
        def complete(messages):
            text = text_of(messages)
            key = (route.name, self.skill(text))
            self.calls.append(key)
            if self.error is not None:
                raise self.error
            if key in self.tool and self.calls.count(key) == 1:  # the first call of this skill asks for a tool
                name, args = self.tool[key]
                return f"Thought: I need the page.\nAction: {name}\nAction Input: {json.dumps(args)}"
            answer = self.final.get(key, self.final.get((route.name, "*"), {"items": []}))
            body = answer if isinstance(answer, str) else json.dumps(answer)
            return body if key[1] == "plain" else f"Thought: I have the answer.\nFinal Answer: {body}"

        return complete


def gateway(script: Script, routes=("small", "strong", "vision")) -> Gateway:
    return Gateway(Routes({n: Route(n, "fake", f"dep-{n}", retries=0) for n in routes}), script, sleep=lambda _s: None)


class Spring:
    def __init__(self, down=False):
        self.down, self.calls = down, []

    def covenant_library(self):
        self.calls.append("library")
        if self.down:
            return [], False
        return [{"id": "CM-1", "name": "DSCR", "aliases": ["Debt Service Coverage Ratio"],
                 "expression": "DSCR >= 1.2"}], True

    def validate_formulas(self, items, correlation_id=None):
        self.calls.append("validate")
        return {i: FormulaCheck(i, not self.down, None if self.down else ("bad" not in e),
                                () if "bad" not in e else ({"code": "PARSE", "message": "unknown token"},))
                for i, e in items}

    def lookup_identifiers(self, items, context=None, correlation_id=None):
        self.calls.append("lookup")
        if any(k not in ("PAN", "CIN") for _, k, _ in items):
            raise ValueError("kind")
        return {i: LookupMatch(i, not self.down, "EXACT", ({"entityType": "BORROWER", "entityId": "B-1"},))
                for i, _, _ in items}


# ---------------------------------------------------------------- the agent and its tools


def test_agent_reads_with_its_tools_and_answers_grounded():
    script = Script({("strong", "sanction"): {"items": [{"name": "SANCTIONED_AMOUNT", "page": 1,
                                                         "quote": AMOUNT_QUOTE, "value": AMOUNT}]}},
                    tool={("strong", "sanction"): ("page_text", {"page": 1})})
    pack, model = load("sanction"), model_of(SANCTION)
    answer, ctx = document_agent.read(pack, gateway(script), model["pages"], "Read the sanction letter.")
    assert answer["items"][0]["quote"] == AMOUNT_QUOTE
    assert ctx.calls == ["page_text"] and script.calls == [("strong", "sanction"), ("strong", "sanction")]


def test_agent_is_fresh_per_job_with_only_the_skill_tools():
    ctx = ToolContext([], None)
    a = document_agent.make_agent(gateway(Script({})), "strong", "certificate", load("certificate").tools, ctx)
    b = document_agent.make_agent(gateway(Script({})), "strong", "certificate", load("certificate").tools, ctx)
    assert a is not b and a.allow_delegation is False and "certificate skill" in a.role
    assert sorted(t.name for t in a.tools) == ["lookup_identifier", "page_text", "search_text"]
    assert build_tools(["page_text", "rm_rf", "shell"], ctx)[0].name == "page_text"  # unknown names ignored
    assert len(build_tools(["page_text", "rm_rf", "shell"], ctx)) == 1


def test_page_tools_see_the_document_and_respect_the_limit():
    pages = [{"no": 2, "text": 'Definitions\n"EBITDA" means earnings before interest.\n\nOther text'},
             {"no": 5, "text": "The Borrower shall maintain DSCR of 1.2"}]
    ctx = ToolContext(pages, None, limit=4)
    assert "not in the document" in PageTextTool(ctx)._run(1) and "2 to 5" in PageTextTool(ctx)._run(9)
    assert SearchTextTool(ctx)._run("dscr").startswith("page 5:")
    assert FindDefinitionTool(ctx)._run("EBITDA").startswith('page 2: "EBITDA" means')
    assert "Tool limit of 4" in FindDefinitionTool(ctx)._run("EBITDA")  # fifth call
    fresh = ToolContext([{"no": 1, "text": ""}], None)
    assert "no readable text" in PageTextTool(fresh)._run(1)
    assert "No line contains" in SearchTextTool(fresh)._run("missing")
    assert "No definition" in FindDefinitionTool(fresh)._run("ICR")


def test_spring_tools_are_java_rules_and_degrade_when_spring_is_down():
    spring = Spring()
    ctx = ToolContext([], spring, "job-1", {"cpIds": ["CP1"]}, limit=10)
    assert json.loads(CovenantLibraryTool(ctx)._run("Debt Service Coverage Ratio"))["id"] == "CM-1"
    assert "No library covenant" in CovenantLibraryTool(ctx)._run("ICR")
    assert ValidateFormulaTool(ctx)._run("DSCR >= 1.2") == "valid"
    assert ValidateFormulaTool(ctx)._run("bad >=").startswith("invalid: unknown token")
    assert LookupIdentifierTool(ctx)._run("pan", "AAPFU0939F").startswith("EXACT")
    assert "Not a supported" in LookupIdentifierTool(ctx)._run("PASSPORT", "X123")
    down = ToolContext([], Spring(down=True), limit=10)
    assert "not reachable" in CovenantLibraryTool(down)._run("DSCR")
    assert "not reachable" in ValidateFormulaTool(down)._run("DSCR >= 1")
    assert "not reachable" in LookupIdentifierTool(down)._run("PAN", "AAPFU0939F")
    none = ToolContext([], None, limit=10)
    assert "not available" in CovenantLibraryTool(none)._run("DSCR")
    assert "not available" in ValidateFormulaTool(none)._run("DSCR >= 1")
    assert "not available" in LookupIdentifierTool(none)._run("PAN", "AAPFU0939F")


def test_gateway_llm_applies_stop_words_and_reports_capabilities():
    script = Script({("strong", "plain"): "Answer\nObservation: invented"})
    llm = GatewayLLM(gateway(script), "strong", stop=["\nObservation:"])
    assert llm.call("hello") == "Answer" and llm.supports_function_calling() is False and llm.supports_stop_words()
    assert GatewayLLM(gateway(script), "strong").call([{"role": "user", "content": "x"}]).endswith("invented")


def test_wrong_answer_is_repaired_once_then_dropped_as_ungrounded():
    pack, pages = load("sanction"), model_of(SANCTION)["pages"]
    bad = {"items": [{"name": "SANCTIONED_AMOUNT", "page": 1, "quote": "not on the page at all", "value": AMOUNT}]}
    notes: list = []
    assert agent_proposals(pack, gateway(Script({("strong", "sanction"): bad, ("strong", "plain"): bad})), pages,
                           notes=notes) is None
    assert notes[0]["code"] == "AI_UNGROUNDED"
    good = {"items": [{"name": "SANCTIONED_AMOUNT", "page": 1, "quote": AMOUNT_QUOTE, "value": AMOUNT}]}
    fixed = agent_proposals(pack, gateway(Script({("strong", "sanction"): bad, ("strong", "plain"): good})), pages)
    assert fixed[0]["quote"] == AMOUNT_QUOTE
    assert agent_proposals(pack, gateway(Script({("strong", "sanction"): "not json", ("strong", "plain"): "{}"})),
                           pages, notes=(n2 := [])) is None and n2[0]["code"] == "MODEL_OUTPUT_INVALID"


def test_model_down_or_budget_used_degrades_to_rules():
    pack, pages = load("sanction"), model_of(SANCTION)["pages"]
    notes: list = []
    assert agent_proposals(pack, gateway(Script({}, error=RuntimeError("401 unauthorized"))), pages,
                           notes=notes) is None
    assert notes[0]["code"] == "AI_UNAVAILABLE"
    gw = gateway(Script({}))
    gw.begin_job(1)
    gw._budget().used = 5
    notes = []
    assert agent_proposals(pack, gw, pages, notes=notes) is None and notes[0]["code"] == "AI_BUDGET_EXCEEDED"


def test_agent_runtime_problem_falls_back_to_one_plain_call(monkeypatch):
    def broken(*_a, **_k):
        raise TimeoutError("agent took too long")

    monkeypatch.setattr(document_agent, "read", broken)
    good = {"items": [{"name": "SANCTIONED_AMOUNT", "page": 1, "quote": AMOUNT_QUOTE, "value": AMOUNT}]}
    script = Script({("strong", "plain"): good})
    out = agent_proposals(load("sanction"), gateway(script), model_of(SANCTION)["pages"])
    assert out[0]["name"] == "SANCTIONED_AMOUNT" and script.calls == [("strong", "plain")]


def test_completion_for_the_agreement_and_statement_readers():
    script = Script({("strong", "covenants"): '{"covenants": []}'})
    complete = document_agent.completion(gateway(script), "strong", "covenants", ("covenant_library",), Spring())
    assert complete([{"role": "system", "content": "ignored"}, {"role": "user", "content": "List covenants"}]) \
        == '{"covenants": []}'
    assert script.calls == [("strong", "covenants")]


# ---------------------------------------------------------------- the skills with the agent (Miro AI functions)


def test_sanction_job_rules_and_agent_agree_and_schedule_skill_runs():
    final = {("strong", "sanction"): {"items": [{"name": "SANCTIONED_AMOUNT", "page": 1, "quote": AMOUNT_QUOTE,
                                                 "value": AMOUNT}]},
             ("strong", "schedule_terms"): {"items": []}}
    script = Script(final, tool={("strong", "sanction"): ("search_text", {"query": "sanctioned limit"})})
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), gateway(script), Tools())
    by = {f["name"]: f for f in result["fields"]}
    assert by["SANCTIONED_AMOUNT"]["method"] == "RULES+AI_CONFIRMED"
    assert by["SANCTIONED_AMOUNT"]["signals"]["agree"] == 1.0
    assert ("strong", "schedule_terms") in script.calls
    assert not [c for c in script.calls if c[0] == "challenger"]  # no second-model re-read
    assert result["usage"]["model_calls"] >= 2 and not [w for w in result["warnings"] if w["code"] == "AI_UNAVAILABLE"]


def test_covenants_match_by_metric_and_an_agent_only_covenant_goes_to_a_person():
    page = ("FINANCIAL COVENANTS\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 "
            "times, tested annually.\nThe Borrower shall maintain a Current Ratio of not less than 1.10 times.")
    model = model_of(page)
    rules = skills.load("covenants").rules(model["pages"], {})
    dscr = next(f for f in rules if (f["value"].get("metric") or "").upper() == "DSCR")
    agent = {"items": [
        {"name": "Debt Service Coverage Ratio", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.20",
         "value": {"metric": "DSCR", "operator": dscr["value"]["operator"], "threshold": dscr["value"]["threshold"]}},
        {"name": "Fixed Asset Coverage", "page": 1, "quote": "Current Ratio of not less than 1.10 times",
         "value": {"metric": "FACR", "operator": ">=", "threshold": 1.1}}]}
    proposals = agent["items"]
    voted = skills._vote(load("covenants"), [dict(f, issues=list(f["issues"])) for f in rules], proposals)
    assert next(f for f in voted if f["value"].get("metric") == "DSCR")["method"] == "RULES+AI_CONFIRMED"
    extra = skills._agent_only(load("covenants"), model["pages"], voted, proposals)
    assert [(f["group"], f["name"], f["status"]) for f in extra] == [
        ("POSSIBLE_COVENANT", "Fixed Asset Coverage", "NEEDS_REVIEW")]
    assert extra[0]["issues"][0]["code"] == "AI_ONLY" and extra[0]["value"]["metric"] == "FACR"


def test_amendment_agent_explains_each_change():
    model = model_of(AMENDMENT)
    rules = load("amendment_diff").rules(model["pages"], {"base": []})
    change = next(f for f in rules if (f["value"].get("change") or {}).get("type"))
    proposal = {"name": change["name"], "page": change["source"]["page"], "quote": change["source"]["quote"],
                "value": {**change["value"], "change": {**change["value"]["change"],
                                                        "explanation": "DSCR floor falls from 1.20 to 1.10 (21.2)"}}}
    voted = skills._vote(load("amendment_diff"), rules, [proposal])
    skills._explain_changes(voted, [proposal])
    assert change["value"]["change"]["explanation"].startswith("DSCR floor") and \
        change["value"]["change"]["explanationBy"] == "AI"


def test_scanned_pages_are_read_from_images_and_always_reviewed():
    model = model_of("", SANCTION)
    model["pages"][0]["quality"] = 0.1  # OCR could not read page 1
    seen = {"items": [{"name": "VALIDITY", "page": 1, "quote": "valid for 90 days",
                       "value": {"key": "VALIDITY", "text": "valid for 90 days"}}]}
    script = Script({("vision", "plain"): seen})
    token = vision.page_images.set(lambda no: b"\x89PNG fake" if no == 1 else None)
    try:
        result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model, gateway(script), None)
    finally:
        vision.page_images.reset(token)
    f = next(x for x in result["fields"] if x["method"] == "AI_VISION")
    assert f["status"] == "NEEDS_REVIEW" and f["issues"][0]["code"] == "READ_FROM_IMAGE"
    assert ("vision", "plain") in script.calls and result["status"] == "NEEDS_REVIEW"
    assert "vision model" in next(w for w in result["warnings"] if w["code"] == "UNREADABLE_PAGES")["message"]


def test_vision_needs_a_renderer_a_route_and_allowed_pages():
    pack = load("sanction")
    assert vision.read_pages(pack, gateway(Script({})), [1]) is None  # no renderer set
    token = vision.page_images.set(lambda no: None)
    try:
        assert vision.read_pages(pack, gateway(Script({})), [1]) is None  # nothing rendered
        assert vision.read_pages(pack, gateway(Script({}), routes=("strong",)), [1]) is None  # no vision route
    finally:
        vision.page_images.reset(token)
    token = vision.page_images.set(lambda no: b"png")
    try:
        notes: list = []
        wrong = {"items": [{"name": "VALIDITY", "page": 7, "quote": "x", "value": {"key": "VALIDITY"}}]}
        assert vision.read_pages(pack, gateway(Script({("vision", "plain"): wrong})), [1], notes) is None
        assert notes[0]["code"] == "MODEL_OUTPUT_INVALID"
    finally:
        vision.page_images.reset(token)


def test_renderer_draws_pdf_pages_only():
    render = vision.renderer(lambda: text_pdf(["Page one", "Page two"]))
    png = render(2)
    assert png.startswith(b"\x89PNG") and render(3) is None and render(0) is None
    assert vision.renderer(lambda: b"PK\x03\x04 a spreadsheet")(1) is None
    message = vision.image_messages("read", [(2, png)])[0]["content"]
    assert message[0] == {"type": "text", "text": "read"} and message[2]["image_url"]["url"].startswith(
        "data:image/png;base64,")


def test_image_only_document_is_classified_by_vision_and_sent_to_a_person():
    from dps.analyze.classify import classify

    model = model_of("", "")
    answer = {"type": "SANCTION_LETTER", "basis": None, "periodType": None,
              "evidence": [{"page": 1, "quote": "SANCTION LETTER"}]}
    token = vision.page_images.set(lambda no: b"png")
    try:
        c = classify(model, None, gateway(Script({("vision", "plain"): answer})))
    finally:
        vision.page_images.reset(token)
    assert (c.type, c.method, c.uncertain) == ("SANCTION_LETTER", "VISION", True)


# ---------------------------------------------------------------- safety: allowed endpoint, masking, telemetry


def test_model_calls_go_only_to_allowed_hosts():
    s = Settings(llm_enabled=True, ai_model="azure/gpt-4.1", ai_allowed_hosts="bank-ai.openai.azure.com")
    assert endpoint_problem(s, {"AZURE_API_BASE": "https://bank-ai.openai.azure.com/"}) is None
    assert "not on DPS_AI_ALLOWED_HOSTS" in endpoint_problem(s, {"AZURE_API_BASE": "https://evil.example.com"})
    assert "not on DPS_AI_ALLOWED_HOSTS" in endpoint_problem(
        s, {"AZURE_API_BASE": "https://bank-ai.openai.azure.com", "OPENAI_BASE_URL": "https://api.openai.com/v1"})
    assert "no model base URL" in endpoint_problem(s, {})
    with pytest.raises(LlmDisabled, match="evil.example.com"):
        from_settings(s, env={"AZURE_API_BASE": "https://evil.example.com"})
    with pytest.raises(LlmDisabled, match="off"):
        from_settings(Settings(llm_enabled=False, ai_model="azure/gpt-4.1"))


def test_only_azure_models_may_be_called_even_with_an_allowed_host(tmp_path, monkeypatch):
    """Phase 3 fix 4 (23 Nov 2026): an "openai/" or "anthropic/" model goes to that provider's public host, which the
    host check never sees; such a model or route is refused before any call."""
    from dps.agent import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_live_gateway", lambda s, f=None: "live")
    env = {"AZURE_API_BASE": "https://bank-ai.openai.azure.com/"}
    allowed = "bank-ai.openai.azure.com"
    for model in ("openai/gpt-4.1", "anthropic/claude-x", "gpt-4.1", "ollama/llama3"):
        with pytest.raises(LlmDisabled, match="only azure/"):
            from_settings(Settings(llm_enabled=True, ai_model=model, ai_allowed_hosts=allowed), env=env)
    assert from_settings(Settings(llm_enabled=True, ai_model="Azure/gpt-4.1", ai_allowed_hosts=allowed),
                         env=env) == "live"
    routes = tmp_path / "routes.yaml"
    routes.write_text("routes:\n  small: {deployment: azure/gpt-4.1-mini}\n  strong: {deployment: openai/gpt-4.1}\n",
                      encoding="utf-8")
    with pytest.raises(LlmDisabled, match="route strong"):
        from_settings(Settings(llm_enabled=True, ai_routes_file=str(routes), ai_allowed_hosts=allowed), env=env)
    routes.write_text("routes:\n  small: {deployment: azure/gpt-4.1-mini}\n  vision: {deployment: ''}\n",
                      encoding="utf-8")
    assert from_settings(Settings(llm_enabled=True, ai_routes_file=str(routes), ai_allowed_hosts=allowed),
                         env=env) == "live"  # a route left empty is simply not used


def test_personal_data_is_masked_in_multimodal_messages_and_no_text_is_kept_by_default():
    m = _masked({"role": "user", "content": [{"type": "text", "text": "PAN AAPFU0939F"},
                                             {"type": "image_url", "image_url": {"url": "data:x"}}]})
    assert "AAPFU0939F" not in m["content"][0]["text"] and m["content"][1]["image_url"]["url"] == "data:x"
    assert _masked({"role": "user", "content": None}) == {"role": "user", "content": None}
    assert Settings().ai_trace_text is False


def test_crewai_telemetry_is_off():
    import dps  # noqa: F401 - the package sets the flags on import

    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true" and os.environ["CREWAI_DISABLE_TRACKING"] == "true"

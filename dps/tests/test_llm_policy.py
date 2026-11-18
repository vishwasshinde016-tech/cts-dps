"""LLM policy (17 Nov 2026): the Document Agent runs on the bank's Azure OpenAI deployments only when it is switched
on with a model and the model host is on the allowed list. Off by default: CI and tests never call a model. Rules
read first; what neither the rules nor the agent can read with certainty goes to a person."""

from __future__ import annotations

import pytest
from conftest import h

from dps.agent import gateway as gateway_module
from dps.settings import Settings, llm_allowed


def test_llm_is_off_by_default_and_needs_a_model():
    assert Settings().llm_enabled is False
    assert llm_allowed(Settings(ai_model="azure/gpt-4.1")) is False  # switch off
    assert llm_allowed(Settings(llm_enabled=True)) is False  # no model configured
    assert llm_allowed(Settings(llm_enabled=True, ai_model="azure/gpt-4.1")) is True
    with pytest.raises(gateway_module.LlmDisabled):
        gateway_module.from_settings(Settings(ai_model="azure/gpt-4.1", ai_routes_file="routes.yaml"))


def test_workers_build_no_model_while_the_switch_is_off(app, settings, monkeypatch):
    from dps import worker

    def refuse(*_a, **_k):
        raise AssertionError("a model gateway was built")

    monkeypatch.setattr(gateway_module, "from_settings", refuse)
    settings.ai_model = "azure/gpt-4.1"
    worker.build_analyzer(settings, app.state.db)
    worker.build_pack_runner(settings, app.state.db)


def test_workers_use_the_gateway_when_switched_on(app, settings, monkeypatch):
    from dps import worker

    built = []
    monkeypatch.setattr(gateway_module, "from_settings", lambda s, f=None: built.append(s) or object())
    settings.llm_enabled, settings.ai_model = True, "azure/gpt-4.1"
    worker.build_analyzer(settings, app.state.db)
    worker.build_pack_runner(settings, app.state.db)
    assert len(built) == 2


def test_api_reports_whether_the_model_is_on(settings, checker):
    from fastapi.testclient import TestClient

    from dps.main import create_app

    settings.ai_model = "azure/gpt-4.1"
    app = create_app(settings, cp_checker=checker, create_schema=True)
    assert app.state.advisor.completion is None
    caps = TestClient(app).get("/dps/v1/capabilities", headers=h("cts", "SERVICE")).json()
    assert caps["llmEnabled"] is False


def test_without_a_model_the_rules_read_and_unread_values_go_to_review():
    from test_phase3_analyze import model_of
    from test_phase4_skills import fake_job

    from dps.skills import load
    from dps.skills import runner as skills

    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of("Dear Sir, a letter with no terms."),
                        None, None)
    assert result["status"] == "NEEDS_REVIEW" and "NOTHING_FOUND" in result["document"]["reasons"]
    assert result["usage"] is None and result["pipeline"]["models"] is None


def test_unreadable_scanned_pages_go_to_a_person():
    from test_phase3_analyze import model_of
    from test_phase4_skills import fake_job

    from dps.skills import load
    from dps.skills import runner as skills

    model = model_of("Sanctioned limit: Rs. 5 crore", "~~ ## ..")
    model["pages"][1].update(quality=0.1, method="OCR", sourceMethod="OCR")
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model, None, None)
    assert result["status"] == "NEEDS_REVIEW" and "UNREADABLE_PAGES" in result["document"]["reasons"]
    assert "2" in next(w["message"] for w in result["warnings"] if w["code"] == "UNREADABLE_PAGES")

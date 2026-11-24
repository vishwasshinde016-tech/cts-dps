import json

import pytest
from conftest import h
from test_jobapi import request_body, submit

from dps.agent.gateway import AiRoutePaused, _live_gateway
from dps.models import Run

ADMIN = h("operator", "ADMIN")
SUPPORT = h("support", "SUPPORT")


def test_pausing_a_route_reaches_an_existing_worker_and_keeps_other_routes_open(client, app, settings, monkeypatch):
    settings.ai_model = "azure/test-deployment"
    gateway = _live_gateway(settings, app.state.db.session_factory)
    request = {"paused": True, "reason": "UAT rehearsal"}
    path = "/dps/v1/ops/ai-routes/strong"
    calls = []

    def complete(messages):
        calls.append(messages)
        return "first batch complete"

    monkeypatch.setattr(gateway, "_completion", lambda route: complete)
    assert gateway.available("strong")
    gateway.begin_job()
    messages = [{"role": "user", "content": "test"}]
    assert gateway.call("strong", messages) == "first batch complete"
    assert client.put(path, json=request, headers=SUPPORT).status_code == 403
    assert client.put(path, json=request, headers=ADMIN).json() == {"route": "strong", "paused": True}
    with pytest.raises(AiRoutePaused):
        gateway.call("strong", messages)
    assert gateway.end_job().calls == 1
    assert calls == [messages]
    assert not gateway.available("strong") and gateway.available("small")
    assert client.put(path, json={**request, "paused": False}, headers=ADMIN).status_code == 200
    assert gateway.available("strong")


def test_affected_results_excludes_superseded_outputs(client, app):
    job = submit(client, request_body(app)).json()["jobId"]
    output = {"pipeline": {"version": "tested-v1", "models": {"strong": "azure/test-deployment"}}}
    with app.state.db.session_factory() as db:
        db.add(Run(job_id=job, run_no=1, status="DONE", superseded_by="new-result", result_json=json.dumps(output)))
        db.add(Run(job_id=job, run_no=2, status="DONE", result_json=json.dumps({**output, "runId": "new-result"})))
        db.commit()
    result = client.get("/dps/v1/ops/affected-results", headers=SUPPORT,
                        params={"route": "strong", "model": "azure/test-deployment", "pipelineVersion": "tested-v1"})
    assert result.status_code == 200
    assert [row["runId"] for row in result.json()["items"]] == ["new-result"]

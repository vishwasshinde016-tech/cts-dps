"""Spring (Java) business rules called as batched tool calls: batching, fallback, retries, breaker, degrade."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from dps.integrations.spring import SpringClient
from dps.integrations.spring_tools import MAX_BATCH, UNAVAILABLE, CircuitBreaker, SpringTools

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "spring-tools.yaml"


def make(handler, **kw) -> tuple[SpringTools, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    client = httpx.Client(base_url="http://spring", transport=httpx.MockTransport(wrapped))
    core = SpringClient("http://spring", 5.0, client=client)
    return SpringTools(core, sleep=lambda _s: None, **kw), seen


def test_contract_lists_the_tools_and_the_event_push():
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert set(spec["paths"]) == {"/api/v1/dps-tools/formulas/validate", "/api/v1/dps-tools/lookups",
                                  "/api/v1/dps-tools/covenant-library", "/api/v1/dps-tools/events",
                                  # the statement reader's reads (WP7: moved here from the old Core contract)
                                  "/api/v1/credit-proposals/{cpId}/currency", "/api/v1/credit-proposals/{cpId}/profile",
                                  "/api/v1/settings/countries", "/api/v1/fx/convert"}


def test_formulas_go_in_one_batch_call_with_correlation_id():
    def handler(req):
        items = json.loads(req.content)["items"]
        return httpx.Response(200, json={"items": [
            {"id": i["id"], "valid": i["expression"] != "bad", "errors": [] if i["expression"] != "bad"
             else [{"code": "PARSE", "message": "x"}]} for i in items]})

    tools, seen = make(handler)
    out = tools.validate_formulas([("f1", "DSCR >= 1.2"), ("f2", "bad")], correlation_id="job-1")
    assert len(seen) == 1 and seen[0].headers["X-Correlation-Id"] == "job-1"
    assert out["f1"].valid is True and out["f2"].valid is False and out["f2"].errors[0]["code"] == "PARSE"


def test_large_documents_are_split_at_max_batch():
    def handler(req):
        return httpx.Response(200, json={"items": [{"id": i["id"], "valid": True, "errors": []}
                                                   for i in json.loads(req.content)["items"]]})

    tools, seen = make(handler)
    out = tools.validate_formulas([(f"f{i}", "A > 1") for i in range(MAX_BATCH + 5)])
    assert len(seen) == 2 and len(out) == MAX_BATCH + 5


def test_404_falls_back_to_existing_single_item_endpoint():
    def handler(req):
        if req.url.path == "/api/v1/dps-tools/formulas/validate":
            return httpx.Response(404)
        return httpx.Response(200, json={"valid": True, "errors": []})

    tools, seen = make(handler)
    out = tools.validate_formulas([("a", "X > 1"), ("b", "Y > 2")])
    assert all(c.available and c.valid for c in out.values())
    tools.validate_formulas([("c", "Z > 3")])  # remembers: no second try of the batch path
    assert [r.url.path for r in seen].count("/api/v1/dps-tools/formulas/validate") == 1


def test_retries_then_degrades_to_review_without_raising():
    tools, seen = make(lambda req: httpx.Response(503), retries=2)
    out = tools.validate_formulas([("f1", "A > 1")])
    assert len(seen) == 3
    assert out["f1"].available is False and out["f1"].code == UNAVAILABLE


def test_retry_succeeds_on_second_attempt():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, json={"items": [{"id": "f1", "valid": True, "errors": []}]})

    tools, _ = make(handler)
    assert tools.validate_formulas([("f1", "A > 1")])["f1"].valid is True


def test_breaker_opens_and_stops_calling_then_half_opens():
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=3, window=60, cooldown=30, clock=lambda: now["t"])
    tools, seen = make(lambda req: httpx.Response(500), breaker=breaker, retries=0)
    for i in range(3):
        tools.validate_formulas([(f"f{i}", "A > 1")])
    assert breaker.state == "OPEN"
    before = len(seen)
    assert tools.validate_formulas([("x", "A > 1")])["x"].code == UNAVAILABLE
    assert len(seen) == before  # no call while open
    now["t"] = 31
    assert breaker.state == "HALF_OPEN"
    tools.validate_formulas([("y", "A > 1")])
    assert len(seen) == before + 1 and breaker.state == "OPEN"  # failed trial re-opens


def test_breaker_forgets_old_failures():
    now = {"t": 0.0}
    b = CircuitBreaker(threshold=2, window=10, cooldown=5, clock=lambda: now["t"])
    b.failure()
    now["t"] = 20
    b.failure()
    assert b.state == "CLOSED"


def test_lookups_batch_with_context_and_degrade():
    def handler(req):
        body = json.loads(req.content)
        assert body["context"] == {"cpIds": ["CP1"]}
        return httpx.Response(200, json={"items": [{"id": "i1", "status": "EXACT",
                                                    "candidates": [{"entityType": "CP", "entityId": "CP1"}]}]})

    tools, _ = make(handler)
    out = tools.lookup_identifiers([("i1", "PAN", "ABCDE1234F")], context={"cpIds": ["CP1"]})
    assert out["i1"].status == "EXACT"
    down, _ = make(lambda req: httpx.Response(404))
    assert down.lookup_identifiers([("i1", "CIN", "U1")])["i1"].code == UNAVAILABLE
    with pytest.raises(ValueError):
        tools.lookup_identifiers([("i1", "PASSPORT", "x")])


def test_covenant_library_uses_etag_and_keeps_cache_when_down():
    state = {"down": False}

    def handler(req):
        if state["down"]:
            return httpx.Response(502)
        if req.headers.get("If-None-Match") == "v1":
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": "v1"}, json={"version": "1", "items": [
            {"id": "C1", "name": "DSCR", "active": True}, {"id": "C2", "name": "old", "active": False}]})

    tools, seen = make(handler, retries=0)
    items, fresh = tools.covenant_library()
    assert fresh and [i["id"] for i in items] == ["C1"]
    items, fresh = tools.covenant_library()
    assert fresh and items and seen[-1].headers["If-None-Match"] == "v1"
    state["down"] = True
    items, fresh = tools.covenant_library()
    assert not fresh and [i["id"] for i in items] == ["C1"]

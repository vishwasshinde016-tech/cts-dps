"""The result check of 22 Nov 2026: an AI-on run of the ex10 agreement broke result.json in 173 places. Every shape
the agent wrote there (and the ledger's own routes) must now end up in the contract's shape."""

from __future__ import annotations

import jsonschema
from test_jobapi import _registry, _schema
from test_phase3_analyze import model_of

from dps.skills import load
from dps.skills import runner as skills
from dps.skills.covenants import ledger
from dps.skills.covenants.obligation import enrich

PAGE = ("FINANCIAL COVENANTS\nThe Borrower shall deliver an Officer's Certificate with each set of financial "
        "statements.\nThe Applicable Margin is determined on the Closing Date and reset five Business Days after a "
        "rating change.\nNo member of the Group may create any Security over its assets.\nThe Borrower shall promptly "
        "notify the Agent of any Change of Control.")


def _field_schema() -> dict:
    result = _schema("result")
    return {**result["$defs"]["field"], "$defs": result["$defs"], "$id": result["$id"]}


def _valid(field: dict) -> list[str]:
    v = jsonschema.Draft202012Validator(_field_schema(), registry=_registry())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(field)]


def test_an_agent_only_covenant_has_every_covenant_value_key():
    """75 fields: the agent sends only the keys it chose; the contract wants metric … expression on every covenant."""
    model = model_of(PAGE)
    proposal = {"name": "Officer's Certificate", "page": 1, "quote": "deliver an Officer's Certificate",
                "value": {"metric": "AFF_OTHER", "frequency": "At delivery of annual and quarterly statements"}}
    [field] = skills._agent_only(load("covenants"), model["pages"], [], [proposal])
    assert field["group"] == "POSSIBLE_COVENANT"
    assert {"metric", "operator", "threshold", "unit", "expression"} <= set(field["value"])
    assert _valid(field) == []


def test_agent_schedule_terms_are_put_in_the_contract_shape():
    """24 + 24 + 15 fields: kind in small letters, free-text frequency, dates null or not a date."""
    model = model_of(PAGE)
    proposals = [
        {"name": "interest_reset", "page": 1, "quote": "determined on the Closing Date",
         "value": {"kind": "interest_reset", "rule": "Determine the Applicable Margin on the Closing Date.",
                   "frequency": "On closing", "dates": ["Closing Date"]}},
        {"name": "INTEREST_RESET", "page": 1, "quote": "reset five Business Days after a rating change",
         "value": {"kind": "Interest Reset", "rule": "five Business Days after a rating change",
                   "frequency": "Upon a rating change", "dates": None}},
        {"name": "REPORTING_DEADLINE", "page": 1, "quote": "with each set of financial statements",
         "value": {"kind": "reporting_deadline", "frequency": "quarterly", "dates": ["2026-06-30", "end of year"]}},
    ]
    fields = skills._agent_only(load("schedule_terms"), model["pages"], [], proposals)
    assert [f["value"]["kind"] for f in fields] == ["INTEREST_RESET", "INTEREST_RESET", "REPORTING_DEADLINE"]
    assert [f["value"]["frequency"] for f in fields] == [None, None, "QUARTERLY"]
    assert [f["value"]["dates"] for f in fields] == [[], [], ["2026-06-30"]]
    assert "On closing" in fields[0]["value"]["rule"] and "Closing Date" in fields[0]["value"]["rule"]
    for f in fields:
        assert _valid(f) == [], f["value"]


def test_an_unknown_schedule_kind_is_not_dropped_and_goes_to_a_person():
    model = model_of(PAGE)
    proposal = {"name": "holiday", "page": 1, "quote": "determined on the Closing Date",
                "value": {"kind": "payment holiday", "rule": "no payment in the first year"}}
    [field] = skills._agent_only(load("schedule_terms"), model["pages"], [], [proposal])
    assert _valid(field) == []
    assert field["status"] == "NEEDS_REVIEW"
    assert any(i["code"] == "AI_ONLY" for i in field["issues"])
    assert "payment holiday" in field["value"]["rule"]


def test_scope_basis_is_only_standalone_or_consolidated():
    """2 fields: the agent put a sentence in basis; scope.basis takes only the two values the contract allows."""
    model = model_of(PAGE)
    proposal = {"name": "Notice of Change of Control", "page": 1, "quote": "promptly notify the Agent of any Change",
                "value": {"metric": "REP_NOTICE_OWNERSHIP", "basis": "Notify Administrative Agent and Lenders"}}
    fields = skills._agent_only(load("covenants"), model["pages"], [], [proposal])
    enrich(fields, model["pages"])
    assert fields[0]["value"]["scope"]["basis"] is None
    assert _valid(fields[0]) == []
    proposal["value"]["basis"] = "consolidated"
    fields = skills._agent_only(load("covenants"), model["pages"], [], [proposal])
    enrich(fields, model["pages"])
    assert fields[0]["value"]["scope"]["basis"] == "CONSOLIDATED"


def test_every_ledger_route_is_in_the_contract():
    """33 entries: the ledger also routes LEXICON, CROSS_DEFAULT and TABLE sentences."""
    entry = _schema("result")["properties"]["document"]["properties"]["coverage"]
    routes = set(_find_route_enum(entry))
    assert {"DUTY", "CUE", "LEXICON", "CROSS_DEFAULT", "TABLE"} <= routes
    found = {e["route"] for e in ledger.candidates(model_of(PAGE)["pages"])}
    assert found <= routes and "LEXICON" in found


def _find_route_enum(node):
    if isinstance(node, dict):
        if "route" in node.get("properties", {}) and "enum" in node["properties"]["route"]:
            return node["properties"]["route"]["enum"]
        for v in node.values():
            hit = _find_route_enum(v)
            if hit:
                return hit
    return None


def test_the_three_answers_the_check_saw_are_in_the_contract():
    """304 (result unchanged), 403 (rerun by a wrong role) and 404 (unknown candidate) were sent but not listed."""
    from test_jobapi import openapi

    paths = openapi()["paths"]
    assert "304" in paths["/dps/v1/jobs/{jobId}/result"]["get"]["responses"]
    assert "403" in paths["/dps/v1/batches/{batchId}/rerun"]["post"]["responses"]
    assert "404" in paths["/dps/v1/ops/golden-candidates/{candidateId}/{decision}"]["post"]["responses"]


def test_a_result_that_follows_the_contract_gets_no_format_warning():
    """The save-time check (22 Nov 2026): a result DPS builds itself passes, so nothing is added."""
    from test_flows import AGREEMENT_PAGES, pages_model

    from dps import results

    result = skills.run(load("covenants"), _job(), pages_model(AGREEMENT_PAGES), None, None, None)
    results.flag_format(result)
    assert results.check_format(result) == []
    assert "RESULT_FORMAT" not in [w["code"] for w in result["warnings"]]


def test_a_result_that_breaks_the_contract_is_saved_with_a_warning(caplog):
    """A shape the contract does not allow is never lost and never silent: the run keeps its findings, gets one
    RESULT_FORMAT warning that says where, and the log names each place."""
    from test_flows import AGREEMENT_PAGES, pages_model

    from dps import results

    result = skills.run(load("covenants"), _job(), pages_model(AGREEMENT_PAGES), None, None, None)
    result["fields"][0]["value"].pop("expression", None)
    result["document"]["coverage"]["ledger"]["entries"][0]["route"] = "SOMETHING_NEW"
    fields = len(result["fields"])
    with caplog.at_level("WARNING"):
        results.flag_format(result)
        results.flag_format(result)  # safe to run twice: one warning
    flagged = [w for w in result["warnings"] if w["code"] == "RESULT_FORMAT"]
    assert len(flagged) == 1 and "2 place(s)" in flagged[0]["message"]
    assert "fields/0/value" in flagged[0]["message"] and "route" in flagged[0]["message"]
    assert len(result["fields"]) == fields
    assert any("RESULT_FORMAT" in r.message for r in caplog.records)


def test_save_run_runs_the_format_check(monkeypatch):
    from dps import results
    from dps.analyze import analysis

    seen = []
    monkeypatch.setattr(results, "flag_format", lambda result: seen.append(result["jobId"]))

    class Db:
        def scalar(self, _):
            return 0

        def add(self, _):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(analysis.jobs, "guard_attempt", lambda db: None)
    analysis.save_run(Db(), _job(), {"jobId": "j-1", "status": "DONE"})
    assert seen == ["j-1"]


def _job():
    from test_phase4_skills import fake_job

    return fake_job("EXTRACT_AGREEMENT")

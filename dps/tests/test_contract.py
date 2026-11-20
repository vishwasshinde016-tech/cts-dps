"""Provider contract tests: real DPS responses must validate against the bank app's compatibility contracts
(contracts/schemas/compat-data-values.json, compat-covenant-status.json; listed in contracts/dps-jobs-api.yaml).

The old Covenexa contract (contracts/dps-api.yaml: C1 recommendations, C3 AI suggestions, documents and terms) was
deleted with the Covenexa routes in the full review (G12, WP7).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import jsonschema
import pytest
from conftest import h

from dps.models import StatementValue

SCHEMAS = Path(__file__).resolve().parents[2] / "contracts" / "schemas"

pytestmark = pytest.mark.skipif(not SCHEMAS.exists(), reason="contracts/ not present")


def schema(name: str, part: str) -> dict:
    whole = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    return {**whole["$defs"][part], "$defs": whole["$defs"]}


def validate(name: str, part: str, body) -> None:
    jsonschema.validate(body, schema(name, part), cls=jsonschema.Draft202012Validator)


def test_data_values_match_the_contract(client, app):
    with app.state.db.session_factory() as s:
        s.add(StatementValue(cp_id="CP-1", key="EBITDA", value=50.0, unit="INR_CR", as_of_date=date(2026, 3, 31),
                             source_type="DOCUMENT"))
        s.commit()
    req = {"cpId": "CP-1", "to": "2026-06-30", "keys": ["EBITDA", "NET_WORTH"]}
    validate("compat-data-values", "request", req)
    body = client.post("/api/data/values", json=req, headers=h("core", "SERVICE")).json()
    assert body["missingKeys"] == ["NET_WORTH"] and body["points"][0]["values"] == {"EBITDA": 50.0}
    validate("compat-data-values", "response", body)


def test_covenant_status_matches_the_contract(client):
    req = {"cpId": "CP-1", "covenants": [{"appliedCovenantId": "a1"}, {"appliedCovenantId": "a2"}]}
    validate("compat-covenant-status", "request", req)
    body = client.post("/api/v1/ai/covenant-status", json=req, headers=h("core", "SERVICE")).json()
    validate("compat-covenant-status", "response", body)


def test_contract_rejects_a_wrong_shape():
    with pytest.raises(jsonschema.ValidationError):
        validate("compat-data-values", "response", {"cpId": "CP-1", "points": [{"values": {}}]})

"""A stand-in for the CTS Spring app's Java tools, for tests only (the Spring team builds the real one).

It answers the three tools in ``contracts/spring-tools.yaml`` (formula validation, master-data lookup, covenant
library) with sample bank data, and checks every request DPS sends and every answer it gives against the contract.
It is plugged into DPS's ``SpringTools`` through an httpx MockTransport, so no network or server is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "spring-tools.yaml"

LIBRARY = [
    {"id": "CM-DSCR", "name": "Debt Service Coverage Ratio", "aliases": ["DSCR"], "metricKeys": ["DSCR"],
     "expression": "DSCR >= 1.2", "assessmentType": "AUTO", "active": True},
    {"id": "CM-ICR", "name": "Interest Coverage Ratio", "aliases": ["ICR"], "metricKeys": ["ICR"],
     "expression": "ICR >= 2", "assessmentType": "AUTO", "active": True},
    {"id": "CM-NP", "name": "Negative Pledge", "aliases": [], "metricKeys": ["NEGATIVE_PLEDGE"], "expression": None,
     "assessmentType": "MANUAL", "active": True},
]
# The bank's master data: identifier -> (entity type, id, name).
MASTER = {
    ("CIN", "L26940MH1990PLC056789"): ("CP", "CP-1", "Shree Polymers Limited"),
    ("BORROWER_NAME", "SHREE POLYMERS LIMITED"): ("CP", "CP-1", "Shree Polymers Limited"),
    ("CIN", "U74999MH2015PTC123456"): ("CP", "CP-9", "Another Borrower Private Limited"),
}


def _operation(spec: dict, path: str, method: str) -> dict:
    return spec["paths"][path][method.lower()]


def _match(spec: dict, path: str) -> str | None:
    """The contract path a request path is for ("/api/v1/credit-proposals/CP-1/currency" is ".../{cpId}/currency")."""
    import re

    for template in spec["paths"]:
        if re.fullmatch(re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(template)), path):
            return template
    return None


def _schema(op: dict, where: str) -> dict:
    if where == "request":
        return op["requestBody"]["content"]["application/json"]["schema"]
    return op["responses"]["200"]["content"]["application/json"]["schema"]


class StubSpring:
    """Call ``client()`` for an httpx client for ``SpringClient``; ``calls`` holds (method, path, headers, body)."""

    def __init__(self, down: bool = False) -> None:
        self.spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.down = down

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://spring", transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, dict(request.headers), body))
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        spec_path = _match(self.spec, path)
        if spec_path is None:
            return httpx.Response(404, json={"error": "no such tool"})
        op = _operation(self.spec, spec_path, method)
        if body is not None:
            jsonschema.validate(body, _schema(op, "request"))  # DPS sent what the contract says
        if path.endswith("/formulas/validate"):
            answer = {"items": [{"id": i["id"], "valid": "??" not in i["expression"],
                                 "errors": [] if "??" not in i["expression"] else
                                 [{"code": "SYNTAX", "message": "unknown token ??"}]} for i in body["items"]]}
        elif path.endswith("/currency"):
            answer = {"currency": "INR"}
        elif path.endswith("/profile"):
            answer = {"currency": "INR", "currencyKnown": True, "fiscalYearEndMonth": 3,
                      "statementEntity": "STANDALONE", "numberStyle": "INDIAN"}
        elif path.endswith("/settings/countries"):
            answer = []
        elif path.endswith("/fx/convert"):
            return httpx.Response(422, json={"code": "FX_RATE_MISSING"})
        elif path.endswith("/lookups"):
            wanted = set((body.get("context") or {}).get("cpIds") or [])
            items = []
            for i in body["items"]:
                hit = MASTER.get((i["kind"], i["value"].upper() if i["kind"] == "BORROWER_NAME" else i["value"]))
                if hit is None:
                    items.append({"id": i["id"], "status": "NONE", "candidates": []})
                    continue
                status = "CONFLICT" if wanted and hit[1] not in wanted else "EXACT"
                items.append({"id": i["id"], "status": status, "candidates": [
                    {"entityType": hit[0], "entityId": hit[1], "name": hit[2], "score": 1.0}]})
            answer = {"items": items}
        else:
            answer = {"version": "lib-2026-11", "items": LIBRARY}
        jsonschema.validate(answer, _schema(op, "response"))  # the stand-in answers what the contract says
        headers = {"ETag": '"lib-2026-11"'} if path.endswith("/covenant-library") else {}
        return httpx.Response(200, json=answer, headers=headers)

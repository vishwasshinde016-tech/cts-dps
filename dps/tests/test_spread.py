"""C6: the full spread of a financial statement, and reviewer corrections that Core then uses."""

from __future__ import annotations

from datetime import date

from conftest import h
from sqlalchemy import false, select
from test_api import add_value, session, upload

from dps.extraction.statement import SECTIONS, extract_values
from dps.models import EventOutbox, StatementValue


def statement(client) -> str:
    return upload(client, type_="FINANCIAL_STATEMENT", name="q3.csv", data=b"EBITDA,640",
                  as_of="2026-09-30").json()["documentId"]


def test_the_reader_maps_the_wider_spread():
    rows = [("Revenue from operations", 4200, None, "B2"), ("Other income", 35, None, "B3"),
            ("Employee benefits expense", 410, None, "B5"), ("Profit before tax", 260, None, "B9"),
            ("Property, plant and equipment", 1900, None, "B20"), ("Total equity and liabilities", 5100, None, "B40"),
            ("Net cash from operating activities", 520, None, "B50"), ("Dividends paid", 40, None, "B55")]
    keys = {v.key for v in extract_values(rows, "Amounts in INR crore").values}
    assert {"REVENUE", "OTHER_INCOME", "EMPLOYEE_COST", "PBT", "FIXED_ASSETS", "TOTAL_EQUITY_AND_LIABILITIES",
            "CFO", "DIVIDENDS_PAID"} <= keys


def test_spread_shows_every_line_with_its_status(client, app):
    doc = statement(client)
    add_value(app, "CP-1", "EBITDA", 640.0, date(2026, 9, 30), doc_id=doc)
    spread = client.get(f"/dps/v1/documents/{doc}/spread", headers=h()).json()
    # Last-12-month lines appear only once they are built (12-month covenants); every other line always does.
    assert len(spread) == sum(len(k) for s, k in SECTIONS.items() if s != "LAST_12_MONTHS")
    by_key = {line["standardKey"]: line for line in spread}
    assert by_key["EBITDA"]["status"] == "MAPPED" and by_key["EBITDA"]["value"] == 640.0
    assert by_key["CFO"]["status"] == "UNMAPPED" and by_key["CFO"]["value"] is None
    assert by_key["CFO"]["section"] == "CASH_FLOW"


def test_a_correction_is_served_to_core_and_announced(client, app):
    doc = statement(client)
    add_value(app, "CP-1", "EBITDA", 64.0, date(2026, 9, 30), doc_id=doc)
    r = client.put(f"/dps/v1/documents/{doc}/spread", headers=h(),
                   json={"lines": [{"standardKey": "EBITDA", "value": 640.0}, {"standardKey": "CFO", "value": 520.0}]})
    assert r.status_code == 200
    by_key = {line["standardKey"]: line for line in r.json()}
    assert by_key["EBITDA"]["status"] == "CONFIRMED" and by_key["EBITDA"]["value"] == 640.0
    assert by_key["CFO"]["value"] == 520.0
    pit = client.post("/dps/v1/values", json={"cpId": "CP-1", "to": "2026-09-30", "keys": ["EBITDA", "CFO"]},
                      headers=h()).json()
    assert pit["points"][0]["values"] == {"EBITDA": 640.0, "CFO": 520.0}
    with session(app) as s:
        assert len(list(s.scalars(select(EventOutbox)))) == 1
        old = s.scalars(select(StatementValue).where(StatementValue.value == 64.0)).one()
        assert old.superseded


def test_corrections_are_checked(client):
    doc = statement(client)
    assert client.put(f"/dps/v1/documents/{doc}/spread", headers=h(),
                      json={"lines": [{"standardKey": "NOPE", "value": 1}]}).json()["code"] == "UNKNOWN_LINE"
    assert client.put(f"/dps/v1/documents/{doc}/spread", headers=h("auditor-1", "AUDITOR"),
                      json={"lines": [{"standardKey": "EBITDA", "value": 1}]}).status_code == 403
    agreement = upload(client).json()["documentId"]
    assert client.get(f"/dps/v1/documents/{agreement}/spread", headers=h()).json()["code"] == "NOT_A_STATEMENT"


def test_a_correction_recomputes_the_derived_totals(client, app):
    # Bug hunt: Total Debt stayed 1,000 after long-term debt was corrected from 700 to 500.
    doc = statement(client)
    with session(app) as s:
        for key, value, derived in [("ST_DEBT", 300.0, False), ("LT_DEBT", 700.0, False), ("CASH", 100.0, False),
                                    ("TOTAL_DEBT", 1000.0, True), ("NET_DEBT", 900.0, True)]:
            s.add(StatementValue(cp_id="CP-1", key=key, value=value, unit="INR_CR", as_of_date=date(2026, 9, 30),
                                 source_type="DOCUMENT", derived=derived, document_id=doc))
        s.commit()
    r = client.put(f"/dps/v1/documents/{doc}/spread", headers=h(), json={"lines": [
        {"standardKey": "LT_DEBT", "value": 500.0}]})
    assert r.status_code == 200
    with session(app) as s:
        now = {v.key: v for v in s.scalars(select(StatementValue).where(StatementValue.cp_id == "CP-1",
                                                                         StatementValue.superseded == false()))}
    assert now["LT_DEBT"].value == 500.0 and now["TOTAL_DEBT"].value == 800.0 and now["NET_DEBT"].value == 700.0
    assert now["TOTAL_DEBT"].derived and "Recomputed" in now["TOTAL_DEBT"].note

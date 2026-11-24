from corrections import accepted_cases


def test_only_accepted_field_with_evidence_becomes_regression_case():
    field = {"name": "DSCR", "group": "FINANCIAL_COVENANT", "value": {"threshold": 1.25},
             "source": {"page": 7, "quote": "Debt service coverage ratio of at least 1.25 times"}}
    approved = {"id": "ok", "jobType": "EXTRACT_AGREEMENT", "status": "ACCEPTED",
                "payload": {"action": "CORRECT", "after": field, "versionId": "d1"}}
    pending = {**approved, "id": "pending", "status": "PENDING"}
    rejected = {**approved, "id": "remove", "payload": {"action": "REJECT", "before": field}}
    result = accepted_cases([pending, approved, rejected])
    assert [(r["candidateId"], r["present"], r["page"]) for r in result] == [("ok", True, 7), ("remove", False, 7)]

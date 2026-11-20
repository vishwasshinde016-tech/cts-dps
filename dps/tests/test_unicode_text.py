"""Text from documents and people keeps every character: the rupee sign, "≥", curly quotes, accents (on SQL Server
a VARCHAR column stored "₹" as "?" and "≥" as "="; CI runs this on SQL Server)."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select

from dps import services
from dps.db import Base, Database
from dps.models import Document, LabelMemory, Run, StatementValue

QUOTE = "“Debt Service Coverage Ratio” ≥ 1.25:1 and Leverage ≤ 3.50:1 on ₹ 500 crore — Société Générale"
LABEL = "Finance costs (₹ in lakhs)"


def test_document_text_keeps_the_rupee_sign_and_comparison_signs(settings):
    db = Database(settings.database_url)
    Base.metadata.create_all(db.engine)
    with db.session_factory() as s:
        doc = Document(type="LOAN_AGREEMENT", file_name="Facility Agreement – ₹ 500 crore.pdf", storage_path="x",
                       sha256="u1", size_bytes=1, uploaded_by="u")
        s.add(doc)
        s.flush()
        s.add(Run(job_id="j-u1", status="DONE", result_json=json.dumps({"quote": QUOTE}, ensure_ascii=False)))
        s.add(StatementValue(cp_id="CP-1", key="NET_WORTH", value=1.0, unit="ABSOLUTE", as_of_date=date(2026, 3, 31),
                             source_type="USER", note="From the “Notes”, ₹ in crore"))
        services.remember_label(s, "CP-1", LABEL, "INTEREST_PAID", doc.id, "rm-anil")
        s.commit()
        doc_id = doc.id
    with db.session_factory() as s:
        assert s.get(Document, doc_id).file_name == "Facility Agreement – ₹ 500 crore.pdf"
        run = s.scalar(select(Run).where(Run.job_id == "j-u1"))
        assert run.result_json == json.dumps({"quote": QUOTE}, ensure_ascii=False)
        assert s.scalar(select(StatementValue.note)) == "From the “Notes”, ₹ in crore"
        assert s.scalar(select(LabelMemory.source_label)) == LABEL
        # confirming the same wording again finds the stored row instead of adding a second one
        services.remember_label(s, "CP-1", LABEL, "INTEREST_PAID", doc_id, "approver-1")
        s.commit()
        assert list(services.label_memory(s, ["CP-1"]).values()) == [("INTEREST_PAID", LABEL)]
        assert len(s.scalars(select(LabelMemory)).all()) == 1

"""Text from documents and people keeps every character: the rupee sign, "≥", curly quotes, accents (on SQL Server
a VARCHAR column stored "₹" as "?" and "≥" as "="; CI runs this on SQL Server)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from dps import services
from dps.db import Base, Database
from dps.models import Document, ExtractedTerm, LabelMemory, LearnedWording, StatementValue

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
        s.add(ExtractedTerm(document_id=doc.id, metric_raw="“DSCR”", name="Minimum “DSCR”", expression="DSCR ≥ 1.25",
                            quote=QUOTE, review_note="Agreed – ₹ figure checked"))
        s.add(StatementValue(cp_id="CP-1", key="NET_WORTH", value=1.0, unit="ABSOLUTE", as_of_date=date(2026, 3, 31),
                             source_type="USER", note="From the “Notes”, ₹ in crore"))
        s.add(LearnedWording(kind="STATEMENT_LINE", phrase=LABEL, key="INTEREST_PAID", evidence=3, active=True,
                             added_by="admin"))
        services.remember_label(s, "CP-1", LABEL, "INTEREST_PAID", doc.id, "rm-anil")
        s.commit()
        doc_id = doc.id
    with db.session_factory() as s:
        assert s.get(Document, doc_id).file_name == "Facility Agreement – ₹ 500 crore.pdf"
        term = s.scalar(select(ExtractedTerm).where(ExtractedTerm.document_id == doc_id))
        assert (term.metric_raw, term.name, term.expression, term.quote, term.review_note) == (
            "“DSCR”", "Minimum “DSCR”", "DSCR ≥ 1.25", QUOTE, "Agreed – ₹ figure checked")
        assert s.scalar(select(StatementValue.note)) == "From the “Notes”, ₹ in crore"
        assert s.scalar(select(LearnedWording).where(LearnedWording.phrase == LABEL)) is not None
        assert s.scalar(select(LabelMemory.source_label)) == LABEL
        # confirming the same wording again finds the stored row instead of adding a second one
        services.remember_label(s, "CP-1", LABEL, "INTEREST_PAID", doc_id, "approver-1")
        s.commit()
        assert list(services.label_memory(s, ["CP-1"]).values()) == [("INTEREST_PAID", LABEL)]
        assert len(s.scalars(select(LabelMemory)).all()) == 1

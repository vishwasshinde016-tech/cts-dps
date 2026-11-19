"""Flow A: Agreement Intake — Parse → Extract → Normalize → Validate → Publish (03-flows-design.md §2).

Deterministic end to end (no LLM). The flow never applies a covenant: it publishes proposals that Core
pulls through C1, and the RM confirms them in the onboarding grid.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel, Field, PrivateAttr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dps import history, jobs, services
from dps.agent.guard import injection_signs
from dps.extraction.agreement import extract_terms
from dps.extraction.mapping import Proposal, normalize
from dps.extraction.safety_net import possible_covenants
from dps.extraction.terms_info import extract_info
from dps.extraction.validation import FAILED, NEEDS_REVIEW, PUBLISH, validate
from dps.flows.persistence import SqlFlowPersistence
from dps.integrations.core import CoreClient
from dps.language import detect as detect_language
from dps.language import name as language_name
from dps.models import Document, ExtractedTerm, Job
from dps.parse.engine import Page, ParseError, needs_heavy

log = logging.getLogger(__name__)

ACTOR = "dps-flow-a"


_BASIS_WORDS = {"CONSOLIDATED": "group figures (consolidated)", "STANDALONE": "company figures (standalone)"}


@dataclass
class IntakeDeps:
    session_factory: sessionmaker[Session]
    read_file: Callable[[Document], bytes]
    parser: object  # ParserEngine or HeavyEngine: .parse(bytes) -> list[Page]
    core: CoreClient
    min_confidence: float
    heavy_pool: bool  # True on dps-worker-heavy: never hand off again
    # Optional AI covenant reader (extraction.agreement_ai.read): confirms, questions or adds to the rules' terms.
    ai_reader: Callable[[list[Page], list], list] | None = None
    default_currency: str = "INR"  # DPS_DEFAULT_CURRENCY: the loan's currency when Core cannot say


class IntakeState(BaseModel):
    id: str = ""
    job_id: str = ""
    document_id: str = ""
    pages: list[dict] = Field(default_factory=list)
    terms: list[dict] = Field(default_factory=list)
    proposals: list[dict] = Field(default_factory=list)
    route: str = ""
    error: str = ""
    outcome: Literal["", "PUBLISHED", "NEEDS_REVIEW", "FAILED", "HEAVY"] = ""


class AgreementIntakeFlow(Flow[IntakeState]):
    _deps: IntakeDeps = PrivateAttr()

    def __init__(self, deps: IntakeDeps, **kwargs) -> None:
        kwargs.setdefault("suppress_flow_events", True)  # workers log job steps; no console panels
        super().__init__(persistence=SqlFlowPersistence(deps.session_factory), **kwargs)
        self._deps = deps

    def _checkpoint(self, step: str) -> None:
        self.persistence.save_state(self.state.id, step, self.state)

    @start()
    def begin(self) -> str:
        return self.state.outcome

    @router(begin)
    def entry(self) -> Literal["resume", "parse"]:
        return "resume" if self.state.outcome == "NEEDS_REVIEW" else "parse"

    @listen("parse")
    def parse_document(self) -> None:
        with self._deps.session_factory() as db:
            doc = db.get(Document, self.state.document_id)
            data = self._deps.read_file(doc)
        try:
            pages = self._deps.parser.parse(data)
        except ParseError as ex:
            self.state.error = str(ex)
            pages = []
        self.state.pages = [p.as_dict() for p in pages]
        self._checkpoint("parse")

    @listen(parse_document)
    def extract(self) -> None:
        pages = self._pages()
        terms = extract_terms(pages)
        # Safety net: covenant-shaped sentences no term explains go to a person as possible covenants.
        candidates, _ = possible_covenants(pages, terms)
        found = terms + candidates
        if self._deps.ai_reader is not None:
            found = self._deps.ai_reader(pages, found)
        # Phase 3b-2: the rules read English. An agreement in another language is read by the AI reader (its quotes
        # checked on the page in the original language); without one, it waits for a person rather than publishing
        # nothing.
        lang = detect_language("\n".join(p.text or "" for p in pages))
        if lang != "en" and self._deps.ai_reader is None:
            from dps.extraction.agreement import Term

            found.append(Term("POSSIBLE_COVENANT", "CANDIDATE", None, None, None, None, pages[0].no if pages else None,
                              "", None, 0.0, issues=[f"LANGUAGE_NOT_READ: the agreement is in {language_name(lang)}; "
                                                     "the rules read English only. Switch on the AI reader "
                                                     "(DPS_AI_MODEL) or enter its covenants by hand"],
                              name=f"Agreement in {language_name(lang)}"))
        self.state.terms = [t.as_dict() for t in found + extract_info(pages)]
        self._checkpoint("extract")

    @listen(extract)
    def normalize_terms(self) -> None:
        from dps.extraction.agreement import Term

        terms = [Term(**t) for t in self.state.terms]
        masters = self._deps.core.active_masters() if terms else []
        entity = self._loan_entity()
        for t in terms:
            if t.basis and entity and t.basis != entity:
                # Phase 3b-1: the clause tests group figures and the borrower's statements are read for the company
                # (or the reverse): one setting decides which column is read, so a person decides.
                t.issues.append(f"ENTITY_DIFFERS: the agreement tests this on {_BASIS_WORDS[t.basis]}; this "
                                f"borrower's statements are read for {_BASIS_WORDS[entity]} (Borrower tab)")
        proposals = normalize(terms, masters, self._loan_currency())
        signs = injection_signs("\n".join(p.text for p in self._pages()))
        if signs:
            # Phase 3a: text written to steer an AI model: every proposal waits for a person, the reason on each.
            for p in proposals:
                p.issues.append(f"PROMPT_INJECTION_SUSPECTED: the agreement addresses an AI model: '{signs[0][:120]}'")
        self.state.proposals = [p.as_dict() for p in proposals]
        self._checkpoint("normalize")

    def _loan_entity(self) -> str | None:
        """Phase 3b-1: whose figures the borrower's statements are read for (Core profile), or None when unknown."""
        with self._deps.session_factory() as db:
            cp_ids = db.get(Document, self.state.document_id).active_cp_ids()
        profile = getattr(self._deps.core, "cp_profile", None)
        found = profile(cp_ids[0]) if cp_ids and profile else None
        return found.statement_entity if found is not None else None

    def _loan_currency(self) -> str | None:
        """Money for any country: amount limits are kept in the currency of the loan the agreement is for. Phase 2:
        the borrower's profile (Core); None when nobody knows it (the CP service did not answer), and the amount
        covenants then wait for a person."""
        with self._deps.session_factory() as db:
            cp_ids = db.get(Document, self.state.document_id).active_cp_ids()
        if not cp_ids:
            return self._deps.default_currency
        profile = getattr(self._deps.core, "cp_profile", None)
        found = profile(cp_ids[0]) if profile else None
        if found is not None:
            return found.currency if found.currency_known else None
        lookup = getattr(self._deps.core, "cp_currency", None)
        return (lookup(cp_ids[0]) if lookup else None) or self._deps.default_currency

    @router(normalize_terms)
    def validate_proposals(self) -> Literal["publish", "needs_review", "failed", "heavy"]:
        pages = self._pages()
        if not self._deps.heavy_pool and pages and needs_heavy(pages) and not self.state.error:
            self.state.route = "heavy"
            return "heavy"
        proposals = [Proposal(**p) for p in self.state.proposals]
        route = validate(pages, proposals, self._deps.core.validate_formula, self._deps.min_confidence)
        self.state.proposals = [p.as_dict() for p in proposals]
        self.state.route = route
        self._checkpoint("validate")
        return route

    @listen(PUBLISH)
    def publish_proposals(self) -> None:
        self._store(document_status="PUBLISHED")
        self.state.outcome = "PUBLISHED"
        self._checkpoint("publish")

    @listen(NEEDS_REVIEW)
    def hold(self) -> None:
        self._store(document_status="NEEDS_REVIEW")
        self.state.outcome = "NEEDS_REVIEW"
        self._checkpoint("hold")

    @listen(FAILED)
    def fail(self) -> None:
        self.state.error = self.state.error or "No text found in the document, even after OCR"
        with self._deps.session_factory() as db:
            jobs.guard_attempt(db)
            db.get(Document, self.state.document_id).status = "FAILED"
            history.pages_read(db, self.state.document_id, self.state.job_id, self.state.pages, self._deps.heavy_pool)
            history.result(db, self.state.document_id, self.state.job_id, "FAILED", self.state.error)
            db.commit()
        self.state.outcome = "FAILED"
        self._checkpoint("fail")

    @listen("heavy")
    def hand_to_heavy_pool(self) -> None:
        with self._deps.session_factory() as db:
            jobs.guard_attempt(db)
            history.pages_read(db, self.state.document_id, self.state.job_id, self.state.pages, False)
            history.result(db, self.state.document_id, self.state.job_id, "SENT TO DOCLING READER",
                           "too many pages the standard reader could not read")
            db.commit()
        self.state.outcome = "HEAVY"
        self._checkpoint("heavy")

    @listen("resume")
    def finish_review(self) -> None:
        """After review: the run completes once no term is left waiting for a decision."""
        with self._deps.session_factory() as db:
            jobs.guard_attempt(db)
            waiting = db.scalars(select(ExtractedTerm.id).where(
                ExtractedTerm.document_id == self.state.document_id,
                ExtractedTerm.status.in_(("NEEDS_REVIEW", "PROPOSED")))).first()
            doc = db.get(Document, self.state.document_id)
            if waiting is None:
                doc.status = "PUBLISHED"
                self.state.outcome = "PUBLISHED"
                history.result(db, doc.id, self.state.job_id, "PUBLISHED", "every covenant decided by a person")
            db.commit()
        self._checkpoint("resume")

    def _pages(self) -> list[Page]:
        return [Page(**p) for p in self.state.pages]

    def _store(self, document_status: str) -> None:
        """Upserts this document's REGEX proposals; terms a person already edited or decided are kept."""
        with self._deps.session_factory() as db:
            jobs.guard_attempt(db)
            job = db.get(Job, self.state.job_id)
            shadow = job is not None and job.rollout == "SHADOW"
            existing = list(db.scalars(select(ExtractedTerm).where(
                ExtractedTerm.document_id == self.state.document_id)))
            decided = {t.metric_raw for t in existing if t.reviewed_by}
            for t in existing:
                if not t.reviewed_by:
                    db.delete(t)
            count = 0
            stored = []
            for p in self.state.proposals:
                if p["metric_raw"] in decided:
                    continue
                stored.append(p)
                db.add(ExtractedTerm(
                    document_id=self.state.document_id, metric_raw=p["metric_raw"], name=(p.get("name") or "")[:200],
                    covenant_master_id=p["covenant_master_id"], expression=p["expression"],
                    operator=p["operator"], threshold=p["threshold"], unit=p["unit"], frequency=p["frequency"],
                    category=p["category"], confidence=p["confidence"], method=p["method"],
                    status="NEEDS_REVIEW" if shadow or p["issues"] else "PUBLISHED",
                    issues_json=json.dumps(p["issues"]), page=p["page"], quote=(p["quote"] or "")[:500],
                    schedule_json=json.dumps(p["schedule"]) if p.get("schedule") else None))
                count += 1
            doc = db.get(Document, self.state.document_id)
            doc.status = document_status
            doc.page_count = len(self.state.pages)
            services.audit(db, ACTOR, "INTAKE_" + document_status, "document", doc.id,
                           proposals=count, job=self.state.job_id)
            history.pages_read(db, doc.id, self.state.job_id, self.state.pages, self._deps.heavy_pool)
            history.agreement_found(db, doc.id, self.state.job_id, stored)
            flagged = sum(1 for p in stored if p["issues"])
            history.result(db, doc.id, self.state.job_id, document_status,
                           f"{len(stored)} found, {flagged} held for a person" if stored else "nothing found")
            db.commit()

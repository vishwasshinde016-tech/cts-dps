"""Tests of the statement reader through the steps a statement job takes in CTS (full review WP7: one pipeline).

``StatementProcessingFlow`` here is not a flow any more: it drives ``skills.statement_lines.reader`` the way a job
does. ``kickoff`` reads the statement (the job ends NEEDS_REVIEW, FAILED or goes to the HEAVY pool); kicking off
again with the first run's ``id`` is what a person's approval in Spring does: a statement held for something fixed
outside it (an FX rate, a borrower setting) is read again, and the figures as read are then stored by ``publish``
(the values table behind ``/api/data/values``). Nothing is stored before that, and nothing is sent to Core.
"""

from __future__ import annotations

from dps.models import Document, Job
from dps.skills.statement_lines.reader import (  # noqa: F401 - re-exported for the tests
    REREAD,
    CurrencyNotSettled,
    StatementDeps,
    StatementReader,
    StatementState,
    as_result,
    publish,
)

_RUNS: dict[str, StatementState] = {}


class StatementProcessingFlow:
    def __init__(self, deps: StatementDeps) -> None:
        self._deps = deps
        self.state = StatementState()

    def kickoff(self, inputs: dict) -> StatementState:
        previous = _RUNS.get(inputs.get("id") or "")
        reader = StatementReader(self._deps)
        if previous is None:
            self.state = reader.read(inputs["job_id"], inputs["document_id"])
            self._sync(self.state.outcome)
        else:
            state = previous
            if any(i.startswith(REREAD) for i in previous.issues):
                state = reader.read(previous.job_id, previous.document_id, review_done=True)
            if any(i.startswith(REREAD) for i in state.issues):
                state.outcome = "NEEDS_REVIEW"  # still held: what a person had to fix is not fixed
            else:
                self._approve(state)
                state.outcome = "REPORTED"
            state.id = previous.id
            self.state = state
        _RUNS[self.state.id] = self.state
        return self.state

    def _sync(self, outcome: str) -> None:
        with self._deps.session_factory() as db:
            doc = db.get(Document, self.state.document_id)
            if doc is not None and outcome in ("NEEDS_REVIEW", "FAILED"):
                doc.status = outcome
                db.commit()

    def _approve(self, state: StatementState) -> None:
        """A person approved every figure as read (Review done in Spring)."""
        with self._deps.session_factory() as db:
            doc = db.get(Document, state.document_id)
            fields, warnings, extra = as_result(state, doc.as_of_date, self._deps.report_derived_values)
            for f in fields:
                f["status"] = "CONFIRMED"
            job = db.get(Job, state.job_id) or Job(id=state.job_id, document_id=state.document_id, flow="SKILL_PACK",
                                                   job_type="EXTRACT_STATEMENT")
            if job.rollout == "SHADOW":
                return
            publish(db, job, {"fields": fields, "warnings": warnings, **extra}, self._deps.report_derived_values,
                    self._deps.fiscal_year_end_month)
            db.commit()


def review_of(flow) -> dict:
    """What the reviewer sees about the statement as a whole (result.statement, with its issues)."""
    _, _, extra = as_result(flow.state, None)
    meta = {k: v for k, v in extra["statement"].items() if k not in ("comparatives", "derivedReported")}
    return {"issues": flow.state.issues, **meta}


def review_done(flow):
    """What a person's approval does for a statement held for review."""
    again = StatementProcessingFlow(flow._deps)  # noqa: SLF001 - the test approves with the run's own dependencies
    again.kickoff(inputs={"id": flow.state.id, "job_id": flow.state.job_id, "document_id": flow.state.document_id})
    return again


def reviewed_if_clean(flow):
    """A statement with no issues, after a person approved it (tests of what storing the figures does)."""
    if flow.state.outcome == "NEEDS_REVIEW" and not flow.state.issues:
        return review_done(flow)
    return flow

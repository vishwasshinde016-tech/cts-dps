"""Skill-job flow: the CrewAI Flow the worker runs for every job type that reads a document (one pipeline, WP7).

Miro (Onboarding and Amendment flows): "Python worker invokes the CrewAI flow". Every job type runs through this
one flow (agreements, statements, sanctions, amendments, waivers, certificates, onboarding, document sets,
commentary), so each job is one isolated flow run with its own state.
Two fixed steps, no LLM: ``run_skills`` (classify, then the job's skill pack and tools)
and ``save_result`` (one ``dps_run``, the event, the job's end status). Exceptions keep their type, so the worker's
retry rules (Spring or storage down: retry; a bug: retry once, then DEAD) stay the same.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, PrivateAttr


class SkillJobState(BaseModel):
    id: str = ""
    job_id: str = ""
    job_type: str = ""
    status: str = ""  # the result's status (DONE / NEEDS_REVIEW / FAILED) once saved
    steps: list[str] = []


class SkillJobFlow(Flow[SkillJobState]):
    """``run`` builds the result from the page model; ``save`` stores it and ends the job."""

    _run: Callable[[], dict[str, Any]] = PrivateAttr()
    _save: Callable[[dict[str, Any]], None] = PrivateAttr()
    _result: dict[str, Any] | None = PrivateAttr(default=None)

    def __init__(self, run: Callable[[], dict[str, Any]], save: Callable[[dict[str, Any]], None], **kwargs) -> None:
        kwargs.setdefault("suppress_flow_events", True)  # workers log job steps; no console panels
        super().__init__(**kwargs)
        self._run = run
        self._save = save

    @start()
    def run_skills(self) -> None:
        self._result = self._run()
        self.state.steps.append("run_skills")

    @listen(run_skills)
    def save_result(self) -> None:
        self._save(self._result)
        self.state.status = str(self._result.get("status", ""))
        self.state.steps.append("save_result")


def run_skill_job(job_id: str, job_type: str, run: Callable[[], dict[str, Any]],
                  save: Callable[[dict[str, Any]], None]) -> SkillJobState:
    flow = SkillJobFlow(run, save)
    flow.kickoff(inputs={"job_id": job_id, "job_type": job_type})
    return flow.state

"""Miro: "Python worker invokes the CrewAI flow". ANALYZE, ONBOARD and the skill-pack jobs run as a CrewAI Flow
(``SkillJobFlow``), one isolated flow run per job, with the worker's retry rules unchanged."""

from __future__ import annotations

import pytest
from test_phase2_ingest import text_pdf
from test_phase4_skills import SANCTION, SERVICE, Tools, submit

from dps import worker as worker_module
from dps.flows import skill_job
from dps.flows.skill_job import SkillJobFlow, run_skill_job
from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.integrations.spring_tools import SpringTools
from dps.parse import sandbox
from dps.worker import Worker


def test_flow_runs_both_steps_and_saves_the_result():
    saved = []
    state = run_skill_job("J1", "ONBOARD", lambda: {"status": "NEEDS_REVIEW", "fields": []}, saved.append)
    assert saved == [{"status": "NEEDS_REVIEW", "fields": []}]
    assert state.job_id == "J1" and state.job_type == "ONBOARD"
    assert state.steps == ["run_skills", "save_result"] and state.status == "NEEDS_REVIEW"


def test_each_job_gets_its_own_flow_state():
    a = run_skill_job("A", "ANALYZE", lambda: {"status": "DONE"}, lambda _r: None)
    b = run_skill_job("B", "EXTRACT_SANCTION", lambda: {"status": "DONE"}, lambda _r: None)
    assert a.id != b.id and a.job_id == "A" and b.job_id == "B" and b.steps == ["run_skills", "save_result"]


@pytest.mark.parametrize("error", [OSError("storage down"), ValueError("a bug")])
def test_errors_keep_their_type_and_nothing_is_saved(error):
    saved = []

    def fail():
        raise error

    with pytest.raises(type(error)):
        run_skill_job("J1", "ONBOARD", fail, saved.append)
    assert saved == []


def test_worker_builders_run_jobs_through_the_flow(app, settings, monkeypatch):
    calls = []
    real = skill_job.run_skill_job

    def spy(job_id, job_type, run, save):
        calls.append(job_type)
        return real(job_id, job_type, run, save)

    monkeypatch.setattr(skill_job, "run_skill_job", spy)
    from dps.analyze import analysis
    from dps.skills import runner as skills

    saved = []
    monkeypatch.setattr(analysis, "analyze", lambda *a, **k: {"status": "DONE", "kind": "analysis"})
    monkeypatch.setattr(analysis, "finish", lambda db, job, result: saved.append(result["kind"]))
    monkeypatch.setattr(skills, "run_job", lambda *a, **k: {"status": "DONE", "kind": "pack"})
    monkeypatch.setattr(skills, "finish", lambda db, job, result: saved.append(result["kind"]))

    class FakeJob:
        id = "J9"
        job_type = "ONBOARD"

    worker_module.build_analyzer(settings, app.state.db)(None, FakeJob(), {"pages": []})
    worker_module.build_pack_runner(settings, app.state.db)(None, FakeJob(), {"pages": []}, None)
    assert calls == ["ONBOARD", "ONBOARD"] and saved == ["analysis", "pack"]


def test_sanction_job_end_to_end_through_the_flow(client, app, settings, tmp_path, monkeypatch):
    kicked = []
    original = SkillJobFlow.kickoff

    def kickoff(self, *a, **k):
        kicked.append(k.get("inputs", {}).get("job_type"))
        return original(self, *a, **k)

    monkeypatch.setattr(SkillJobFlow, "kickoff", kickoff)
    monkeypatch.setattr(SpringTools, "from_settings", classmethod(lambda cls, s: Tools()))
    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)
    w = Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, None,
               worker_module.build_pack_runner(settings, app.state.db))
    r = submit(client, app, "EXTRACT_SANCTION", text_pdf([SANCTION]), ".pdf", "application/pdf", "fv-1", "key-flow-1")
    assert r.status_code == 202, r.text
    assert w.run_once()
    job = client.get(f"/dps/v1/jobs/{r.json()['jobId']}", headers=SERVICE).json()
    assert job["status"] in ("DONE", "NEEDS_REVIEW")
    res = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    assert any(f["name"] == "SANCTIONED_AMOUNT" for f in res["fields"])
    assert kicked == ["EXTRACT_SANCTION"]

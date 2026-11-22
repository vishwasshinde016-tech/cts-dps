"""Large documents (Phase 1, 22 Nov 2026). The parse time limit was always the 120 s minimum, because the page count
was never passed (a 100-page agreement timed out on a busy machine); a PDF too long for the LIGHT pool tried it first
and only then moved to HEAVY; and the HEAVY limit of 900 s was too short for a 396-page facility agreement."""

from __future__ import annotations

from test_phase2_ingest import text_pdf
from test_phase2_worker import AGREEMENT, make_worker, submit

from dps import jobs
from dps.ingest.pipeline import LIGHT_MAX_PAGES, page_hint
from dps.models import Job
from dps.parse import sandbox


def test_the_page_count_is_read_without_parsing():
    assert page_hint(text_pdf([AGREEMENT] * 7), "PDF") == 7
    assert page_hint(b"not a pdf", "PDF") is None
    assert page_hint(b"a,b\n1,2\n", "CSV") is None


def test_the_parse_gets_the_page_count_so_its_time_limit_scales(client, app, settings, tmp_path):
    w, _, _, _ = make_worker(app, settings, tmp_path, [])
    hints: list = []
    w.ingestor._parse = lambda req, hint: (hints.append(hint), sandbox.parse_in_process(req))[1]
    submit(client, app, text_pdf([AGREEMENT] * 3), "dv-hint", "key-hint-00001")
    w.run_once()
    assert hints == [3]
    assert sandbox.Limits().wall_clock(100) == 600.0  # a 100-page agreement: 10 minutes, not 2


def test_a_pdf_too_long_for_the_light_pool_goes_to_heavy_without_trying(client, app, settings, tmp_path):
    w, _, _, _ = make_worker(app, settings, tmp_path, [])
    parsed: list = []
    w.ingestor._parse = lambda req, hint: (parsed.append(hint), sandbox.parse_in_process(req))[1]
    job_id = submit(client, app, text_pdf(["Page"] * (LIGHT_MAX_PAGES + 1)), "dv-long", "key-long-00001")
    w.run_once()
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.pool) == (jobs.QUEUED, "HEAVY")
    assert parsed == []


def test_the_heavy_pool_has_time_for_a_long_agreement():
    assert sandbox.Limits.for_pool("HEAVY").wall_clock(396) == 3600.0
    assert sandbox.Limits.for_pool("LIGHT").wall_clock(LIGHT_MAX_PAGES) <= sandbox.Limits().max_seconds

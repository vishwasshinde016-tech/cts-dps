"""Improvement plan Group 1 (24 Nov 2026): speed, scale and safety fixes found in the full review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_ai_guard import audits, database, spend  # noqa: F401 - the fixture

from dps.agent.guard import AiBudgetExhausted, AiDailyBudgetExhausted, check_budget, guarded
from dps.settings import Settings

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


# ---------- S11 the day's token cap

def test_the_daily_cap_stops_the_model_for_the_rest_of_the_day(database):  # noqa: F811
    settings = Settings(ai_daily_token_budget=1000)
    spend(database, 400, NOW - timedelta(days=1))  # yesterday does not count
    spend(database, 900, NOW - timedelta(hours=2))
    with database.session_factory() as s:
        check_budget(s, settings, NOW)  # 900 of 1000: still allowed
    spend(database, 100, NOW - timedelta(minutes=5))
    with database.session_factory() as s, pytest.raises(AiDailyBudgetExhausted):
        check_budget(s, settings, NOW)
    assert audits(database, "AI_DAILY_BUDGET_EXHAUSTED") == 1
    with database.session_factory() as s:
        check_budget(s, settings, NOW + timedelta(days=1))  # a new day


def test_the_routes_files_daily_budget_is_enforced_when_no_setting(database):  # noqa: F811
    spend(database, 500, NOW - timedelta(hours=1))
    called = []
    call = guarded(lambda m: called.append(m) or "ok", "m", database.session_factory, Settings(), lambda: NOW,
                   daily=500)
    with pytest.raises(AiBudgetExhausted):
        call([{"role": "user", "content": "x"}])
    assert called == []  # the model was not called


def test_a_spent_day_is_reported_as_its_own_code_and_not_as_an_outage():
    from dps.agent.gateway import AiDailyBudgetSpent, AiMonthlyBudgetSpent

    assert AiDailyBudgetSpent.code == "AI_DAILY_BUDGET_SPENT"
    assert issubclass(AiDailyBudgetSpent, AiMonthlyBudgetSpent)  # handled like a spent month: no breaker failure


# ---------- S3 queue limits for bulk uploads

def test_bulk_uploads_run_more_files_at_once():
    s = Settings()
    assert s.queue_max_running_per_tenant == 10 and s.queue_max_running_per_batch == 6
    assert s.queue_max_running_per_batch < s.queue_max_running_per_tenant  # room left for interactive uploads


# ---------- S5 lookup indexes

def test_jobs_have_lookup_indexes():
    from dps.models import Job

    names = {i.name for i in Job.__table__.indexes}
    assert {"ix_dps_job_document", "ix_dps_job_version", "ix_dps_job_correlation"} <= names

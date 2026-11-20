"""GAP-05: tokens and cost of model calls are recorded per request and summarised for support."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dps.advisory.advisor import Advisor
from dps.agent import usage
from dps.main import create_app

GOLDEN = Path(__file__).resolve().parents[2] / "golden-set" / "documents" / "AGR-003.pdf"


def metered(messages):
    """A model that reports 1,200 prompt and 80 completion tokens for every call."""
    usage.note("azure/test-model", 1200, 80)
    return "The DSCR floor is 1.50 times [p. 4]."


@pytest.fixture
def ai_client(settings, checker):
    settings.ai_prompt_cost_per_1k = 0.005
    settings.ai_completion_cost_per_1k = 0.015
    return TestClient(create_app(settings, cp_checker=checker, create_schema=True, advisor=Advisor(metered)))


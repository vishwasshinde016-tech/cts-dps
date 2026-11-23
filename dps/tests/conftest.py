from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dps.api.auth import Principal
from dps.db import Base, make_engine
from dps.main import create_app
from dps.settings import AuthMode, Settings

# The tests never read a developer's own .env (the AI switched on, real paths) nor model keys left in the process
# environment: they run the same on a laptop with Azure configured as in CI.
Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _no_local_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("DPS_", "AZURE_", "OPENAI_", "OTEL_")) and key != "DPS_TEST_DATABASE_URL":
            monkeypatch.delenv(key, raising=False)
    # A laptop's dps/.env may point at Langfuse (setup.ps1 -Observability): tests never read it, never send spans.
    monkeypatch.setenv("DPS_OTEL_ENV_FILE", "")


class FakeChecker:
    """Ownership map: user -> CPs. ADMIN/SERVICE bypass, AUDITOR and SUPPORT read (same rule as Core)."""

    def __init__(self, owners: dict[str, set[str]]) -> None:
        self.owners = owners
        self.calls: list[tuple[str, str, bool]] = []

    def allowed(self, principal: Principal, cp_id: str, read_only: bool) -> bool:
        from dps.api.access import has_bypass
        self.calls.append((principal.user, cp_id, read_only))
        return has_bypass(principal, read_only) or cp_id in self.owners.get(principal.user, set())


# Set DPS_TEST_DATABASE_URL (e.g. mssql+pymssql://sa:...@localhost:1433/dps_test) to run the suite on
# SQL Server instead of a throwaway SQLite file; each test then starts from an emptied schema.
TEST_DATABASE_URL = os.environ.get("DPS_TEST_DATABASE_URL", "")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    url = TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'dps.db'}"
    if TEST_DATABASE_URL:
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    # _env_file=None: a developer's own .env (AI switched on, real paths) never changes what the tests run against
    return Settings(_env_file=None, database_url=url, storage_dir=tmp_path / "store",
                    auth_mode=AuthMode.DEV_HEADER, max_upload_mb=1)


@pytest.fixture
def checker() -> FakeChecker:
    return FakeChecker({"rm-anil": {"CP-1", "CP-2"}, "approver-1": {"CP-1"}, "rm-other": {"CP-9"}})


@pytest.fixture
def app(settings: Settings, checker: FakeChecker):
    return create_app(settings, cp_checker=checker, create_schema=True)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def h(user: str = "rm-anil", roles: str = "RM") -> dict[str, str]:
    return {"X-User": user, "X-Roles": roles}


from statement_shim import review_done, reviewed_if_clean  # noqa: E402, F401 - statement tests share them

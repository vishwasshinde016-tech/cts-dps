"""Configuration from environment variables (prefix ``DPS_``)."""

from __future__ import annotations

import socket
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthMode(str, Enum):
    JWT = "JWT"
    DEV_HEADER = "DEV_HEADER"  # dev/test only: trust X-User / X-Roles
    # The bank app has no identity provider: Spring calls with a service token from the vault, only from its own
    # network; the acting user comes in X-User (full review G11).
    SERVICE_TOKEN = "SERVICE_TOKEN"  # noqa: S105  # nosec B105 - a mode name, not a secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DPS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./dps.db"
    storage_dir: Path = Path("./storage")
    # Phase 2: the DMS store. storage_dir holds committed versions (read-only for DPS in production), staging_dir
    # files not yet committed (ANALYZE), derived_dir DPS's own write-once artefacts (page models, page images).
    staging_dir: Path = Path("./staging")
    derived_dir: Path = Path("./derived")
    max_file_mb: int = 100
    # The spec's audit-seals/ folder: daily hash-chain checkpoints, kept apart from derived/ and separately protected.
    seals_dir: Path = Path("./audit-seals")
    # G6 (Miro: "call the API to update the covenants"): when set, the leader worker also POSTs every event to this
    # path on the Spring app (core_base_url), in order, at least once. Empty: Spring pulls GET /dps/v1/events only.
    events_push_path: str = ""
    scorer_dir: str | None = None
    events_push_batch: int = 50
    ingest_enabled: bool = True
    # Unix socket of the parser container (network_mode: none). Empty: parse in a sandboxed child process.
    parser_socket: str = ""
    # The Document Agent (CrewAI Agent + skills + tools, agent/document_agent.py) on the bank's Azure OpenAI
    # deployments. Off unless switched on with a model (llm_allowed); CI and tests never call a model. Rules always
    # read first; the agent proposes, a person decides what the rules and the agent disagree on.
    llm_enabled: bool = False
    # Model routes (small, strong, vision): deployments, timeouts, rate limits, token budgets.
    ai_routes_file: str = ""
    # Worker processes sharing the Azure deployments: each process uses rpm / ai_rpm_share (full review G16).
    ai_rpm_share: int = Field(default=1, ge=1)
    # Hosts the model calls may go to (comma separated, e.g. "bank-ai.openai.azure.com"). With llm_enabled, every
    # configured model base URL (AZURE_API_BASE, OPENAI_API_BASE, ...) must be on this list, or no model is used.
    ai_allowed_hosts: str = ""
    # Phase 7 rollout per job type: SHADOW (results stored, not shown: dps.extraction.shadow), ASSISTED (every field
    # to review), NORMAL (thresholds). Unlisted types use rollout_default. Env: DPS_ROLLOUT_MODES='{"EXTRACT_X": ...}'
    rollout_default: Literal["SHADOW", "ASSISTED", "NORMAL"] = "NORMAL"
    rollout_modes: dict[str, Literal["SHADOW", "ASSISTED", "NORMAL"]] = Field(default_factory=dict)
    loan_account_pattern: str = ""
    facility_id_pattern: str = ""
    max_upload_mb: int = 50
    max_pages: int = 500  # FR-01: longer PDFs are refused at upload

    # "prod" refuses DEV_HEADER at startup; set DPS_ENV=prod in every production deployment.
    env: str = "dev"
    auth_mode: AuthMode = AuthMode.JWT
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_jwks_url: str = ""
    jwt_public_key_pem: str = ""  # alternative to JWKS (tests, air-gapped)
    jwt_principal_claim: str = "preferred_username"
    jwt_roles_claim: str = "roles"
    # Clock difference allowed between the identity provider and DPS, in seconds (Core allows 60).
    jwt_leeway_seconds: int = 30
    # SERVICE_TOKEN mode: the SHA-256 (hex) of the token Spring sends (the token itself stays in the vault) and the
    # networks it may call from (comma separated, e.g. "10.20.0.0/24,127.0.0.1/32"). Both are required.
    service_token_sha256: str = ""
    service_allowed_networks: str = ""
    # Full review G22: the figures DPS works out from a statement (total debt, EBITDA from its parts, trailing twelve
    # months) are not reported; Spring's formula engine works them out from the printed figures. On: the old way.
    report_derived_values: bool = False

    # The Spring app (CTS), used for CP ownership checks and the Spring tools (spring-tools.yaml). The DPS_CORE_*
    # names are kept so existing deployments need no config change.
    core_base_url: str = "http://localhost:9310"
    core_timeout_seconds: float = 5.0
    core_health_path: str = ""
    # DPS → Spring calls. Production: client credentials (token refreshed automatically,
    # wins over core_service_token). A fixed core_service_token expires; empty both: SERVICE dev headers.
    core_token_url: str = ""
    core_client_id: str = ""
    core_client_secret: str = ""
    core_scope: str = ""
    core_service_token: str = ""

    # Flow A: proposals below this confidence, or with any issue, are held for review.
    review_confidence: float = 0.8
    # Workers
    # Defaults to the host name, so scaled replicas (docker compose --scale, Kubernetes) are told apart.
    worker_id: str = socket.gethostname() or "dps-worker"
    worker_pool: str = "STANDARD"  # STANDARD | HEAVY
    # A running job's lease, renewed every third of it while the worker is alive: a worker that dies
    # gives its job back within this time, and a long job is never taken over while it still runs.
    worker_lease_seconds: int = 120
    worker_heartbeat_file: Path | None = None
    go_live_evaluation_file: str = ""
    diagnostics_retention_days: int = Field(default=0, ge=0)
    worker_max_attempts: int = 3
    # Phase 1 queue rules (CTS DPS design, Phase 1 tab). A transient failure waits
    # min(retry_max, retry_base * 4^(attempt-1)) seconds, +/-20%, before the next attempt.
    worker_retry_base_seconds: float = 10.0
    worker_retry_max_seconds: float = 900.0
    # A job whose lease expired this many times (its worker died on it each time) is a poison file: DEAD.
    worker_poison_crashes: int = 2
    # Waiting this long moves a job one priority step up, so bulk work is never starved.
    queue_aging_minutes: int = 15
    # Fairness defaults when a tenant has no dps_tenant_limit row. Raised for many files and heavy jobs (improvement
    # plan S3, 24 Nov 2026): 4 per tenant and 2 per batch made a bulk upload wait in line while workers and the Azure
    # quota were free. A bulk batch still leaves room for interactive uploads (6 of 10).
    queue_max_running_per_tenant: int = 10
    queue_max_running_per_batch: int = 6
    queue_max_queued_per_tenant: int = 1000
    batch_max_items: int = 500
    # Part of the "same input runs once" key and of the page-model cache. "auto" (default): computed at start-up from
    # the code, skill packs, taxonomy, parser libraries and model routes (dps/versioning.py; full review G13), so a
    # change never returns an old result. Set a fixed value only to pin a version on purpose.
    pipeline_version: str = "auto"
    # The bank app has no tenants today: one default. A request for any other tenant is refused (TENANT_MISMATCH).
    tenant_id: str = "default"
    ocr_enabled: bool = True

    # Advisory AI (C3 suggestions, statement narrative). Empty: rules only. Example: azure/gpt-4o
    # with AZURE_API_KEY, AZURE_API_BASE and AZURE_API_VERSION set for the Azure client.
    ai_model: str = ""
    # Empty: the model's default (required by newer Azure OpenAI models). Set e.g. 0 where the model allows it.
    ai_temperature: float | None = None
    # Longest wait for one model answer. Kept under the gateway's 120 s so a slow model gives the rules
    # answer instead of a 504 page.
    ai_timeout_seconds: float = 180.0
    # Statement PDFs: the model also reads the statement pages and a covenant input the two readers read
    # differently is held for review (extraction/statement_ai.py). Needs ai_model; off by default (cost).
    statement_ai_check: bool = True
    # Which entity's figures a statement with both is read for: STANDALONE (the company) or CONSOLIDATED (the
    # group). Both readers use it: the table reader's column and the second reader's instructions.
    statement_entity: Literal["STANDALONE", "CONSOLIDATED"] = "STANDALONE"
    # The month borrowers' financial years end (3: March, as in India and Sri Lanka). Used only when a statement does
    # not say how many months its profit covers: a statement to 30 September is then six months.
    fiscal_year_end_month: int = Field(default=3, ge=1, le=12)
    # Money for any country: the currency of a loan when Core cannot say (no "currency" or facility unit on the CP).
    # Amounts are kept in crore for INR and in millions for any other currency.
    default_currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    # Model price per 1,000 tokens (the bank's Azure OpenAI rates); empty leaves the recorded cost empty.
    ai_prompt_cost_per_1k: float | None = None
    ai_completion_cost_per_1k: float | None = None
    # AI traceability (dps_ai_call): keep the text sent to the model and its answer, secrets masked. Off (default)
    # keeps only the model, prompt hash, tokens, time and outcome of each call: no document text in logs.
    ai_trace_text: bool = False
    # Phase 3a: model tokens allowed per calendar month (prompt + answer); empty: no cap. At 80% an alert is logged and
    # audited; at 100% the model is not called and the rules answer until the next month.
    ai_monthly_token_budget: int | None = Field(default=None, ge=1)
    # Tokens allowed per calendar day (UTC); empty: the routes file's budgets.per_tenant_day_tokens when a routes file
    # is used, else no daily cap. Production sets both (deploy/docker-compose.cts-dps.yml).
    ai_daily_token_budget: int | None = Field(default=None, ge=1)
    # Model calls one job may run at once (improvement plan S1, 24 Nov 2026): a document's AI reads (covenant parts,
    # page batches, schedule batches) run in parallel, within each route's requests per minute. 1 = one at a time.
    ai_parallel_reads: int = Field(default=3, ge=1, le=16)
    # Jobs one worker process runs at once, each on its own thread (25 Nov 2026: a laptop with one worker read one
    # document at a time while the rest of an upload waited in the queue). Each job also reads with
    # ai_parallel_reads model calls, so the Azure load is up to worker_threads x ai_parallel_reads calls at once.
    worker_threads: int = Field(default=3, ge=1, le=16)
    ai_skip_confident: bool = True
    # The same messages to the same model are answered from the cache (dps_ai_cache) instead of a new call. Off by
    # default (full review G17): cached answers hold document quotes outside the DMS's custody. When on, an answer
    # is used only for the same pipeline version and for ai_cache_days, and older answers are purged daily.
    ai_cache: bool = False
    ai_cache_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _resolve_pipeline(self) -> Settings:
        from dps.versioning import resolve

        if self.env.lower() in ("prod", "production") and "rollout_default" not in self.model_fields_set:
            self.rollout_default = "SHADOW"

        # Settings that change what is read, found or routed to a person (T3). Logging, limits and switches that
        # do not change a result stay out, so they do not make every old job look stale.
        options = ";".join(f"{name}={getattr(self, name)}" for name in OUTPUT_SETTINGS)
        self.pipeline_version = resolve(self.pipeline_version, self.ai_routes_file, self.ai_model, options,
                                        self.scorer_dir or "")
        return self


OUTPUT_SETTINGS = ("review_confidence", "ocr_enabled", "ai_temperature", "statement_ai_check", "ai_skip_confident",
                   "ai_parallel_reads", "statement_entity",
                   "fiscal_year_end_month", "report_derived_values", "loan_account_pattern", "facility_id_pattern",
                   "llm_enabled")


def llm_allowed(settings: Settings) -> bool:
    """True only when the switch is on AND a model is configured. Default: False."""
    return bool(settings.llm_enabled and (settings.ai_model or settings.ai_routes_file))


MODEL_URL_VARS = ("AZURE_API_BASE", "AZURE_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_BASE", "OPENAI_BASE_URL")


def endpoint_problem(settings: Settings, env: dict | None = None) -> str | None:
    """Why the model may not be called (None when it may): a configured base URL whose host is not on
    ai_allowed_hosts, or no base URL at all. Document pages only ever go to the bank's own deployments."""
    import os
    from urllib.parse import urlparse

    env = dict(os.environ) if env is None else env
    allowed = {h.strip().lower() for h in settings.ai_allowed_hosts.split(",") if h.strip()}
    urls = [env[v] for v in MODEL_URL_VARS if env.get(v)]
    if not urls:
        return "no model base URL is configured (AZURE_API_BASE)"
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if not host or host not in allowed:
            return f"model host {host or url!r} is not on DPS_AI_ALLOWED_HOSTS"
    return None


MODEL_ENV_PREFIXES = ("AZURE_", "OPENAI_")


def export_model_env(path: str | Path = ".env") -> list[str]:
    """The model client reads AZURE_* / OPENAI_* from the process environment, but
    pydantic reads .env only into Settings. Copies those lines of .env into os.environ, never overriding a value
    already set (a vault or the shell wins). Returns the names copied (never the values)."""
    import os

    file = Path(path)
    if not file.is_file():
        return []
    copied = []
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key.startswith(MODEL_ENV_PREFIXES) and value and key not in os.environ:
            os.environ[key] = value.strip('"').strip("'")
            copied.append(key)
    return copied


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    export_model_env()
    return Settings()


def runtime_settings() -> Settings:
    """Production and local app processes require Azure-backed document extraction."""
    export_model_env()
    settings = Settings(ai_skip_confident=False, statement_ai_check=True, ai_cache=False)
    if not llm_allowed(settings):
        raise RuntimeError("DPS requires Azure AI. Set DPS_LLM_ENABLED=true and configure a model or routes.")
    if problem := endpoint_problem(settings):
        raise RuntimeError(f"DPS requires an approved Azure endpoint: {problem}")
    return settings

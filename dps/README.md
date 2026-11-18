# Document Processing Service (DPS)

Turns loan agreements and financial statements into covenant **proposals** and financial **values** for Covenant Core. It never decides compliance. Contract: [`../contracts/dps-api.yaml`](../contracts/dps-api.yaml). The CTS job API (submit and follow jobs, batches, event feed) is [`../contracts/dps-jobs-api.yaml`](../contracts/dps-jobs-api.yaml); see [`../contracts/README.md`](../contracts/README.md).

## What it does

| Part | Detail |
|---|---|
| API | Documents (upload, idempotent, link to N CPs, original file), extracted terms review, jobs (status, resume, support list and retry), manual values, C1 recommendations, C2 values, catalog, C3 AI suggestions, statement narrative, Ask AI |
| Flow A | Parse → Extract (rules, safety net, optional AI reader) → Normalize → Validate → Publish / hold for review (`dps.flows.intake`) |
| Flow B | Parse → Extract/Calculate → Validate (checks, optional AI reader) → Report / hold (`dps.flows.statement`); C5 event per linked CP. A reviewer's corrections are kept when the statement is reported |
| Every step | [../docs/dps-how-it-works.md](../docs/dps-how-it-works.md) |
| Workers | `python -m dps.worker`; `DPS_WORKER_POOL=HEAVY` (with `.[heavy]`) runs Docling for scans |
| Advisory AI | Rules by default; set `DPS_AI_MODEL` (for example `azure/<deployment>`) to use an LLM through CrewAI. Always labelled advisory |
| Tracking | Each document's history (`GET /dps/v1/documents/{id}/history`), every AI call (`dps_ai_call`), log lines with request id, user, job and document, OpenTelemetry when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and the daily log file (`python -m dps.logcollector`): [../docs/observability.md](../docs/observability.md) |
| Auth | Entra ID JWT or `DEV_HEADER`; CP access delegated to Core |

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # includes the worker extras (CrewAI, RapidOCR)
cp .env.example .env               # DEV_HEADER mode, SQLite
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn --factory dps.main:create_app --port 9400
.venv/Scripts/python -m dps.worker   # second terminal: processes uploaded documents
```

Call with dev headers, for example `X-User: rm-anil` and `X-Roles: RM`.

## Test

```bash
.venv/Scripts/pytest          # coverage gate 90%; includes the wording stress gates
.venv/Scripts/ruff check src tests
.venv/Scripts/python tests/stress_covenants.py     # full covenant wording grid (11,919 cases; CI runs every third)
.venv/Scripts/python tests/stress_nonfinancial.py  # 72 non-financial wordings
.venv/Scripts/python tests/stress_statements.py    # 1,080 statement labels and 136 look-alikes
.venv/Scripts/python tests/stress_several_ratios.py  # 1,620 clauses with two ratios each
```

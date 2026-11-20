# Document Processing Service (DPS)

Turns loan agreements, amendments, waivers and financial statements into covenant **proposals** and financial **values** for the CTS Spring app. It never decides compliance. The CTS job API (submit and follow jobs, batches, event feed) is [`../contracts/dps-jobs-api.yaml`](../contracts/dps-jobs-api.yaml); see [`../contracts/README.md`](../contracts/README.md).

## What it does

| Part | Detail |
|---|---|
| API | Documents (upload, idempotent, link to N CPs, original file), extracted terms review, jobs (status, resume, support list and retry), manual values, C1 recommendations, C2 values, catalog, C3 AI suggestions, statement narrative, Ask AI |
| Flow A | Parse → Extract (rules, safety net, optional AI reader) → Normalize → Validate → Publish / hold for review (`dps.flows.intake`) |
| Flow B | Parse → Extract/Calculate → Validate (checks, optional AI reader) → Report / hold (`dps.flows.statement`); C5 event per linked CP. A reviewer's corrections are kept when the statement is reported |
| Every step | [../docs/cts-dps/end-to-end-explained.md](../docs/cts-dps/end-to-end-explained.md) |
| Workers | `python -m dps.worker`; `DPS_WORKER_POOL=HEAVY` (with `.[heavy]`) runs Docling for scans |
| Advisory AI | Rules by default; set `DPS_AI_MODEL` (for example `azure/<deployment>`) to use an LLM through CrewAI. Always labelled advisory |
| Tracking | Each document's history (table `dps_trace_event`, shown by `scripts\show_job.py`), every AI call (`dps_ai_call`), log lines with request id, user, job and document, OpenTelemetry when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and the daily log file (`python -m dps.ops.logcollector`): [`src/dps/ops/observability.py`](src/dps/ops/observability.py) |
| Auth | Entra ID JWT or `DEV_HEADER`; CP access delegated to the Spring app |

## Code layout

One home per concept, two levels deep at most. Processes start from `dps.main` (API) and `dps.worker` (workers).

```text
src/dps/
  main.py, worker.py      process entry points: the FastAPI app and the job worker loop
  settings.py, db.py, models.py, errors.py
                          configuration, database session, tables, the error envelope
  jobs.py, feed.py        the job queue on dps_job and the event feed Spring reads
  results.py, services.py, history.py
                          the dps.result document, document services, history
  catalog.py, profiles.py, money.py, language.py
                          shared domain knowledge: statement lines, country profiles, money, language
  api/                    HTTP edge: jobs (the CTS job API), review, evidence, auth, access, shared deps
  ingest/                 reading stored bytes (DMS store, upload store) and file inspection
  parse/                  parsing engine (text, OCR, Docling), the page model, sandbox, segmentation
  analyze/                classification, identifiers, key pages, obligation checks
  extraction/             deterministic readers: agreement covenants, statements, schedules, validation
  skills/                 skill packs run by the generic runner; covenants/ holds the taxonomy, the parts,
                          the candidate ledger and the residual read
  agent/                  the Document Agent, the model gateway, vision, model-call guards and usage
  scoring/                field confidence and review routing
  flows/                  CrewAI Flows: agreement intake, statement processing, skill jobs, state persistence
  integrations/           outbound calls to Spring: Spring reads (spring.py), Java business-rule tools, service identity
  advisory/               advisory AI for people: narratives, portfolio questions, agreement Q&A
  ops/                    observability, the daily log collector, leader lock and audit seals, go-live readiness
  contract/               packaged copies of ../contracts (schemas and catalogues served by the API)
```

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

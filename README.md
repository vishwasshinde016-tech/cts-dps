# CTS DPS (Document Processing Service)

The Python service that reads loan documents for the CTS covenant tracking system, built to the architect's design:
Spring and DPS on the same VM, talking through jobs; a Python worker polls the job queue; each job runs in its own
CrewAI Flow; one Document Agent with several skills and tools; business rules in Java (Spring) called as tools.

This folder holds DPS only. The Spring app and the DMS are built by other teams and are not here.

Copied on 18 Nov 2026 from `Vishwas2311/loan-covenant-platform`, branch `feat/dps-v2-phase1`, commit `55996cc`.

## Folders

| Folder | What it is |
| --- | --- |
| `dps/` | The service: `src/dps` (code), `tests/`, `migrations/` (database), `Dockerfile`, `pyproject.toml` |
| `contracts/` | The agreed interfaces: job API, result schema, page-model schema, Spring tools, error codes |
| `deploy/` | Running on the bank VM: `docker-compose.cts-dps.yml`, alerts, model routes example |
| `docs/cts-dps/` | Design, schema explained, runbook, go-live, model inventory, check against the architect |
| `golden-set/` | Labelled sample agreements and statements used to measure extraction quality |

## Where each architect point lives

| Architect asked for | Code |
| --- | --- |
| Jobs between Spring and DPS | `dps/src/dps/jobapi.py`, `contracts/dps-jobs-api.yaml` |
| Python worker polls the job queue | `dps/src/dps/worker.py`, `dps/src/dps/jobs.py` |
| CrewAI Flow per job, isolated context | `dps/src/dps/flows/skill_job.py` (also `flows/intake.py`, `flows/statement.py`) |
| One agent, many skills and tools | `dps/src/dps/agent/document_agent.py`, `dps/src/dps/skills/*/pack.yaml` |
| Java business rules as Python tools | `dps/src/dps/spring_tools.py`, `contracts/spring-tools.yaml` |
| Schema of the extraction tool | `contracts/schemas/page-model.json`, `contracts/schemas/result.json` |
| PDF (text or image), Excel, Word | `dps/src/dps/parsing.py`, `dps/src/dps/parse/page_model.py` |
| Data quality, confidence | `dps/src/dps/scoring/`, page `quality` in the page model |
| Logging, exceptions | `dps/src/dps/observability.py`, `dps/src/dps/contract/errors.yaml` |
| Model calls (Azure GPT) | `dps/src/dps/agent/gateway.py` (budget, masking, retries, call log) |

## Run the tests

```bash
cd dps
python -m venv C:\Users\vishw\.venvs\cts-dps        # kept outside OneDrive
C:\Users\vishw\.venvs\cts-dps\Scripts\python -m pip install -e ".[worker,dev]"
C:\Users\vishw\.venvs\cts-dps\Scripts\python -m pytest
```

## Turn on the Azure model

Copy `dps/.env.example` to `dps/.env` and fill in the Azure values yourself. The `.env` file was not copied here on
purpose: it holds the API key and this folder syncs to OneDrive. Move `.env` aside while running the tests, or the
tests pick up the live model settings.

## Older parts still inside

`EXTRACT_AGREEMENT` and `EXTRACT_STATEMENT` jobs still run the earlier flows (`flows/intake.py`, `flows/statement.py`),
with the Document Agent as their reader. Some modules serve the earlier Covenexa Core demo (`api.py`, `review_api.py`,
`core_client.py`). They are kept because tests and those two job types use them.

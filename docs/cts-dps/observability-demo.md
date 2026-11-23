# Observability and traceability: Langfuse demo

DPS records every document it reads as one **trace**, using OpenTelemetry, the open standard. The trace shows each step, how long it took, and each AI call (model, tokens, prompt version, time, failures). For the demo the traces go to **Langfuse**, an open-source (MIT) tool that runs in Docker on this laptop. Nothing leaves the laptop: Langfuse's own telemetry is off, every port is bound to `127.0.0.1`, and DPS sends traces only to `127.0.0.1`.

Audit-grade records stay in DPS's own database, and each one carries the trace id, so it opens its trace in Langfuse and back:
- every AI call (`dps_ai_call`);
- each AI finding in the document history (`dps_trace_event`);
- every review decision;
- the daily hash-chain seal.

## 1. One-time setup (about 10 minutes, most of it downloading)

Open PowerShell in `C:\cts-dps\dps`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Observability
```

This installs DPS (if not done yet), checks the OCR model, then starts Langfuse:
- it starts Docker Desktop if needed;
- it generates the secrets into `deploy\observability\.env` (git-ignored);
- it starts 6 containers: Langfuse web and worker, Postgres, ClickHouse, Redis and MinIO;
- it writes the 3 `OTEL_*` lines into `dps\.env`.

Langfuse alone, at any time (also after a reboot):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\cts-dps\deploy\observability\start-langfuse.ps1
```

**Office laptop without internet, or with Docker Hub blocked.** On a laptop with internet, run `scripts\make-offline-bundle.ps1`. Copy the project together with `offline-bundle\` and `deploy\observability\images\`. On the office laptop, run `scripts\setup.ps1 -Offline -Observability`. It loads the Langfuse images from the file, checking their SHA-256, and installs the Python packages from the bundle.

Needs: Docker Desktop, about 6 GB of free RAM for Langfuse, and about 5 GB of disk.

## 2. Sign in

Open `http://localhost:3000`.
- User: `demo@cts-dps.local`.
- Password: the `LANGFUSE_USER_PASSWORD` line in `C:\cts-dps\deploy\observability\.env`.

Then open the project **CTS DPS**, then **Tracing**.

## 3. Make traces

Restart the DPS API and worker after setup, so they read the new `OTEL_*` lines. Then run any demo phase; every job is one trace. For example, phase 3:

```powershell
C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\architect-documents\us_ex10_1_credit_agreement_2020.pdf --type LOAN_AGREEMENT --json C:\cts-dps\dps\ex10-result.json
```

Without the API and worker, the one-document demo is also a trace:

```powershell
C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\demo_onboard.py C:\cts-dps\golden-set\documents\AGR-001.pdf --agent
```

Leave out `--agent` for rules only: that trace then has steps but no AI calls.

## 4. What to show (about 5 minutes)

| Show | Where in Langfuse | What to say |
|---|---|---|
| One document, start to finish | Tracing, then the trace named after the job type (`ONBOARD`, `EXTRACT_AGREEMENT` ...) | "Every document is one trace: reading, classifying, the skill pack, each AI read, the ledger, scoring." |
| The steps | The tree on the left: `dps.job`, then `dps.ingest` (pages, OCR pages, Docling pages), `dps.classify` (type, confidence), `dps.pack covenants`, then `dps.ai.read covenants` part 1/3 ..., `dps.ledger`, `dps.enrich`, `dps.scoring` | "We see where time goes. A slow OCR page or a slow AI call stands out." |
| One AI call | A `chat <deployment>` generation | "Model, tokens in and out, time, prompt version (a hash of the instructions), attempt number. A retried call is its own red step." |
| A failed step | A red span with `error.type` | "Errors are visible without reading logs. Messages are masked; no stack traces with document text." |
| Cost | The trace's token total, or Dashboard | "Tokens per document and per day." |
| Sessions | Sessions: one per document | "Every job on one document side by side: onboarding, amendment, reprocess." |
| Traceability | In DPS: `dps_trace_event` (`SHOWN` / `FOUND` rows with `ai_span_id`) and `dps_ai_call` (`trace_id`, `span_id`) | "Each AI finding knows the AI read and the model call that produced it, by id. The audit tables are permanent; Langfuse is the viewer." |

## 5. What is and is not sent

| Data | In the trace? |
|---|---|
| Ids (job, document hash, field id), counts, times, page numbers, model name, tokens, prompt version | Yes |
| Error type and a short, masked error message | Yes |
| File name, document text, prompts, AI answers | **No, by default.** Only with `DPS_AI_TRACE_TEXT=true` in `dps\.env`, and then masked (personal data, keys and passwords). For the demo, use public documents only. |
| Anything to the internet | No: Langfuse runs on `127.0.0.1`, with its telemetry off |

## 6. If something goes wrong

| Symptom | Fix |
|---|---|
| No traces in Langfuse | Restart the DPS API and worker (they read `dps\.env` on start). Check that `dps\.env` has `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`. Check `http://127.0.0.1:3000/api/public/health` |
| `start-langfuse.ps1`: port 3000 in use | `start-langfuse.ps1 -Port 3100` (also updates `dps\.env`) |
| Docker Desktop does not start | Start it from the Start menu, wait for "Engine running", run the script again |
| Langfuse down during a run | Nothing breaks: DPS drops the spans and every job finishes normally |
| Start from scratch | `stop-langfuse.ps1 -Purge` (deletes all traces), delete `deploy\observability\.env`, start again |

Stop Langfuse after the demo (the traces are kept):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\cts-dps\deploy\observability\stop-langfuse.ps1
```

## 7. For the architecture team

- **Standard:** OpenTelemetry. DPS is not tied to Langfuse; production can send the same traces to the bank's platform (Grafana Tempo, Azure Monitor, Dynatrace ...) by changing the endpoint only.
- **Production layout:** DPS sends to an OpenTelemetry Collector, which forwards traces to Langfuse and/or the bank's platform, and metrics and logs to the bank's platform. Set `OTEL_EXPORTER_OTLP_ENDPOINT` for all three, or the per-signal variables.
- **Code:** `dps/src/dps/ops/observability.py` (setup, spans, ids); `dps/src/dps/agent/gateway.py` (one span per model call); `dps/src/dps/skills/common.py` (one span per agent read); `dps/src/dps/skills/runner.py` (step spans, links into the history); migration `b9d3e6f1a4c2_trace_ids.py`; tests `dps/tests/test_tracing.py`.
- **Research and tool choice:** OpenTelemetry + Langfuse, against Grafana, Arize Phoenix, MLflow and Azure Monitor, with sources. The write-up is kept outside the repository.

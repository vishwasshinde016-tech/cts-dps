# CTS DPS runbook

For the ops team. Every alert in `deploy/alerts/cts-dps-alerts.yaml` has a section here with the same name. The
ops endpoints need the SUPPORT or ADMIN role.

## Where to look

| What | Where |
| --- | --- |
| Queues by pool, DEAD count | `GET /dps/v1/ops/queues` |
| Scaling signals and advice | `GET /dps/v1/ops/scaling` |
| Document Agent on (true in production) | `GET /dps/v1/capabilities` → `llmEnabled` |
| Model tokens and cost per job type | `GET /dps/v1/ops/usage` |
| Jobs waiting for a person (review backlog) | `GET /dps/v1/ops/queues` → `review` |
| QA sample error rate, golden-set candidates | `GET /dps/v1/ops/quality`, `GET /dps/v1/ops/golden-candidates` |
| Audit hash chain | `GET /dps/v1/ops/audit-seals` (`intact` must be true) |
| A job's attempts and error code | `GET /dps/v1/jobs/{id}` |
| A job's trace: each step's time, each AI call (model, tokens, prompt version, failures) | Langfuse (laptop: `http://localhost:3000`) or the bank's OpenTelemetry platform; the job's `trace_id` is on `dps_ai_call` and `dps_trace_event` |
| Which AI call produced a finding | `dps_trace_event` row (`SHOWN`/`FOUND`) → `detail_json.ai_span_id` → the `dps.ai.read` span → its `chat` span = `dps_ai_call.span_id` |

## Alerts and first actions

| Alert | First action |
| --- | --- |
| QueueSlow: oldest interactive job over 5 min | Check `/ops/scaling` for the pool; check worker health (`docker compose ps`); add a LIGHT pair (worker + parser). |
| ScaleOut | Add one replica pair for that pool, up to the limit (LIGHT 8, HEAVY 4 on the shared VM). Remove one when the advice is SCALE_IN for 10 minutes. |
| DeadJob | Open the DLQ (`/ops/queues`, then the job). Read `errorCode`: PARSER_CRASH twice = poison file (ask the DMS to check the file); SPRING_UNAVAILABLE = the Spring app was down. Fix the cause, then `POST /dps/v1/jobs/{id}/retry`. |
| OverrideRate | Open the field's recent reviews (`/ops/golden-candidates`); raise with the pack owner; a pack change goes through a pull request and the golden set. |
| Calibration | Re-run calibration; if still above 0.05, raise the auto-accept threshold in that pack's `pack.yaml` (pull request). |
| Deadlocks | Check long transactions and the claim index (`ix_dps_job_claim`); confirm READ_COMMITTED_SNAPSHOT is ON. |
| DiskFull | Check the DMS retention settings for `derived/`; page images and page models can be rebuilt from the versions. |
| SealLate | Check that a worker holds the scheduler lock (`dps_leader`); restart one worker if none does. |
| ReviewBacklog: over 200 waiting, or one waiting over 24 h | Open `/ops/queues` → `review.byReason`. Many `ENTITY_MISMATCH` / `TYPE_MISMATCH` = wrong documents or wrong cycle data from Spring; tell the Spring team. Many `FIELDS_TO_REVIEW` = ask the review team for more people. |
| AiUnavailable | Documents are read by rules only and sent to review (no job fails). Check the Azure deployment health and quota (429), the key in the vault and `DPS_AI_ALLOWED_HOSTS`; the gateway's breaker closes by itself 30 s after Azure recovers. |
| COVERAGE_GAP on many agreements | The listed duty sentences are not explained by any extracted covenant. Open a few: real undertakings the rules do not know (escrow, subordination, insurance ...) are wording to teach the rules or the skill; noise (board resolutions, arbitration) is a duty-word to exclude in `skills/covenants/pages.py`. |
| SECOND_READER_EMPTY on statements | The AI checker answered nothing twice. Check the Azure deployment and the statement's page text; the figures were not double-checked, so the statement waits for a person. |
| No traces arriving | Check the `OTEL_EXPORTER_OTLP_*` settings of the API and worker (they are read at start), and the collector or Langfuse health (`/api/public/health`). Jobs are not affected: spans are exported in the background and dropped when nothing answers. |
| AiBudget | Check `/ops/usage` for the job type using the most tokens; lower `page_selection.max_pages` or the pack's token budget (pull request), or raise the monthly budget with approval. When the month's budget is spent (`DpsAiMonthlyBudgetSpent`, warning `AI_MONTHLY_BUDGET_SPENT`), documents are read by the rules only and sent to review; this is not an Azure outage and does not open the gateway's breaker. |
| AiRefused | The Azure content filter refused requests (warning `AI_REFUSED`). Those documents are read by the rules only and sent to review; the model itself is up, so the breaker stays closed. Open the job traces (`chat` spans marked failed) to see which document, and ask the Azure owner whether the filter setting fits bank documents. |

## Sizing

`dps.ops.production.capacity(docs_per_hour, heavy_share, light_minutes, heavy_minutes, tokens_per_doc)` gives worker counts
(and Azure tokens per minute per route, from `tokens_per_doc`):

    workers = documents per hour x share x minutes per document / (60 x 0.6)

Worked example (to replace with the bank's volumes): 100 documents at the peak hour, 20% heavy, 2 min / 8 min:
5 LIGHT workers, 5 HEAVY workers (or 2 if heavy work may wait an hour).

## Deploy and roll back

1. The pipeline builds, scans (pip-audit, bandit, semgrep, Trivy) and tags the images; the bank's pipeline signs
   them and keeps the SBOM.
2. `alembic upgrade head` runs first (migrations are expand-then-contract, so the previous image still works on
   the new schema).
3. `docker compose -f deploy/docker-compose.cts-dps.yml up -d` with the new `DPS_IMAGE_TAG`.
4. Smoke test: `GET /health/ready`, `GET /dps/v1/capabilities`, one ANALYZE job on a sample file.
5. Roll back: set `DPS_IMAGE_TAG` to the previous tag and run step 3 again (under 15 minutes).

## Backup and disaster recovery

| Item | Protection | Target (to confirm with the bank) |
| --- | --- | --- |
| DPS schema | Bank SQL Server backups: full daily, log every 15 min | RPO 15 min, RTO 4 h |
| `derived/` | Backed up with the DMS store; rebuildable by re-running jobs | Measured in the DR test |
| Contract, packs, rules | git, tagged per release | — |
| Secrets | Bank vault | — |

DR test (before go-live, then yearly): restore the schema to a new VM, start the stack, replay the event feed from
the last cursor Spring stored, run the smoke test, and check `/ops/audit-seals` reports `intact: true`.

## Chaos expectations

Kill a worker mid-job: its lease runs out (120 s) and another worker finishes the job once (tested in
`tests/test_phase6_ops.py`). Kill the parser mid-parse: the job retries once, a second crash makes it DEAD.
Database failover: workers reconnect; leases protect against double work. Azure outage: the breaker opens, jobs are read by rules only and sent to review, then the agent is used again when Azure recovers. Every job ends exactly once and its event is emitted once.

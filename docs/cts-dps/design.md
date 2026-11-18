# CTS Document Processing Service (DPS) — End-to-End Design

Draft v0.8 · Nov 17, 2026 (Document Agent on Azure GPT, as the architect asked; scaling decision and Java tools) · @vishwas

## Decision, 17 Nov 2026 (later the same day): the architect's agent, on the bank's Azure GPT

An earlier decision that day turned all LLM use off. It is replaced: DPS builds the architect's design (Teams points
4 and 5) on the bank's Azure OpenAI deployments. Rules always read first; the agent proposes; a person decides
what the rules and the agent do not agree on.

| Architect asked | Built |
| --- | --- |
| Python worker invokes the CrewAI flow | Every job runs as a CrewAI Flow (`AgreementIntakeFlow`, `StatementProcessingFlow`, `SkillJobFlow`) |
| 1 agent with multiple skills and tools | One CrewAI `Agent`, the **Document Agent** (`dps/agent/document_agent.py`), built fresh for each read: no memory, no delegation |
| Per job: a small subset of skills, tools and prompt instructions | The job's skill pack gives the role, `prompt.md`, the answer schema, the tool list and limits (`max_tool_calls`, `max_model_turns`, `max_seconds`, token budget) |
| Tools | Page tools over the job's selected pages (`page_text`, `search_text`, `find_definition`) and Java business rules in the Spring app (`covenant_library`, `validate_formula`, `lookup_identifier`), batched |
| AI function 1: classify and identify | Rules decide; the agent gives a second opinion; disagreement → `TYPE_DISPUTED`, a person confirms. Image-only documents: the vision route |
| AI function 2: extract covenants | Rules + Document Agent (covenant skill); matched by metric; agent-only covenants → `POSSIBLE_COVENANT` for a person |
| AI function 3: extract schedule terms | Rules + Document Agent (schedule skill) |
| Amendment | Diff against the approved covenants; the agent explains each change with its clause |
| Data quality, confidence | Grounding (quote must be on the page), vote of rules and agent, Java formula check, calibrated scorer with the agent signals |
| Model | Through the DPS model gateway: routes `small`, `strong`, `vision` (`deploy/config/routes.example.yaml`); retries, circuit breaker, rate limits, per-job and monthly token budgets, masking, call log without prompt text |
| Safety | Only allowed hosts (`DPS_AI_ALLOWED_HOSTS`); only the selected pages; prompt-injection guard; CrewAI telemetry off |
| Outage | Model down or budget used → rules only, `AI_UNAVAILABLE` / `AI_BUDGET_EXCEEDED`, fields to review; the job never fails because of the model |

Everything else in this design (DMS vs DPS, jobs and events, queue, sandbox, page model, schemas, Java tools,
confidence routing, review, operations, rollout) is unchanged.

## Summary

CTS has two document components, and this doc designs both. The **DMS** (the architect's Document Management System: upload, safety, immutable storage, versions, links, approval, evidence) runs inside the Spring app, as the architect's Miro shows. The **DPS** (Document Processing Service) is a separate Python service that reads accepted versions with AI and returns checked, cited, scored data. They talk only through jobs and events. The DMS design is in its own tab; the rest of this doc is the DPS.

**DMS and DPS are different (rechecked 17 Nov):** the Word doc defines the DMS and calls the AI part a downstream extraction pipeline; the Miro colours the DMS steps green (Spring App) and the AI functions blue (AI Flow, Python worker). Decided 17 Nov: a separate team in our organisation (the DMS team) codes the DMS in the Spring app from the DMS tab; this team builds the DPS.

The decisions this design proposes:

1. **Two components.** The DMS (in the Spring app) owns upload, safety, storage, versions, links, approval and evidence; the DPS owns AI reading. They meet at two points: a fast *pre-acceptance analysis* (what is this file, which borrower, facility and period does it name) and a full *extraction* after the version is committed.
2. **A workflow with one agent inside, not a free agent.** A deterministic CrewAI Flow runs the steps; one Document Agent with a small skill pack per job type does the reading. Banks need the same input to give the same path, and every step to be explainable.
3. **Rules first, AI second, a second model as a checker.** Every value must be found on its page. Two readers that disagree send the field to a person. A null is better than a wrong value.
4. **Multi-modal and multi-model.** Text, tables, scans and images are read: native text first, OCR for scans, a vision model for pages that text cannot read. Two different model families check each other.
5. **One result schema for every document type**, versioned, with page, quote, location, method, model and confidence on every field.
6. **Jobs in SQL Server with leases**, owned by DPS, submitted through a DPS API (not by Spring writing into DPS tables), results pushed back as events through an outbox.
7. **Python where the libraries are, Java where the rules are.** Parsing, OCR and the agent stay in Python; formula, covenant-library and master-data rules stay in Spring and are called as batched tool calls with timeouts, a circuit breaker and fallback to review.
8. **Production from day one:** error taxonomy with retry and dead-letter queue, OpenTelemetry tracing, append-only hash-chained audit, a golden set that gates every model or prompt change.

Decided (17 November 2026): the AI runs on Azure endpoints the bank provides. Files stay on premises; only the selected page text or page images of a job are sent to Azure, through a private endpoint. DPS stays model-agnostic behind its gateway, so a later change needs only the route file.

## What the inputs ask for

Three inputs were read in full: the architect's *01 - Document Upload* spec (with its one comment), the Onboarding and Amendment Miro flows, and the architect's spoken critical points.

**From the upload spec (DMS):**

- Never overwrite, rename or move an accepted file; each accepted upload gets an immutable `document_version_id`, a server-side SHA-256 and a fixed object key.
- One original, many links (borrower, facilities, agreements, covenants, periods, cycles); evidence used by a completed cycle is frozen; restatements make new runs, never edits.
- Tenant, user, role and borrower/facility scope come from the server, never from a filename, a client field or an AI suggestion.
- Local-first: bytes and authoritative metadata on premises; SharePoint only an optional adapter.
- Storage layout: `staging/`, `quarantine/`, `blobs/<tenant>/<hash2>/<id>`, **`derived/<tenant>/<version-id>/` for OCR, extraction and index artefacts** (this is where DPS output belongs), `audit-seals/` daily hash chain.
- Upload sequence: authenticate → bounded staging (size, count, MIME allowlist PDF/XLSX/DOCX/CSV, checksum on stream) → content and security checks (magic bytes, broken PDF, macros, encryption, malware, archive bombs; *comment: "Take this later"*) → **structural/semantic validation: document type, legibility, key pages, signature, dates, borrower and facility identifiers, period, currency and units, issuer** → exact duplicate check → decision (commit / MAPPING\_REVIEW / reject or quarantine) → **async extraction only for vetted versions; human-reviewed extraction governs covenant rules; deterministic calculation governs numbers**.
- Mapping precedence: (1) facility chosen on an authorised screen, (2) exact facility ID or loan account number in the document or manifest, (3) AI/OCR classification or fuzzy name match, shown as a suggestion with confidence and evidence, never assigned without a person.
- Five flows: onboarding upload from the Facility screen; from the Borrower screen (to be discussed); assessment upload incl. restatement; bulk upload with a CSV manifest and per-file outcomes `SUCCEEDED / PENDING_MAPPING / DUPLICATE / REJECTED / FAILED_RETRYABLE`; cycle outcome creation where *AI may draft commentary but never approves compliance or breach*.
- Open in the spec: Flow 2 and the roles who manage documents.

**From the Miro flows (Onboarding, Amendment):** the user searches a CP/borrower; existing staged covenants are reviewed; otherwise the document is looked up in the DMS or uploaded (format, checksum, virus scan, re-upload on failure); a duplicate running job shows "Covenant extraction is in progress"; a row goes into the Onboarding Job table; a Python worker runs the CrewAI flow with three AI functions — **classify and identify (Sanction, Loan Agreement, Amendment), extract covenants, extract schedule terms**; success calls an API that saves covenants for review, failure goes to the Exception Flow; the approved covenants are persisted as immutable and the schedule is updated.

**From the architect (critical points):** Spring and AI app on the same VM; Spring ↔ AI only through jobs; the Python worker continuously polls the job queue; each job runs an AI flow in an isolated context with a small subset of skills, tools and prompt instructions; one agent with multiple skills and tools; and five critical concerns — the schema of the extraction tool, PDF/Excel/Word with text or images, data quality and confidence, agent runtime and scalability, logging and the exception flow.

**Where the inputs disagree** (to settle with the architect):

| Topic | Upload spec | Miro flow | Proposal |
| --- | --- | --- | --- |
| When extraction starts | After the version is vetted, and for onboarding after approval and activation | Right after upload checks | Pre-acceptance analysis at upload; full extraction once the version is committed; results usable only after the document is approved |
| Document type | Chosen by the user, then validated | Classified by AI | User's choice is the expectation; DPS classifies and flags a mismatch |
| Job table | Durable job + item queues | "Onboarding Job table" written by Spring | DPS-owned job tables behind a DPS submit API |
| Covenants "immutable" | Versions, supersede, restate | Persisted as immutable | Immutable per version; an amendment makes a new version that supersedes, never an edit |

## Review of the architect's critical decisions

All five points are kept; four of them get changes so they do not hurt later.

| # | Decision | Verdict | Why, and what changes |
| --- | --- | --- | --- |
| 1 | Spring app and AI app on the same VM | Keep for phase 1, with guard rails | Fine for cost and latency. Risks: OCR, Docling and vision models take CPU and memory in bursts and can starve Spring; DPS parses untrusted files, so a parser exploit lands next to Spring. Guard rails: separate containers with CPU and memory limits, separate OS users, DPS reaches Spring only through its API and the shared database only through its own schema, no shared local folder. Then moving DPS to its own VM or a GPU box is a config change, not a rewrite. |
| 2 | Spring ↔ AI only through jobs | Keep | Durable, retryable, survives restarts, no lost uploads. Make it explicit: Spring submits through `POST /dps/v1/jobs` (DPS writes the job row in the same transaction, checks idempotency and input), DPS reports through an outbox event feed. Spring writing straight into a DPS table couples two teams to one table layout and skips validation. If the architect insists on the table, it must be a DPS-owned *inbox* table with a frozen contract. |
| 3 | Python worker continuously polls the queue | Keep, refined | Polling SQL Server is proven (`UPDLOCK, READPAST, ROWLOCK`, or the compare-and-set claim DPS already uses). Add: lease plus heartbeat so a dead worker's job comes back; idle back-off with jitter (0.5 s busy → up to 5 s idle) so polling does not load the database; priority; per-tenant fairness; attempt limit then dead-letter queue. |
| 4 | Isolated execution context per job, small subset of skills, tools and prompts | Keep, strengthen | Least privilege is the best defence against prompt injection from a document. Add process isolation too: parsing runs in a child process with a time limit, a memory limit, a temp folder wiped after, and no network at all. The agent's model calls and lookups go through the worker, which allows only the model gateway and Spring's read-only lookups. A crash or a hostile file kills one job, not the worker. |
| 5 | One agent with multiple skills and tools | Keep, but inside a workflow | One agent avoids the 3–10× token cost and hand-off errors of multi-agent setups. But the agent must not decide the route: the steps of a covenant extraction are known, so a deterministic Flow calls the agent at fixed points (classify, extract, verify) and code persists results. The agent proposes; it has no write tool. |

**"Multi-model agent"** — both readings belong in the design: *multi-modal* (reads text, tables and page images, so scans, stamps, signatures and charts are covered) and *multi-model* (a second model from another family checks the first, and a small fast model does classification while a strong model does extraction).

**The five critical concerns** are each answered in their own section below: extraction schema, formats and images, data quality and confidence, runtime and scalability, logging and exception flow.

## Boundaries: who owns what

The DMS owns the bytes and their life, the DPS owns their meaning, Spring owns business decisions and the UI owns screens. Each piece below has exactly one owner.

| Responsibility | Owner | Notes |
| --- | --- | --- |
| Login, user, role, borrower and facility scope | Spring (gateway, `X-User`) | Passed to DPS on every job; DPS never trusts a filename or the document for scope |
| Upload, staging, size and type limits, checksum on stream | **DMS** (Spring app) | UI uploads to the Spring app |
| Virus scan, macros, active content, archive bombs, quarantine | **DMS** | ClamAV + inspection rules; DPS inspects again before parsing (defence in depth) |
| Immutable store, `document_version_id`, SHA-256, object keys | **DMS** | Write-once; DPS has read-only access to `blobs/` |
| `derived/` artefacts (OCR, page model, results) | **DPS** writes, DMS stores | Write-once per run |
| Document type, legibility, identifiers, period, currency (pre-acceptance) | **DPS** (`ANALYZE` job) | Submitted by the DMS before commit |
| Mapping precedence, mapping review queue | **DMS** | Uses DPS's analysis as evidence, never as authority |
| Duplicates, families, versions, supersede, activation, retirement | **DMS** |  |
| Approval of a document version (maker-checker) | **DMS** | Roles still to be agreed (open in the spec) |
| Covenant, schedule, statement, certificate extraction | **DPS** | Started by the DMS after commit |
| Review of extracted fields | **DPS** stores; UI + Spring show and send decisions | One review step for the user |
| Covenant rules, schedules, applying covenants, assessments | Spring | Receives DPS proposals |
| Calculations, compliance, breach | Spring (deterministic engine) | DPS never decides a breach |
| Cycle outcome freezing, report files, restatement chain | **DMS** |  |
| Commentary drafts | **DPS** | Draft only; a person approves |
| Audit trail and daily seal | **DMS** and **DPS** | Each keeps a hash-chained trail; both feed the daily seal |
| Retention, legal hold, purge | **DMS** | Purge only by a controlled root-level job |

Why two components: it is how the architect drew it (DMS steps in the Spring App, AI in a separate Python worker), and it separates two kinds of risk. The record-keeping DMS must change rarely and be trusted; the AI changes monthly. DPS can only read accepted bytes and write `derived/`, so a model or prompt change can never touch stored evidence. They communicate only through jobs (DMS → DPS) and events (DPS → Spring), the architect's "communication via job" decision.

## Fit with the bank app (covenant-monitoring)

DPS attaches to the existing `covenant-monitoring` app (Spring Boot 3.3, Java 17, React 18, SQL Server with Flyway, H2 in dev); reading its code on 17 November 2026 showed 12 places where this design had to change or be agreed, listed here with the change made.

| # | What the bank app does today | Effect on DPS | Change made in this design |
| --- | --- | --- | --- |
| 1 | No Keycloak or JWT; the acting user arrives in an `X-User` header from a gateway; ops calls use an `X-Support-Token` | Service-token design assumed an identity provider | Auth adapter with two modes: bank identity provider tokens, or (same VM) a shared service token + network restriction, with the acting user in `X-User`. Mode set per environment |
| 2 | No tenant concept | Multi-tenant columns had no source | `tenant_id` kept with one default value, so a second tenant later needs no migration; `TENANT_MISMATCH` only active when tenants exist |
| 3 | Everything is keyed by `cpId` (credit proposal) and obligor id; the DMS spec says borrower and facility | Job context could not be mapped | Job context carries `cp_ids` and `obligor_id` beside `borrower_id` and `facility_ids`; mapping to confirm (question 11) |
| 4 | Files go through the backend to an external file-upload service: `POST /api/v1/files` → `fileId`; `download(fileId)` | DPS cannot read a DMS object key that does not exist yet | Decided 17 Nov: the DMS is built new inside the Spring app and DPS reads only from its store; no old files are migrated |
| 5 | `AiStatusRecommender` calls an external AI service, `POST /api/v1/ai/covenant-status` with `cpId`, `certificateFileIds`, covenants and `attachmentFileIds`, 10 s timeout | DPS can be that service, but the call is synchronous | DPS serves this endpoint unchanged, answering in ≤ 10 s from results it has already extracted (certificates, evidence); never parses a file inside the call; returns an empty list when nothing is ready, which the app already treats as "AI unavailable" |
| 6 | Measure values come from external sources through `POST {baseUrl}/api/data/values` (`cpId`, `from`, `to`, `keys` → dated points, `sourceChangedAt`) | Statement figures need a way into assessments | DPS serves the same contract from **approved** statement figures, so the bank registers DPS as one more data source; no new Spring code for values |
| 7 | Covenants live in `cov_covenant_master`, conditions in the app's formula DSL, checked by `POST /api/v1/formulas/validate` | DPS proposals must be in that form | Covenant fields carry `library_match` (master id) and the DSL condition, validated through that endpoint by the `validate_formula` tool |
| 8 | Errors use the envelope `{code, message, details, timestamp}` | RFC 9457 was planned | DPS uses the same envelope, so the Spring client parses one format |
| 9 | Rule: every application table in its database starts with `cov_`; Flyway owns its schema with `ddl-auto: validate` | `dps_` tables and Alembic in the same schema would break the rule | All DPS tables live in a SQL Server schema `dps`; the database is only a connection setting. Development uses the local database now. Production (separate database `cts_dps`, recommended, or the bank app's database) is decided later with no code change; the bank app's database would need `READ_COMMITTED_SNAPSHOT` on |
| 10 | Quality gates: JaCoCo 95% lines / 90% branches, SpotBugs, OWASP check, SonarQube | DPS must meet the same bar | Python gates set to match: coverage ≥ 95% / 90%, bandit, pip-audit, SonarQube |
| 11 | WebClient + resilience4j, 3–10 s timeouts, graceful fallback | Long work cannot be a synchronous call | Confirms the job API for extraction; only the two compatibility endpoints are synchronous and fast |
| 12 | Quartz schedules, notifications, workflow cases are in Spring | Must not be duplicated | DPS owns none of them; its events only tell Spring when to act |

**Two compatibility endpoints** are therefore added to the catalogue (26 in total): `POST /api/v1/ai/covenant-status` and `POST /api/data/values`, both read-only over approved DPS results.

**Questions from this check:** 11. Is a CP the same as a facility in the DMS, and is the obligor the borrower? 12. *Answered:* the DMS is new, no migration. 13. Which identity provider and gateway sit in front of the bank app in production? 14. *Deferred:* local database for development; production database chosen later (separate `cts_dps` recommended).

## Architecture

DPS is two processes on top of its own SQL Server schema: a FastAPI service that accepts jobs and serves results, and a pool of Python workers that claim jobs and run one Flow each. The DMS is not one of them: it runs inside the Spring app.

&#91;embedded content: DPS architecture · Spring submits jobs, workers process, events go back\]

The Spring app, including its DMS, is the only caller; workers read files from the DMS store and write their artefacts to `derived/`; only the gateway talks to models.

- **Deployment on the shared VM (phase 1):** containers `dps-api`, `dps-worker` (LIGHT, 2–4 replicas), `dps-worker-heavy` (1–2 replicas), each with CPU and memory limits so Spring keeps its share; own OS user; own database login with rights on the DPS schema only.
- **Isolation per job:** parsing runs in a child process with a time limit, memory limit, its own temp folder and no network; the agent step runs in the worker, whose only outbound calls are the model gateway and Spring's read-only lookups; a child crash fails one job, never the worker.
- **Scaling:** workers are stateless, so more replicas = more throughput; heavy and AI work scale separately; the model gateway caps concurrent calls per deployment; later the heavy pool can move to a GPU host without code changes.
- **No shared local disk:** every exchange goes through the database, the object store or HTTP, so any component can move to another machine.

## Scaling and the Java tools boundary

Decided 17 Nov 2026, after the architect's review ("the module should be scalable; all Python may not be scalable; implement in Java and wrap in Python as a tool call").

**Verdict: hybrid (Option B).** Python keeps parsing, OCR and the AI agent. Java, inside the bank's Spring app, keeps the business rules. The agent calls those rules as batched tool calls.

**Why Python is not the bottleneck.** A DPS job spends its time in three places: native parsers (pdfium, qpdf and ONNX OCR run as C/C++ code, not as Python), Azure model calls (network wait), and the database. Python code only joins these steps. Moving it to Java would not make a page parse faster or a model answer sooner. It would make the team rewrite libraries that have no Java equal (CrewAI, RapidOCR, Docling, pdfplumber).

**Why some parts belong in Java.** The rules for formulas, the covenant library, master data and calculations already live in the Spring app. A Python copy of them would drift out of step. So DPS calls them; it never re-implements them.

### Option A vs Option B

| | Option A: all Python | Option B: Python DPS + Java tools (chosen) |
| --- | --- | --- |
| Speed of parsing and AI | Same: native libraries and network-bound model calls | Same |
| Business rules | Copied into Python and drift from Spring | One copy, in Java, called as a tool |
| Libraries | Full Python document and AI stack | Same, plus the Java rules the bank already has |
| Scaling | Worker processes, scale by replicas | Same, plus Spring scales its tool endpoints |
| Risk: two languages | None | Contract-first (`contracts/spring-tools.yaml`) and contract tests on both sides |
| Risk: coupling to Spring | None | Timeouts, retries, circuit breaker, fallback to review |
| Risk: chatty calls | — | One batched call per document, never one per field |

Option A is worse because it duplicates the bank's rules. A Java-only DPS (the opposite extreme) is worse still: it loses the document and AI libraries, and it would not run faster.

### Concurrency model

- **One job per worker process.** No threads share a job. A child process does the parsing with a time limit, a memory limit and no network; a crash fails one job, never the worker.
- **Two pools.** LIGHT workers (native text, CSV, Excel, Word; about 1 GB each) and HEAVY workers (OCR, Docling, vision; about 3–4 GB each). Each pool claims only its own jobs, so a big scan never blocks a small statement.
- **Model calls are capped** per Azure deployment by the gateway, so more workers never means more 429 errors.
- **Stateless workers.** Every hand-off goes through the database, the object store or HTTP, so a replica can start or stop at any time.

### Autoscaling rules

| Signal | Scale out | Scale in |
| --- | --- | --- |
| Oldest QUEUED job age, per pool | > 60 s for 2 min: +1 replica | < 10 s for 10 min: −1 replica |
| Queue depth per running worker | > 5 | < 1 |
| Azure 429 rate | — | > 5%: stop scaling out; the gateway backs off |
| Limits | LIGHT 2–8, HEAVY 1–4 on the shared VM | Never below the minimum |

On the shared VM (phase 1) the limits are fixed replica counts with CPU and memory caps; the same signals drive autoscaling when DPS moves to the bank's container platform.

### Time and memory budget per stage

| Stage | Target time | Memory | Pool |
| --- | --- | --- | --- |
| Inspect (safety) | < 1 s per file | 200 MB | LIGHT |
| Native text page model | < 0.2 s per page | 500 MB | LIGHT |
| OCR page model | < 3 s per page | 2 GB | HEAVY |
| Classify | < 5 s per document | — | LIGHT |
| Agent extraction | < 60 s per document | — | LIGHT / HEAVY |
| Java tool calls | < 5 s per batch, ≤ 200 items | — | Spring |
| Whole job, 50-page native PDF | < 3 min (p95) | — | — |

The Phase 6 load test checks these numbers on the bank's real document mix. Where a stage misses its budget, that stage gets its own pool or a faster tool; the rest is not rewritten.

### Java tools contract

`contracts/spring-tools.yaml` defines three read-only tools the Spring team adds as thin wrappers:

| Tool | Endpoint | Wraps |
| --- | --- | --- |
| Formula validation (batch) | `POST /api/v1/dps-tools/formulas/validate` | Existing `POST /api/v1/formulas/validate` (`ValidateFormulaRequest` / `ValidateFormulaResponse`) |
| Identifier lookup (batch) | `POST /api/v1/dps-tools/lookups` | Credit-proposal / facility / borrower master data: facility id, loan account, CIN, PAN, GSTIN, LEI, name |
| Covenant library | `GET /api/v1/dps-tools/covenant-library` (ETag) | Covenant master library |

Rules for every tool call (built in `dps/src/dps/spring_tools.py`):

1. **Batched:** one call per document, up to 200 items.
2. **Bounded:** 5 s timeout, two retries with back-off on timeout or 5xx.
3. **Circuit breaker:** opens after 5 failures in 60 s, tries one call again after 30 s.
4. **Degrades, never fails:** when Spring cannot answer, the affected fields go to review with `SPRING_UNAVAILABLE`; the job finishes.
5. **Read-only:** no tool changes data in Spring.
6. **Until the batch endpoint ships**, DPS calls the existing single-item endpoint with at most 4 calls in parallel.

### Schemas completed (17 Nov 2026)

- `result.json`: each field's `value` is typed by its group (covenant, statement line, schedule term, certified figure, agreement term, identifier); `classification` and `identifiers` are typed.
- `page-model.json`: tables (cells with row and column spans and positions) and spreadsheets (sheets, cells, formulas, formats).
- New: `review.json`, `review-decisions.json`, `compat-data-values.json`, `compat-covenant-status.json`. The OpenAPI file uses all of them.

## Job model

Every piece of work is a job row in DPS's own SQL Server schema. Jobs are idempotent: `ANALYZE` on `(tenant, sha256, pipeline_version)`, because a staged file has no version id yet; extraction jobs on `(tenant, document_version_id, job_type, pipeline_version)`. A replayed request returns the first job; only `REPROCESS`, with a reason, runs the same input again. The page model built by `ANALYZE` is kept by SHA-256 and reused by the extraction job, so a file is parsed once.

**Job types**

| Job type | Started by | Input | Output | Target time |
| --- | --- | --- | --- | --- |
| `ANALYZE` (pre-acceptance) | DMS, after security checks, before commit | Staged file ref, SHA-256, user's chosen type, screen context | Type + confidence, identifiers, period, currency, units, legibility, signature hints, page count, language | under 30 s for a 50-page native PDF; under 3 min for a scan (the upload screen shows "checking") |
| `EXTRACT_AGREEMENT` | DMS/Spring, after commit | Version id, facility context, covenant library version | Covenants, definitions used, testing schedule, cure periods | minutes |
| `EXTRACT_AMENDMENT` | same | Version id + the version it amends | Changed, added and removed covenants against the base, the effective date, and the full covenant set as amended | minutes |
| `EXTRACT_SANCTION` | same | Version id | Sanctioned terms, covenants, conditions | minutes |
| `EXTRACT_STATEMENT` | same | Version id, period, basis (standalone or consolidated), currency, units | Line items with cell or page references, period, units; for a restatement, the figures that changed against the approved version of the same period | minutes |
| `EXTRACT_CERTIFICATE` | same | Version id, cycle | Certified figures, signatory, date, declarations | minutes |
| `DRAFT_COMMENTARY` | Spring (Flow 5) | Outcome manifest, figures, results | Draft text with citations | seconds |
| `REPROCESS` | Ops or Spring | Version id + reason | New run beside the old one; nothing overwritten | as the type |

A **batch** (Flow 4 bulk upload) is a parent row with one child job per file: per-file transactions, an isolated failure does not stop the rest, and a rerun takes only failed or retryable items. Each file's analysis is checked against its manifest row (borrower, facility, type, period); a mismatch makes that file PENDING\_MAPPING, never a forced assignment.

**States:** `QUEUED → RUNNING(stage) → SUCCEEDED | NEEDS_REVIEW | FAILED | CANCELLED`, plus `RETRY_WAIT` for transient errors and `DEAD` (dead-letter) when attempts run out. `NEEDS_REVIEW → RUNNING` again when a person submits the review (resume, not restart: the flow state is checkpointed per stage).

**Queue mechanics**

- Claim: one atomic statement takes the oldest due job of the highest priority for this pool, skipping locked rows (`UPDLOCK, READPAST, ROWLOCK`, or compare-and-set on status), sets `locked_by`, `locked_until` and `attempts + 1`.
- Lease and heartbeat: lease 120 s, renewed every 30 s while the stage runs; an expired lease makes the job claimable again.
- Pools: `LIGHT` (native text), `HEAVY` (OCR, layout models, vision), `AI` model calls capped by a token bucket per deployment, shared by both pools. A job moves pools by re-queuing, never by blocking a worker.
- Priority: 1 interactive upload, 2 amendment, 3 assessment, 4 bulk, 5 reprocess. Starvation guard: a job's effective priority rises with age.
- Fairness: at most N running jobs per tenant and per batch, so one bulk import cannot block an RM's single upload.
- Retries: exponential back-off with jitter (10 s, 40 s, 160 s, max 15 min), max 5 attempts for transient errors only; deterministic outcomes (unreadable, wrong type) never retry.
- Cancel: a flag the running stage checks between steps; uploads that are withdrawn in the DMS cancel their jobs.
- Back-pressure: the submit API returns `429` with `Retry-After` when the queue for that tenant is above its limit.
- Indexes: `(pool, status, priority, due_at)` so the claim never scans and never escalates to a table lock.

## Processing pipeline

Every document job runs the same ten stages (DRAFT\_COMMENTARY reads no file and skips stages 1–6); job types differ only in the skill pack used at stages 6–8. Each stage checkpoints, so a retry or a resume starts at the failed stage, not at the beginning.

| # | Stage | What happens | Main tools | Fails to |
| --- | --- | --- | --- | --- |
| 1 | Fetch | Read the version from the DMS store by object key; re-compute SHA-256 and compare | storage client | `INTEGRITY_MISMATCH` → DEAD, alert |
| 2 | Inspect | Magic bytes vs extension; encrypted, password-protected, truncated or repaired PDF; JavaScript, launch actions, embedded files; macros in Office files; page count and size limits; zip-bomb ratio for XLSX/DOCX | pikepdf/qpdf, oletools, zipfile limits | `UNSAFE_CONTENT` (quarantine request to DMS), `ENCRYPTED`, `CORRUPT` |
| 3 | Parse | One parser per format into one **page model**: PDF native text with positions; scan pages by OCR; layout and tables by a layout model when the fast parse is poor; XLSX sheets and cells with addresses; DOCX paragraphs, tables, tracked changes; CSV rows | pypdf/pdfplumber, RapidOCR or PaddleOCR, Docling, openpyxl, python-docx | `NO_TEXT` after OCR → FAILED with reason; low quality → HEAVY pool |
| 4 | Segment | Split packs into logical documents by page range (agreement + schedules + certificate in one PDF); find key pages (covenant section, statements, signature page) | rules + agent skill | Uncertain split → review flag |
| 5 | Classify | Rules score first (titles, parties, clause words, statement headings, sheet names); if unsure, a small model on the first pages and headings; if the page is an image, a vision model on the page image. Compare with the user's chosen type | rules, LLM, VLM | Mismatch → `TYPE_MISMATCH` review |
| 6 | Identify | Borrower names, facility IDs, loan account numbers, dates, period end, currency, units (lakh, crore, million, thousand), consolidated vs standalone, issuer, signature present | regex + agent skill + master-data lookup through Spring | Contradiction with the screen context → `MAPPING_CONFLICT` |
| 7 | Extract | The job type's skill: rules engine first, then the agent reads the relevant pages only and returns the schema | rules + LLM (+ VLM for image pages) | Partial → fields flagged |
| 8 | Verify | Grounding (every value and quote found on its cited page), cross-checks (balance sheet balances, totals add up, ratio inside a sane range, threshold direction matches wording, period matches), compare with last period | verify module | Disagreement → field to review; a value never found on the page is dropped |
| 9 | Score and route | Multi-signal confidence per field and per document; route by thresholds set per document type and field risk | calibrated scorer | `AUTO_ACCEPTED` / `NEEDS_REVIEW` / `REJECTED` |
| 10 | Publish | Write the result JSON and artefacts to `derived/`; write result rows; write the outbox event in the same transaction | outbox | Spring pulls or receives the event |

Rules that hold across all stages:

- **Only vetted versions are parsed.** Stage 2 runs even though the DMS scanned the file: defence in depth.
- **The model never sees more than it needs:** relevant pages only, wrapped as data, with injection signs checked before the call.
- **A value that cannot be found on its page never becomes a result.** A null with a reason beats a guess.
- **Numbers are never computed by a model.** Ratios, totals and compliance stay in deterministic code in Spring.
- **Non-English documents** are read by the model and checked on the page in the original script; without a model they wait for a person.

## Agent design

One Document Agent, built fresh for each job with only that job type's skill pack; the Flow decides the order of steps, the agent only reads and proposes.

**Skill pack = the isolated execution context.** Each pack is a versioned folder: prompt instructions, the output schema, the tools allowed, the model route, the confidence thresholds, and its golden-set cases. The agent for a statement job cannot see agreement tools or prompts, and no pack has a write or network tool.

| Skill pack | Used by | Tools allowed | Model route |
| --- | --- | --- | --- |
| `classify` | ANALYZE | `page_text`, `page_image`, `headings` | small fast model; vision model if pages are images |
| `identify` | ANALYZE, all EXTRACT | `page_text`, `search_text`, `lookup_borrower`, `lookup_facility` (read-only, through Spring, tenant-scoped) | small model |
| `covenants` | EXTRACT\_AGREEMENT, \_SANCTION | `page_text`, `search_text`, `find_definition`, `covenant_library`, `validate_formula` | strong model |
| `amendment_diff` | EXTRACT\_AMENDMENT | the above + `base_version_terms` | strong model |
| `schedule_terms` | agreement, sanction, amendment | `page_text`, `search_text`, `calendar_rules` | strong model |
| `statement_lines` | EXTRACT\_STATEMENT | `page_text`, `table`, `sheet_cells`, `label_memory`, `page_image` | strong model + vision for scanned tables |
| `certificate` | EXTRACT\_CERTIFICATE | `page_text`, `table`, `page_image` (signature, stamp) | strong model + vision |
| `commentary` | DRAFT\_COMMENTARY | `outcome_manifest`, `cited_figures` | strong model |

**Why one agent, not many:** the steps share one context (the document), hand-offs between agents lose detail, and multi-agent setups cost several times the tokens for the same task. A second agent would only pay off for very long documents read in parallel; DPS gets that cheaper by reading only the relevant pages.

**Multi-model, multi-modal:**

- A **model gateway** (one Python client, LiteLLM-style routing) maps a route name to a deployment: `small`, `strong`, `vision`. Changing a model is a config change, logged and gated by the golden set.
- **No challenger (decision 18 Nov 2026):** there is no second-model re-read. Rules and the Document Agent vote; a disagreement, an agent-only value on a high-risk field and every agent-only covenant go to a person.
- **Vision** is used where text is weak: scanned pages with poor OCR, tables drawn as images, stamps and signatures, charts. Its numbers go through the same grounding check against OCR text of the region.
- **Deployment options** (bank to choose): Azure OpenAI with a private endpoint in an Indian region and no data retention, or an on-premises open-weight model served with vLLM on a GPU host. The gateway hides the difference.

**Guard rails on every call:** document text sent as data, not instructions; injection phrases detected before the call; output must validate against the JSON schema (one repair retry, then the field is null with a reason); a timeout, a token budget per job and per tenant per day; responses cached by `(content hash, skill version, model version)`; every call logged with prompt hash, model version, tokens and cost.

## Extraction schema

Two schemas, both JSON Schema files versioned with semantic versions and published by `GET /dps/v1/schemas`: the **page model** every parser produces, and the **result** every job returns. A breaking change bumps the major version and both versions are served until Spring moves.

**Page model** (one per document version, stored in `derived/`): what the parsers saw, so any result can be traced and any run replayed without parsing again.

```json
{
  "schema": "dps.page-model/1.0",
  "document_version_id": "dv_01J...", "sha256": "9f2c...",
  "format": "PDF", "page_count": 48, "language": "en",
  "pages": [{
    "no": 12, "width": 595, "height": 842,
    "method": "NATIVE | OCR | LAYOUT | VISION", "quality": 0.94,
    "blocks": [{"id": "p12-b7", "kind": "PARAGRAPH | HEADING | TABLE | CELL | IMAGE",
                "text": "...", "bbox": [72, 410, 523, 468], "ocr_confidence": null}],
    "tables": [{"id": "p12-t1", "rows": 14, "cols": 4, "cells": [{"r": 0, "c": 1, "text": "FY2026", "bbox": [...]}]}]
  }],
  "sheets": [{"name": "BS", "cells": [{"ref": "C14", "value": 1520.4, "formula": "=C12+C13"}]}],
  "segments": [{"from_page": 1, "to_page": 41, "type": "LOAN_AGREEMENT"}, {"from_page": 42, "to_page": 48, "type": "SCHEDULE"}]
}
```

**Result** (one per run):

```json
{
  "schema": "dps.result/1.0",
  "run_id": "run_01J...", "job_id": "job_01J...", "job_type": "EXTRACT_AGREEMENT",
  "document_version_id": "dv_01J...", "sha256": "9f2c...",
  "pipeline": {"version": "2.3.0", "skill_pack": "covenants@1.4.2",
               "models": {"strong": "model-a@2026-09"}},
  "classification": {"type": "LOAN_AGREEMENT", "expected": "LOAN_AGREEMENT", "confidence": 0.97,
                     "evidence": [{"page": 1, "quote": "FACILITY AGREEMENT dated ..."}]},
  "identifiers": [{"kind": "FACILITY_ID", "value": "FAC-101", "page": 3, "quote": "...",
                   "match": {"status": "EXACT", "candidate_ids": ["FAC-101"]}}],
  "fields": [{
    "id": "f_7", "group": "FINANCIAL_COVENANT", "name": "DSCR",
    "value": {"operator": ">=", "threshold": 1.25, "unit": "RATIO"},
    "source": {"page": 27, "bbox": [72, 410, 523, 468], "block_id": "p27-b4",
               "quote": "maintain a Debt Service Coverage Ratio of not less than 1.25:1"},
    "test": {"frequency": "QUARTERLY", "basis": "CONSOLIDATED", "first_test_date": "2027-03-31"},
    "method": "RULES+AI_CONFIRMED",
    "confidence": 0.96,
    "signals": {"grounded": true, "rules": true, "ai": true, "agree": 1.0, "parse_quality": 0.98},
    "issues": [],
    "status": "AUTO_ACCEPTED | NEEDS_REVIEW | REJECTED"
  }],
  "document": {"confidence": 0.93, "route": "NEEDS_REVIEW", "reasons": ["2 fields need review"]},
  "warnings": [{"code": "ENTITY_DIFFERS", "field_id": "f_9", "message": "..."}],
  "timing_ms": {"parse": 4210, "extract": 18300, "verify": 6120},
  "usage": {"model_calls": 6, "tokens_in": 41200, "tokens_out": 3100}
}
```

Rules of the contract: every field has a `source` (page + quote, plus `bbox` or a sheet cell reference), so the review screen can highlight it; amounts carry `currency`, `unit` and `scale` as printed and as normalised; dates are ISO; unknown is `null` plus an `issues` entry, never an invented value; enums are closed lists published with the schema; the `issues` codes are the error catalogue codes. Covenant fields also carry library\_match: the matched cov\_covenant\_master id and the condition in the bank app's formula DSL, validated through its POST /api/v1/formulas/validate.

## Confidence and data quality

Confidence is computed by DPS from several measured signals, never taken from what a model says about itself; research and vendor guidance agree that self-reported model confidence is overconfident and that raw token probabilities alone are a weak signal.

**Signals per field**

| Signal | Measures | Example effect |
| --- | --- | --- |
| Grounding | Value and quote found on the cited page or cell | Not found → field dropped |
| Parse quality | Native text vs OCR, OCR confidence of the region | OCR 0.6 → lower score |
| Agreement | Rules and the extractor model agree | They differ → review |
| Consistency | Accounting identities, totals, sign, ratio range, threshold direction vs wording | Balance sheet off by 2% → review |
| History | Same borrower last period, same label learned before | 20× jump → unit check |
| Schema | Required fields present, value types valid | Missing period → review |

**Score and calibration.** A small scorer combines the signals into a 0–1 confidence per field. It is calibrated on the golden set per document type and field, so that "0.95" means about 95 of 100 such fields were right; calibration error is reported on every evaluation run.

**Routing thresholds** are set by the cost of an error, per document type and field risk:

| Field risk | Examples | Auto-accept at | Below that |
| --- | --- | --- | --- |
| High | Financial covenant threshold and operator, statement figures used in a covenant | never auto-accepted in phase 1 | always reviewed; DPS pre-fills and highlights |
| Medium | Test frequency, first test date, cure period, basis | ≥ 0.95 | review |
| Low | Borrower name as printed, document date, signatory | ≥ 0.90 | review |

This matches the upload spec: human-reviewed extraction governs covenant rules. Auto-accept for high-risk fields can be switched on later, per field, once the measured accuracy on live reviews justifies it.

**Document-level quality** is reported too: legibility (share of pages read natively or with good OCR), completeness (expected sections found: covenant section, all three statements, signature page), and the share of fields that needed review. A random 2–5% of auto-accepted fields is also sent to review, to keep measuring the real error rate.

**Learning from reviews:** each correction is stored with the reviewer, the before and after, and the reason. Confirmed wordings and statement labels are remembered per borrower and globally (DPS already does this), and every correction becomes a candidate golden-set case.

## API catalogue

24 endpoints in 8 groups, all under `/dps/v1`, plus 2 compatibility endpoints for the bank app; called by the Spring app (including its DMS) with a service token; the UI never calls DPS directly. The DMS's own API is in the DMS tab. The contract is an OpenAPI 3.1 file in the repo, with contract tests on both sides.

| Group | Method and path | Purpose |
| --- | --- | --- |
| Jobs | `POST /jobs` | Submit one job; `Idempotency-Key` header; returns `202` + `job_id` (or the existing job) |
| Jobs | `GET /jobs/{id}` | Status, current stage, progress, attempts, last error code |
| Jobs | `GET /jobs/{id}/result` | The result JSON (schema `dps.result`) |
| Jobs | `POST /jobs/{id}/cancel` | Cancel a queued or running job |
| Jobs | `POST /jobs/{id}/retry` | Re-queue a FAILED or DEAD job (ops) |
| Jobs | `GET /jobs?status=&type=&tenant=&from=` | List for support screens, paged |
| Batches | `POST /batches` | Bulk: manifest + file refs; returns `batch_id`, one child job per file |
| Batches | `GET /batches/{id}` | Counts by outcome and per-file results |
| Batches | `GET /batches/{id}/errors.csv` | Restricted error CSV: codes and references, no document content |
| Batches | `POST /batches/{id}/rerun` | Re-run only failed or retryable items |
| Review | `GET /jobs/{id}/review` | Fields needing review, with evidence and highlights |
| Review | `POST /jobs/{id}/review` | Confirm, correct or reject fields; resumes the job |
| Review | `POST /jobs/{id}/reclassify` | Reviewer sets the right type; re-runs with that skill pack |
| Evidence | `GET /documents/{versionId}/pages/{n}/image` | Page render for highlighting (no full-file download from DPS) |
| Evidence | `GET /documents/{versionId}/runs` | Every run on a version, for audit and restatement compare |
| Assist | `POST /documents/{versionId}/ask` | Question about an agreement, answer with page citations |
| Events | `GET /events?after={cursor}&limit=` | Event feed for Spring (see Events) |
| Catalogue | `GET /schemas` and `GET /schemas/{name}/{version}` | JSON schemas of page model, result and events |
| Catalogue | `GET /capabilities` | Document types, formats, limits, skill pack and model versions in use |
| Ops | `GET /health/live`, `GET /health/ready` | Liveness; readiness checks the database, storage and model gateway |
| Ops | `GET /ops/queues` | Depth and oldest age per pool and priority |
| Ops | `GET /ops/usage` | Model calls, tokens and cost by tenant, job type and day |

API rules: errors in the bank app's envelope {code, message, details, timestamp} with a stable `code`; `X-Correlation-Id` accepted and echoed, carried into logs and traces; pagination by cursor; rate limits per caller with `429` + `Retry-After`; `ETag` on results; the tenant comes from the service token's claims and the job, never from a query string the UI controls; no endpoint returns the original file.

## Events back to Spring

DPS reports every outcome as an event written to an outbox table in the same transaction as the result, so a result is never saved without its event or announced without being saved. Delivery is at least once; Spring de-duplicates on `event_id`.

Two delivery modes, Spring picks one:

1. **Pull (recommended):** Spring reads `GET /dps/v1/events?after={cursor}` every few seconds and stores its cursor. Nothing is lost while Spring is down, and DPS needs no call into Spring.
2. **Push:** DPS posts each event to a Spring endpoint with retries and back-off; the feed stays available as the fallback.

| Event | When | Payload (no document content) |
| --- | --- | --- |
| `dps.job.accepted` | Job created | job, type, version, priority |
| `dps.analysis.completed` | ANALYZE done | type + confidence, mismatch flag, identifier candidates, period, legibility |
| `dps.extraction.completed` | Result auto-accepted or published after review | job, run, result URL, counts |
| `dps.extraction.needs_review` | At least one field needs a person | job, run, number of fields, reasons |
| `dps.job.failed` | Deterministic failure | job, error code, user message |
| `dps.job.dead` | Retries exhausted | job, error code; also alerts ops |
| `dps.batch.completed` | All items of a batch ended | batch, counts per outcome, error CSV URL |

Each event carries `event_id`, `type`, `occurred_at`, `tenant`, `correlation_id`, `job_id`, `document_version_id` and `schema` version. Spring then calls the result API and does its own business steps (save covenants for review, update the schedule, notify the RM).

## Error handling and exception flow

Every error has a stable code in one catalogue, a class that fixes how it is handled, a message for the user and a detail for support; nothing fails silently and nothing retries forever.

| Class | Examples (codes) | Handling | User sees |
| --- | --- | --- | --- |
| Request | `INVALID_INPUT`, `UNSUPPORTED_FORMAT`, `TOO_MANY_PAGES`, `DUPLICATE_REQUEST` | `4xx` at submit, no job created (duplicate returns the first job) | What to fix |
| Security | `UNSAFE_CONTENT`, `ENCRYPTED`, `MACRO_PRESENT`, `INTEGRITY_MISMATCH` | No parsing; quarantine request to the DMS; ops alert for integrity | "File refused for safety", re-upload a clean copy |
| Content | `NO_TEXT`, `CORRUPT`, `TYPE_MISMATCH`, `NOTHING_FOUND`, `LANGUAGE_NOT_READ` | No retry; FAILED or NEEDS\_REVIEW with the reason | Plain reason and the next step (change type, upload a better scan, enter by hand) |
| Quality | `LOW_CONFIDENCE`, `MODELS_DISAGREE`, `NOT_GROUNDED`, `TOTALS_DO_NOT_ADD`, `MAPPING_CONFLICT`, `ENTITY_DIFFERS` | Field-level flag; job goes to NEEDS\_REVIEW | The field highlighted with the reason |
| Transient | `MODEL_TIMEOUT`, `MODEL_RATE_LIMITED`, `STORAGE_UNAVAILABLE`, `DB_DEADLOCK`, `SPRING_UNAVAILABLE` | Retry with back-off and jitter, max 5; circuit breaker per dependency | "Still processing" |
| Degraded | `AI_UNAVAILABLE` (model down or budget spent) | Rules-only result, every field marked `AI_NOT_RUN`, routed to review; or wait, per tenant setting | "Read without the AI checker, please review" |
| Resource | `PARSE_TIMEOUT`, `OUT_OF_MEMORY` | Move to HEAVY pool once; then FAILED | "Too large or complex", split the file |
| Internal | `INTERNAL_ERROR` | Retry once; then DEAD and alert with the trace id | "Processing failed, support informed" |

**Exception flow** (the red box in the Miro flows): a FAILED or DEAD job emits an event; Spring shows the reason on the document and offers the action the code allows (retry, change type, re-upload, enter manually); ops get a dead-letter queue screen with requeue, a daily DLQ report, and alerts when DLQ size or the oldest queued job crosses a limit. The previous active version and its covenants are never touched by a failed run.

**Specific traps handled:** a worker crash mid-job (lease expiry, resume at the last checkpoint); the same version submitted twice (idempotency key); a model returning invalid JSON (one repair retry, then null field); a model returning a number not on the page (dropped); a poison file that crashes the parser (isolated child process, counted, sent to DEAD after 2 crashes, never blocks the queue); clock skew (all times UTC from the database); a half-written result (result and event in one transaction).

## Logging, observability and audit

Three separate records with three purposes: logs for engineers, traces and metrics for operations, and an append-only audit trail for the bank's auditors.

**Logs:** structured JSON, one line per event, always with `correlation_id`, `trace_id`, `tenant`, `job_id`, `document_version_id`, `stage`, `error_code`. Never document text, figures, borrower names, tokens or passwords: a masking filter enforces it and a test fails the build if a known secret or sample text reaches a log line. Kept 30 days locally (as today) and shipped to the bank's log platform.

**Traces:** OpenTelemetry, one trace per job from Spring's submit through every stage and model call to the event; Spring's correlation id joins both sides.

**Metrics and alerts:**

| Metric | Alert when |
| --- | --- |
| Queue depth and oldest job age per pool | Oldest interactive job over 5 min |
| Stage duration p50/p95 per job type | p95 doubles week on week |
| Outcomes per job type (auto, review, failed, dead) | Failure rate over 5% in an hour; any DEAD |
| Review rate and reviewer override rate per field | Override rate rises: model or document drift |
| Model calls, latency, errors, tokens, cost per tenant | Error rate over 10%; daily budget at 80% |
| Calibration error on sampled reviews | Above the agreed limit |

**Audit trail:** an append-only table, each row holding the hash of the previous row (a hash chain), so a deleted or edited row breaks the chain. Rows are chained in database-sequence order under an exclusive lock on the chain head, so concurrent workers cannot fork the chain. Rows: job submitted, classified, extracted, verified, routed, reviewed (who, what changed, why), reclassified, cancelled, re-run, result read. A daily checkpoint hash is handed to the platform's `audit-seals/`. Each result stores the pipeline, skill pack, prompt and model versions, so an auditor can replay how a value was produced.

## Security and compliance

DPS treats every document as hostile input and every model as an untrusted helper; the controls below follow the OWASP file upload guidance and the RBI FREE-AI principles (explainability, human oversight, board-approved AI policy, model risk management).

- **Identity:** an auth adapter with two modes: the bank's identity provider tokens (short-lived, audience-checked, one client per caller), or on the shared VM a service token plus network restriction with the acting user in X-User, as the bank app works today. Optional mTLS inside the VM network.
- **Tenant isolation:** every row and every derived artefact carries the tenant (one default tenant today, as the bank app has none); every query filters on it; storage paths are tenant-scoped; DPS's storage credential reads blobs/ and writes only to derived/; a test suite tries cross-tenant reads on every endpoint.
- **Untrusted files:** second-line inspection (stage 2), parsing in a sandboxed child process with time and memory limits, no network, temp files wiped; DPS never serves the original file.
- **Prompt injection:** document text is wrapped as data, injection phrases are detected and send the whole job to review; tools are read-only and tenant-scoped, so an injected instruction has nothing to act with.
- **Data residency:** Azure endpoints provided by the bank (decided). Private endpoint, Indian region, prompt retention and abuse-monitoring setting confirmed with the bank, only the relevant pages sent, personal identifiers masked where the task allows.
- **Secrets:** from the bank's vault or environment, never in code, images or logs; rotated; a secret scan runs on every commit.
- **Human oversight:** no covenant, figure or type change becomes binding without a person; AI-drafted commentary is labelled as a draft; AI never decides compliance or breach.
- **Model risk:** a model inventory (each model, version, purpose, owner, validation evidence), change control through the golden set, monitoring of drift and override rates, and a documented fallback when a model is withdrawn.
- **Retention:** derived artefacts follow the source version's retention and legal hold, set by the DMS; DPS purges nothing on its own.

## Quality: golden set and change control

No prompt, model, rule or library change reaches production unless the golden set says it is at least as good as what runs today.

- **Golden set:** real and synthetic documents per type and format (native PDF, scan, XLSX, DOCX, Indian and foreign formats, standalone and consolidated, amendments against their base), each with the correct answer checked by a credit analyst. Target before go-live: at least 30 per document type; grows with every reviewer correction.
- **Held-out set:** documents never used while tuning prompts or rules, to catch over-fitting.
- **Measures per run:** classification accuracy; per-field precision and recall; exact-value match for thresholds and figures; grounding rate; share auto-accepted and its error rate; calibration error; cost and time per document.
- **Gate in CI:** a pull request that changes a skill pack, a prompt, a model route or the parser runs the golden set; a drop in any high-risk field's accuracy blocks the merge.
- **Shadow runs:** a new model runs beside the current one on live traffic for 2 weeks; results are compared, not used.
- **Release record:** each production pipeline version lists its skill packs, prompts, models and golden-set scores, signed off by the model owner (model inventory).

## Tools and libraries

All open source and self-hostable except the model itself; Azure Document Intelligence is not used.

| Need | Choice | Why | Alternative |
| --- | --- | --- | --- |
| Language and API | Python 3.12, FastAPI, Pydantic v2 | Typed schemas, OpenAPI for free; DPS today | — |
| Workflow and agent | CrewAI Flows + one Agent per job | Already in DPS; `@persist` checkpoints; skill packs map to agent config | LangGraph |
| Job store | SQL Server (DPS schema), SQLAlchemy | Bank already runs it; one transaction for result + event | Postgres |
| PDF text | pypdf, pdfplumber, pypdfium2 | Fast native text with positions | PyMuPDF (licence check needed) |
| PDF safety | pikepdf (qpdf) | Detect encryption, JavaScript, actions, embedded files, repair | — |
| Office safety | oletools (olevba), zipfile ratio limits | Macros, DDE, zip bombs | — |
| OCR | RapidOCR (today), PaddleOCR | CPU-friendly, good on printed tables | Tesseract |
| Layout and tables | Docling (heavy pool) | Strong on complex tables, reads DOCX/XLSX too | Marker, MinerU |
| Excel, Word, CSV | openpyxl, python-docx, csv with limits | Cell addresses and tracked changes kept | — |
| Model gateway | LiteLLM-style client | One interface for Azure OpenAI and on-premises models | — |
| Models | Strong model, small model, vision model | Cost control | On-premises vLLM with open-weight models |
| Observability | OpenTelemetry, Grafana LGTM (today) | Traces, logs, metrics in one place | Bank's platform |
| Security | Keycloak, secret scanning, ClamAV in DMS | Already in the platform | — |
| Quality | Own golden-set harness (today) + CI gate | Domain metrics a generic tool lacks | promptfoo for prompt tests |

The benchmarks found are mostly vendor-run; the parser choice should be confirmed on the bank's own documents with the golden set before go-live.

## Decision register

Every tool and setting is in one of three states: **Final** (decided, no test needed), **Provisional** (chosen now, confirmed or replaced by a measured test on the bank's golden set at the phase shown), or **Open** (waiting for the architect or the bank). A provisional choice becomes final only with its test result written here.

| Area | Choice now | State | How and when it is finalised |
| --- | --- | --- | --- |
| Language, API, schemas | Python 3.12, FastAPI, Pydantic v2 | Final | — |
| Workflow and agent | CrewAI Flows + one agent with skill packs | Final | — |
| Language split (scaling review) | Python DPS for parsing, OCR, AI; Java (Spring) business rules as batched tool calls | Final | Phase 6 load test confirms per-stage budgets |
| Java tools contract | `contracts/spring-tools.yaml`: formula validation, lookups, covenant library | Provisional | Spring team reviews and ships the three endpoints |
| Worker concurrency | One job per process; LIGHT and HEAVY pools; autoscale on oldest queued job age | Final | Phase 6 load test sets replica limits |
| Job queue | SQL Server table, compare-and-set claim, leases | Final | Load test in Phase 6 confirms the numbers |
| Database | Schema `dps`, local database for development | Final for development; production Open | Architect decides production database |
| AI hosting | Azure endpoints from the bank | Final | — |
| Error envelope, correlation id | Bank app's `{code, message, details, timestamp}` | Final | — |
| Observability | OpenTelemetry, Grafana LGTM | Final | — |
| Sign-in | Adapter: service token + `X-User`, or bank identity provider | Final design; mode Open | Architect names the identity provider (Q13) |
| PDF native text | pypdf + pdfplumber + pypdfium2 | Provisional | Phase 2 bake-off: text accuracy and speed on the golden set; PyMuPDF only if licence (AGPL) is cleared |
| OCR for scans | RapidOCR (in use today) | Provisional | Phase 2 bake-off vs PaddleOCR and Tesseract: character error rate, table-cell accuracy, seconds per page, memory, on the bank's scans |
| Layout and tables | Docling (heavy pool) | Provisional | Phase 2 bake-off vs Marker and MinerU on statement tables |
| Excel, Word, CSV readers | openpyxl, xlrd, python-docx, csv | Final | — |
| File safety (DPS second line) | pikepdf, oletools, ZIP limits | Final | Hostile-file corpus in Phase 2 proves it |
| Virus scan | ClamAV in the DMS | Provisional | DMS team confirms vs the bank's existing scanner |
| Models per route (small, strong, vision) | Azure deployments the bank provides | Provisional | Phase 3 bake-off: accuracy, cost and time per document on the golden set |
| Classification rules and weights | Signal tables in Phase 3 | Provisional | Tuned to ≥ 98% accuracy on the golden set, checked on the held-out set |
| Confidence thresholds | High never auto, medium ≥ 0.95, low ≥ 0.90 | Provisional | Phase 5 calibration on reviewed data |
| Capacity (workers, quota) | Worked example in Phase 6 | Provisional | Bank's real volumes + Phase 6 load test |
| CP vs facility mapping | Both carried in every job | Open | Architect (Q11) |
| Roles for upload, approve, retire, download | — | Open | Architect (open in the spec) |

**Decision note, Phase 2 parser bake-off (17 Nov 2026, `golden-set/bakeoff_parsers.py`, 44 golden PDFs):** all 44 pass inspection; page-model text equals today's parser on 44 of 44 (flows unchanged); 98.1% of text blocks carry a position box. Native text median seconds per page: pypdfium2 0.003, pypdf 0.021, pdfplumber 0.074; page model build 0.087. Kept: pypdf for text (parity with today's golden scores), pdfplumber for boxes and tables, pypdfium2 for rendering. OCR (RapidOCR vs PaddleOCR vs Tesseract) and layout (Docling vs Marker vs MinerU) stay Provisional until the bank's scanned set runs on the heavy host; the golden set has no scans.

**Decision note, Phase 3 classification (17 Nov 2026, `dps/analyze/classify.py`, golden set):** rules alone classify 41 of 44 golden PDFs; the 3 misses are scans with no text layer (they need OCR or the vision route), so every text-readable document (41 of 41) is right, with no model call. A wrongly chosen type is caught for 40 of 41 (97.6%) and no document is flagged against its own type (0% false flags). Rules thresholds kept (decide alone at score >= 0.8 with a 0.3 lead); the small-model route is used only below that, and its answer must quote the pages verbatim. Model bake-off per route waits for the bank's Azure deployments.

**Decision note, Phase 4 skill packs (17 Nov 2026):** seven packs under `dps/skills/` (covenants 1.5.0, schedule_terms, amendment_diff, sanction, statement_lines 2.0.0, certificate, commentary). EXTRACT_AGREEMENT and EXTRACT_STATEMENT keep their proven flows (golden scores unchanged); their results gain schedule terms from the page model and the statement identities. EXTRACT_SANCTION, EXTRACT_CERTIFICATE, EXTRACT_AMENDMENT and DRAFT_COMMENTARY run the generic flow: rules, page selection, the agent through the gateway (grounded answers only), voting, Java formula validation and covenant-library match through the Spring tools, the pack's cross-checks, and the challenger on disputed, high-risk and low-confidence fields (the challenger was removed on 18 Nov 2026). Results are stored per run in `dps_run`. Per-pack golden targets need labelled sanction letters, certificates and amendments from the bank; the golden set today has agreements and statements only.

**Decision note, Phases 5-7 (17 Nov 2026):** confidence comes from a documented rule table until a document type has 200 labelled fields, then from a logistic regression per type calibrated by isotonic regression (expected calibration error and Brier score reported); every field keeps its top three reasons. Routing: high-risk fields always reviewed in the first release, medium auto-accepted at >= 0.95, low at >= 0.90 (thresholds in each `pack.yaml`), plus a deterministic 3% QA sample. Review decisions are kept in `dps_review_decision`; every review saves a new run (old runs never change). One worker holds the scheduler lock; the audit trail is sealed daily as a hash chain (`/ops/audit-seals`). Rollout per job type is SHADOW, ASSISTED or NORMAL by configuration, recorded on every job; `/ops/go-live` measures each type against the go-live targets.

Each bake-off is recorded as a short decision note (what was compared, numbers, chosen, why) under this table, so the team and the architect can see why each tool was picked.

## What exists today vs gaps

The Covenexa DPS already covers about half of this design and is the starting point, not a throw-away: its extraction core, safety net and job queue carry over.

| Area | Today (Covenexa DPS) | Gap to close |
| --- | --- | --- |
| Job queue | `dps_job` with lease, heartbeat, attempts, LIGHT/HEAVY pools | Priorities, fairness, back-off schedule, DEAD state + DLQ screen, cancel, batches, idempotency on version |
| Intake | DPS receives the upload itself | Read from the DMS by version id + SHA-256 check; DMS owns upload |
| Formats | PDF, XLSX, XLS, CSV | DOCX; second-line safety inspection (stage 2) |
| Parsing | pypdf, RapidOCR, Docling heavy pool | Common page model with positions saved to `derived/`; segmentation of packs |
| Classification | None, trusts the user's type | Classify stage with rules, small model, vision; `TYPE_MISMATCH` |
| Document types | Loan agreement, financial statement, evidence | Sanction, amendment (as its own job, with diff), compliance certificate, security and guarantee |
| Identifiers | Not extracted | Borrower, facility, loan account, period, currency, units, signature; master-data matching |
| Covenants | Rules + safety net + AI second reader with grounding and voting | Schedule terms skill |
| Statements | Page finder, table reader, AI check, label memory | Vision for scanned tables; accounting identity checks |
| Confidence | Per-field confidence and issue codes | Multi-signal calibrated scorer, risk-based thresholds, review sampling |
| Result contract | Internal tables and REST views | Versioned `dps.result` and `dps.page-model` JSON schemas |
| Events | Outbox (C5 events) | Event feed endpoint with cursor, event catalogue |
| Audit | `dps_audit_event`, AI call log | Hash chain, daily seal hand-off, model and prompt versions on every result |
| Isolation | Parsing in the worker process | Child-process sandbox per job; skill packs |
| Quality | Golden set, benchmarks, CI tests | Golden-set gate on prompt and model changes; shadow runs; model inventory |

## Open questions and build phases

Ten questions for the architect decide details of the build; none blocks starting phase 1 except the model hosting choice.

- [x] **Model hosting (decided 17 Nov: Azure endpoints from the bank):** may page text go to Azure OpenAI (private endpoint, Indian region, no retention), or must the model run on premises? ("Local-first" in the spec.)
- [ ] **Submit path:** DPS submit API (proposed) or Spring writing into a shared job table?
- [ ] **When extraction starts:** at commit (proposed) or only after document approval and activation?
- [ ] **Event delivery:** Spring pulls the feed (proposed) or DPS pushes?
- [ ] **Document types in scope for phase 1:** sanction, agreement, amendment, statement, certificate; security and guarantee later?
- [ ] **Who runs the virus scan:** DMS with ClamAV or the bank's existing scanner? (Spec comment: "Take this later".)
- [ ] **Bulk upload:** CSV manifest signed by whom; maximum files per batch?
- [ ] **Target volumes:** documents per day and peak hour, largest file and page count, number of tenants.
- [ ] **GPU:** available on the VM, on a separate host, or not at all (decides vision and on-premises models)?
- [ ] **Flow 2 and document roles:** still open in the spec.

**Build phases** (each ends with the golden set green, a demo, and the contract published):

1. **Contract and skeleton** — OpenAPI file, JSON schemas, error catalogue, event catalogue agreed with the Spring and UI teams; job API and queue upgrades (priorities, DLQ, cancel, idempotency, batches).
2. **Ingest from DMS and page model** — fetch by version id + SHA-256, stage 2 inspection, sandboxed parsing, DOCX, page model in `derived/`.
3. **ANALYZE** — classification (rules, small model, vision), identifiers, mismatch and mapping suggestions; wired into the DMS upload sequence.
4. **Extraction skill packs** — agreement and sanction covenants + schedule terms, amendment diff, statements, certificates.
5. **Confidence and review** — calibrated scorer, risk thresholds, review and reclassify APIs, learning from corrections, review sampling.
6. **Production hardening** — hash-chained audit, dashboards and alerts, load test at 2× the stated peak, chaos tests (kill workers, model down, database deadlocks), security test of every endpoint, runbook.
7. **Pilot** — shadow run on real documents, compare with manual work, then go live per document type.

**Plan by phase** — about 31 weeks (7 months) for the DPS with two developers, while the DMS team builds Phase 1B in parallel; each phase has its own tab with exit criteria, design detail, tasks and days, tests and definition of done.

| Phase | Weeks | Developer-days | Detail |
| --- | --- | --- | --- |
| DMS design, in the Spring app (reference) | — | — | DMS (Spring app) |
| 1 Contract and skeleton (DPS API, DMS ↔ DPS contract) | 1–4 | 27 | Phase 1 plan |
| 1B DMS core in the Spring app (DMS team, in parallel): upload, safety, store, versions, links, approval, bulk | 5–9 | 25 (DMS team) | Phase 1B plan |
| 2 Processing ingest, sandbox, page model | 5–8 | 20 | Phase 2 plan |
| 3 ANALYZE and model gateway | 9–12 | 20 | Phase 3 plan |
| 4 Extraction skill packs | 13–18 | 30 | Phase 4 plan |
| 5 Confidence, review, learning | 19–21 | 15 | Phase 5 plan |
| 6 Production hardening | 22–25 | 20 | Phase 6 plan |
| 7 Golden set (from week 1), pilot, go-live | 1–31 | analysts + 10 | Phase 7 plan |

**What DPS needs from the other teams** (agree these in phase 1, because DPS cannot finish without them):

| From | Item | Needed by |
| --- | --- | --- |
| DMS team (our organisation) | Host the DMS in the Spring app (Java, Flyway, `cov_` tables), built from the DMS tab | Phase 1B |
| DMS team | Submit DPS jobs from the DMS (`POST /dps/v1/jobs`); consume the DPS event feed | Phase 1 |
| Spring team | Read-only lookup API for facilities and borrowers by ID, account number, CIN, PAN, name | Phase 3 |
| Spring team | Who may upload, approve, retire, download (roles), passed on each call | Phase 1B |
| UI team | Upload, mapping review, approval, version history and folder tree screens on the DMS API; field review screen on the DPS review API | Phases 1B and 5 |
| Infrastructure | VM sizing and storage for `blobs/` and `derived/`, ClamAV signature updates, the Azure endpoints, log platform, vault, backups | Phases 1B–6 |
| Credit analysts | Golden-set documents and labels | Week 1 onwards |
| Security / model risk | Threat model review; model inventory sign-off | Phases 6–7 |

## Sources

Researched 17 November 2026 from search results; most product pages are vendor-written, so figures are directional.

- Agents: [Anthropic, Building effective agents](https://www.anthropic.com/research/building-effective-agents); [Building multi-agent systems: when and how](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them); [LangChain, How and when to build multi-agent systems](https://blog.langchain.com/how-and-when-to-build-multi-agent-systems)
- Human review and thresholds: [AWS, PDF processing with a human loop (Textract + A2I)](https://aws.amazon.com/blogs/machine-learning/processing-pdf-documents-with-a-human-loop-using-amazon-textract-and-amazon-augmented-ai); [AWS, Scalable IDP with Bedrock Data Automation](https://aws.amazon.com/blogs/machine-learning/scalable-intelligent-document-processing-using-amazon-bedrock-data-automation)
- Two-model verification: [Unstract LLMChallenge](https://docs.unstract.com/unstract/unstract_platform/features/llm_challenge/llm_challenge_intro)
- Confidence: [Beyond Logprobs: multi-signal confidence for field extraction (arXiv 2606.24420)](https://arxiv.org/pdf/2606.24420); [Microsoft Foundry, confidence, grounding and HITL](https://devblogs.microsoft.com/foundry/content-understanding-confidence-grounding-hitl/)
- Parsers: [Unstract, open-source OCR tools 2026](https://unstract.com/blog/best-opensource-ocr-tools/); [Self-hosting Docling, Marker, MinerU](https://www.spheron.network/blog/self-host-document-intelligence-docling-marker-mineru-rag-guide/); [Zero-shot table extraction benchmark, WACV 2026](https://openaccess.thecvf.com/content/WACV2026/html/Thomas_Zero-Shot_Table_Extraction_in_Business_Documents_A_Unified_Benchmark_with_WACV_2026_paper.html); [Multimodal models on French financial documents (arXiv 2602.10384)](https://arxiv.org/abs/2602.10384)
- File safety: [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- Queues: [MSSQLTips, queues with READPAST and UPDLOCK](https://www.mssqltips.com/sqlservertip/1257/processing-data-queues-in-sql-server-with-readpast-and-updlock/); [Erik Darling, reusable queues in SQL Server](https://erikdarling.com/building-reusable-queues-part-2/)
- Workflow: [CrewAI production architecture](https://docs.crewai.com/en/concepts/production-architecture)
- Covenant market: [Covenant compliance software for private credit, 2026](https://workwisesolutions.org/guides/best-covenant-compliance-software-private-credit.html); [V7, loan and credit agreement analysis](https://www.v7labs.com/automations/loan-and-credit-agreement-analysis)
- Regulation: [RBI FREE-AI framework, Chambers summary](https://chambers.com/articles/a-framework-for-using-ai-in-the-indian-financial-sector)

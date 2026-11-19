# CTS DPS, the complete picture: how a document becomes a reviewed covenant result {#dps-complete}

Spring sends a job. A Python worker claims it, and a CrewAI Flow reads the document with **rules first and the Document Agent second**. Every value is checked against its page and the Java business rules. The result then waits in DPS until Spring pulls it and a person approves.

- Spring and DPS talk only through jobs: `POST /dps/v1/jobs` in [`dps/src/dps/api/jobs.py:311`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L311), and results come back from `GET /dps/v1/jobs/{id}/result`.[](source:submit)
- One CrewAI Flow runs per job, and one Document Agent reads with the job's skill pack and 6 tools.[](source:flow)
- Every AI answer must quote its page or it is dropped, and a whole-document ledger stops a covenant sentence going missing.[](source:grounded)
- Business rules stay in Java inside Spring. DPS calls them as batched tools, and DPS never decides compliance.[](source:spring)

@answer scope="Repository cts-dps at commit ce996d3 · paths from the repository root · Python code under dps/src/dps/ · 19 Nov 2026"
@meta locale=en repo_url=https://github.com/vishwasshinde016-tech/cts-dps.git repo_revision=ce996d3d33d461c19d0071b3df8aa5b63a77def8
@source submit title="submit_job: the job API Spring calls" path=dps/src/dps/api/jobs.py lines=311:348
@source result title="job_result: Spring pulls the result" path=dps/src/dps/api/jobs.py lines=351:368
@source claim title="claim: compare-and-set queue claim with a lease" path=dps/src/dps/jobs.py lines=85:129
@source fence title="guard_attempt: the attempt fence" path=dps/src/dps/jobs.py lines=54:70
@source runonce title="Worker.run_once: the polling loop" path=dps/src/dps/worker.py lines=131:147
@source process title="Worker._process: heartbeat and run" path=dps/src/dps/worker.py lines=195:209
@source flow title="SkillJobFlow: one CrewAI Flow per job" path=dps/src/dps/flows/skill_job.py lines=27:49
@source onboard title="run_onboard: classify, pick the pack, extract" path=dps/src/dps/skills/runner.py lines=611:661
@source extract title="_extract: the whole extraction order" path=dps/src/dps/skills/runner.py lines=391:520
@source finish title="finish: save the run and its event" path=dps/src/dps/skills/runner.py lines=557:563
@source numbers title="check_numbers: the number must be in the quote" path=dps/src/dps/skills/runner.py lines=64:83
@source grounded title="grounded: the quote must be on its page" path=dps/src/dps/skills/common.py lines=53:69
@source proposals title="agent_proposals: one model call per batch" path=dps/src/dps/skills/common.py lines=142:200
@source makeagent title="make_agent: a fresh CrewAI Agent per read" path=dps/src/dps/agent/document_agent.py lines=277:286
@source agentread title="read: schema check and one repair" path=dps/src/dps/agent/document_agent.py lines=293:318
@source gateway title="call_json: the model gateway call" path=dps/src/dps/agent/gateway.py lines=242:257
@source ledger title="settle: one state for every duty sentence" path=dps/src/dps/skills/covenants/ledger.py lines=146:183
@source spring title="validate_formulas: Java rules as one batched call" path=dps/src/dps/integrations/spring_tools.py lines=151:161
@source stamp title="stamp: taxonomy code, suggestion only" path=dps/src/dps/skills/covenants/codes.py lines=34:75
@source amend title="amendment rules: ADD, CHANGE, DELETE" path=dps/src/dps/skills/amendment_diff/rules.py lines=56:124
@source amendset title="amended_set: the covenants after the amendment" path=dps/src/dps/skills/amendment_diff/rules.py lines=145:169
@source models title="Database models (all 25 tables)" path=dps/src/dps/models.py lines=78:566

## What is inside the DPS, what is outside, and how do they connect? {#architecture}

@diagram architecture id=dps-architecture source=dps-architecture.json title="CTS DPS architecture: inside, outside and connectors"

@steps id=architecture-steps
| a1 | Spring app (CTS) | Spring submits a job with `POST /dps/v1/jobs`. The contract is [`contracts/dps-jobs-api.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/dps-jobs-api.yaml). | dps-architecture | spring |
| a2 | DPS job API | [`dps/src/dps/api/jobs.py:311`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L311) `submit_job` validates the request, checks the SHA-256 and returns the running job for a duplicate. | dps-architecture | api |
| a3 | SQL Server schema dps | The job is a row in `dps_job`. The worker claims it with compare-and-set in [`dps/src/dps/jobs.py:85`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py#L85) `claim`. | dps-architecture | queue |
| a4 | Python worker | [`dps/src/dps/worker.py:131`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py#L131) `run_once` polls, holds a lease with a heartbeat and fences every write by attempt. | dps-architecture | worker |
| a5 | Ingest + sandbox parse | The file is read from the DMS by version and SHA-256, inspected, and parsed in a child process with no network. | dps-architecture | ingest |
| a6 | CrewAI Flow per job | [`dps/src/dps/flows/skill_job.py:27`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py#L27) `SkillJobFlow` runs the skills and saves the state after each step. | dps-architecture | flow |
| a7 | Skill runner | [`dps/src/dps/skills/runner.py:611`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L611) `run_onboard` classifies, runs rules, the page finder, 4 agent reads, the ledger, Java checks, codes and the score. | dps-architecture | runner |
| a8 | Document Agent | [`dps/src/dps/agent/document_agent.py:277`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py#L277) `make_agent` builds one CrewAI agent with the pack's tools. | dps-architecture | agent |
| a9 | Model gateway | [`dps/src/dps/agent/gateway.py:242`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/gateway.py#L242) `call_json` applies the budget, retries, breaker and masking, and sends only selected pages to Azure. | dps-architecture | gateway |
| a10 | Spring tools client | [`dps/src/dps/integrations/spring_tools.py:151`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations/spring_tools.py#L151) calls the Java business rules once per document. | dps-architecture | tools |
| a11 | Event feed | The result and its event are written in one transaction. Spring reads `GET /dps/v1/events`. | dps-architecture | feed |

| Architect decision | Where in the code |
| --- | --- |
| Spring and DPS on the same VM | [`deploy/docker-compose.cts-dps.yml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/deploy/docker-compose.cts-dps.yml): separate containers with CPU and memory limits |
| They talk only through jobs | [`dps/src/dps/api/jobs.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py) (`POST /dps/v1/jobs`), [`dps/src/dps/jobs.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py) (the queue) |
| A Python worker polls the queue | [`dps/src/dps/worker.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py) (`Worker.run_once`, lease-based claim) |
| Each job has its own context with a small set of skills, tools and prompts | [`dps/src/dps/flows/skill_job.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py), `dps/src/dps/skills/<pack>/pack.yaml` |
| One agent with many skills and tools | [`dps/src/dps/agent/document_agent.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py) (`make_agent`, `read`) |

## How does the Miro onboarding board run in the code? {#onboarding}

@diagram sequence id=onboarding-flow source=dps-onboarding-sequence.json title="Onboarding job (ONBOARD): submit to review"

@steps id=onboarding-steps
| o1 | DPS job API | Spring posts an `ONBOARD` job. The same document and context already in DPS returns that job, which Spring shows as extraction in progress. | onboarding-flow | api |
| o2 | Worker | The worker claims the job and runs ingest and the sandbox parse to build the page model. | onboarding-flow | worker |
| o3 | Flow + runner | AI 1 classifies with rules first. The agent gives a second opinion only when the rules are unsure. | onboarding-flow | runner |
| o4 | Document Agent | AI 2 reads covenants in 4 focused parts: financial, undertakings, reporting and events of default. | onboarding-flow | agent |
| o5 | Spring tools | The agent checks formulas and library names against the Java rules in one batched call. | onboarding-flow | tools |
| o6 | dps_job / dps_run | The run is saved with its `needs_review` event. Spring pulls it for the review screen. | onboarding-flow | queue |

| Board box | Owner | Code (full path) |
| --- | --- | --- |
| Search CP, present in CTS DB, get covenants | Spring | Spring team |
| Check the DMS, upload, file checks | DMS | DPS checks again: [`dps/src/dps/ingest/inspect.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/inspect.py), [`dps/src/dps/ingest/pipeline.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/pipeline.py), [`dps/src/dps/parse/sandbox.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/sandbox.py) |
| Invoke the Onboarding Flow | Spring to DPS | job type `ONBOARD` in [`dps/src/dps/api/jobs.py:38`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L38) `JOB_TYPES` |
| Job present: extraction is in progress | DPS | [`dps/src/dps/api/jobs.py:311`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L311) `submit_job` (idempotency key and `input_key` give `duplicate: true`) |
| Create an entry in the job table | DPS | [`dps/src/dps/jobs.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py) and table `dps_job` in [`dps/src/dps/models.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/models.py) |
| Python worker invokes the CrewAI Flow | DPS | [`dps/src/dps/worker.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py) then [`dps/src/dps/flows/skill_job.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py) |
| AI 1: classify and identify | DPS | [`dps/src/dps/analyze/classify.py:294`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/classify.py#L294), [`dps/src/dps/analyze/identify.py:153`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/identify.py#L153) |
| AI 2: extract covenants | DPS | [`dps/src/dps/skills/covenants/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants) (rules, pages, parts, ledger, omissions, codes) |
| AI 3: extract schedule terms | DPS | [`dps/src/dps/skills/schedule_terms/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/schedule_terms) |
| Extraction completed, update for review | DPS to Spring | [`dps/src/dps/skills/runner.py:557`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L557) `finish`, [`dps/src/dps/feed.py:39`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/feed.py#L39); Spring pulls `GET /dps/v1/jobs/{id}/result` |
| Exception flow | DPS | [`dps/src/dps/jobs.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py) (retry, back-off, DEAD), [`contracts/catalogues/errors.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/catalogues/errors.yaml) |
| Review, persist, update schedule | Spring | DPS keeps each review as a new run: [`dps/src/dps/api/review.py:141`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/review.py#L141) |

## How does the Miro amendment board run in the code? {#amendment}

@diagram sequence id=amendment-flow source=dps-amendment-sequence.json title="Amendment job (EXTRACT_AMENDMENT): base, changes, amended set"

@steps id=amendment-steps
| m1 | Spring app | Spring sends `EXTRACT_AMENDMENT` with the approved covenants in `context.baseCovenants`, each with its `appliedCovenantId`. | amendment-flow | spring |
| m2 | Flow + runner | The worker runs the same Flow as onboarding with the `amendment_diff` skill. | amendment-flow | runner |
| m3 | amendment_diff | [`dps/src/dps/skills/amendment_diff/rules.py:56`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/amendment_diff/rules.py#L56) finds ADD, CHANGE and DELETE per approved covenant. | amendment-flow | diff |
| m4 | Document Agent | The agent explains changes the rules cannot match. An unclear target gives `AMBIGUOUS_AMENDMENT_TARGET`. | amendment-flow | agent |

| Board box | Code |
| --- | --- |
| CP present, fetch CP and covenants | Spring |
| Upload the amendment, file checks | Spring and DMS; DPS checks again |
| Invoke the Amendment Flow | `EXTRACT_AMENDMENT` then [`dps/src/dps/flows/skill_job.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py) then [`dps/src/dps/skills/runner.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py) |
| Find what changed per approved covenant | [`dps/src/dps/skills/amendment_diff/rules.py:56`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/amendment_diff/rules.py#L56) `rules`, matched by `appliedCovenantId` and clause |
| Schedule changes | [`dps/src/dps/skills/schedule_terms/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/schedule_terms), also run for amendments |
| The covenant set after the amendment | `amendedSet` from [`dps/src/dps/skills/amendment_diff/rules.py:145`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/amendment_diff/rules.py#L145) `amended_set` |
| Approve, persist, update schedule | Spring |

## What about assessment, and which job types exist? {#job-types}

The boards show assessment only as an entry point, so DPS follows Flow 3 of the design document.

| Job type | Started by | Flow (file) | What it returns |
| --- | --- | --- | --- |
| `ANALYZE` | DMS, before accepting a file | [`dps/src/dps/analyze/analysis.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/analysis.py) | type, identifiers, period, legibility, key pages |
| `ONBOARD` | Spring (onboarding board) | [`dps/src/dps/flows/skill_job.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py) and [`dps/src/dps/skills/runner.py:611`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L611) | classification, covenants, schedule terms |
| `EXTRACT_AGREEMENT` | Spring | [`dps/src/dps/flows/intake.py:70`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/intake.py#L70) | covenants, with the same ledger |
| `EXTRACT_AMENDMENT` | Spring (amendment board) | [`dps/src/dps/skills/amendment_diff/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/amendment_diff) | changes and the amended set |
| `EXTRACT_SANCTION` | Spring | [`dps/src/dps/skills/sanction/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/sanction) | sanction terms and covenants |
| `EXTRACT_STATEMENT` | Spring (assessment) | [`dps/src/dps/flows/statement.py:133`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/statement.py#L133) | statement lines with cells |
| `EXTRACT_CERTIFICATE` | Spring (assessment) | [`dps/src/dps/skills/certificate/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/certificate) | certified figures, UDIN, signatory |
| `DRAFT_COMMENTARY` | Spring (cycle outcome) | [`dps/src/dps/skills/commentary/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/commentary) | draft text; it never decides compliance |

| Assessment check | Code |
| --- | --- |
| Period, legal entity, basis, currency and units against the cycle | [`dps/src/dps/analyze/obligations.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/obligations.py) (`PERIOD_MISMATCH`, `ENTITY_MISMATCH`) |
| Restated figures against the approved ones | [`dps/src/dps/skills/runner.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py) (`RESTATED`) |
| Every statement line not found is listed | [`dps/src/dps/catalog.py:65`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/catalog.py#L65) (`LINES_NOT_FOUND`) |
| Approved figures served to Spring | `POST /api/data/values` in [`dps/src/dps/api/documents.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/documents.py) |

## What happens to one document, step by step? {#steps}

Open each file at the line shown. The run log [`docs/cts-dps/sample-run-AGR-001.txt`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/docs/cts-dps/sample-run-AGR-001.txt) shows the same path for a real document.

| # | Stage | What happens | File:line |
| --- | --- | --- | --- |
| 1 | Submit | The request is validated and the stored SHA-256 is checked. A duplicate returns the existing job; a full queue returns 429. | [`dps/src/dps/api/jobs.py:311`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L311) `submit_job`, [`:166`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L166) `validate_job`, [`:268`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L268) `_input_key`, [`:240`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L240) `create_job` |
| 2 | Queue | A `dps_job` row in state `QUEUED` and the event `dps.job.accepted` | [`dps/src/dps/api/jobs.py:240`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L240), [`dps/src/dps/feed.py:39`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/feed.py#L39) `emit_for_job` |
| 3 | Claim | Compare-and-set claim, heartbeat, attempt fence on every write | [`dps/src/dps/worker.py:131`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py#L131) `run_once`, [`:195`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py#L195) `_process`; [`dps/src/dps/jobs.py:85`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py#L85) `claim`, [`:162`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py#L162) `renew_lease`, [`:54`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py#L54) `guard_attempt` |
| 4 | Route | By flow: AGREEMENT_INTAKE, STATEMENT_PROCESSING, SKILL_PACK, ANALYZE | [`dps/src/dps/worker.py:211`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py#L211) `_run`; [`dps/src/dps/api/jobs.py:38`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L38) `JOB_TYPES` |
| 5 | Fetch | Read the bytes from the DMS store and check the SHA-256 | [`dps/src/dps/worker.py:330`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py#L330) `_ingest`, [`dps/src/dps/ingest/pipeline.py:48`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/pipeline.py#L48) `read` |
| 6 | Inspect | Real type, encryption, corruption, macros, scripts, zip bombs, size and page limits | [`dps/src/dps/ingest/inspect.py:63`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/inspect.py#L63) `inspect` |
| 7 | Parse in a sandbox | A child process with CPU, memory and time limits and no network | [`dps/src/dps/ingest/pipeline.py:56`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/pipeline.py#L56) `page_model`, [`dps/src/dps/parse/sandbox.py:157`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/sandbox.py#L157) `run` |
| 8 | Page model | pypdf text, RapidOCR for pages with no text, pdfplumber boxes, Excel and Word readers | [`dps/src/dps/parse/page_model.py:30`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py#L30) `build`, [`:53`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py#L53), [`:142`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py#L142), [`:240`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py#L240); [`dps/src/dps/parse/engine.py:127`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py#L127), [`:71`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py#L71), [`:207`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py#L207) |
| 9 | Segment | Split a bundled PDF into its documents | [`dps/src/dps/parse/segment.py:46`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/segment.py#L46) `segment` |
| 10 | Flow | One CrewAI Flow per job; state saved per step | [`dps/src/dps/flows/skill_job.py:27`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py#L27); [`dps/src/dps/flows/intake.py:70`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/intake.py#L70); [`dps/src/dps/flows/persistence.py:22`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/persistence.py#L22) |
| 11 | Onboard entry | Classify, pick the pack, extract, identifiers, cycle checks | [`dps/src/dps/skills/runner.py:611`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L611) `run_onboard`, [`:608`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L608) `ONBOARD_PACKS` |
| 12 | Classify | Rules score each type; agent only when unsure | [`dps/src/dps/analyze/classify.py:294`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/classify.py#L294) `classify`, [`:134`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/classify.py#L134), [`:193`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/classify.py#L193) |
| 13 | Identify | Borrower, facility, account, period, currency, units; matched in Spring | [`dps/src/dps/analyze/identify.py:153`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/identify.py#L153) `find_identifiers`, [`:271`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/identify.py#L271) `match` |
| 14 | Generic run | Budget, extract, codes, scoring, checks, result JSON | [`dps/src/dps/skills/runner.py:313`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L313) `run` |
| 15 | Extraction order | The whole order in one function | [`dps/src/dps/skills/runner.py:391`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L391) `_extract` |
| 16 | Rules | Numbered clauses, metrics, thresholds, non-financial rules | [`dps/src/dps/skills/covenants/rules.py:61`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/rules.py#L61), [`dps/src/dps/extraction/agreement.py:947`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/extraction/agreement.py#L947) |
| 17 | Page finder | Rule pages, covenant sections, duty sentences, definitions; 4 batches of 25 pages | [`dps/src/dps/skills/covenants/pages.py:98`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/pages.py#L98) `covenant_batches`, [`:61`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/pages.py#L61) `is_duty` |
| 18 | Agent read in 4 parts | Financial, undertakings, reporting, events of default | [`dps/src/dps/skills/covenants/parts.py:75`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/parts.py#L75), [`dps/src/dps/skills/common.py:203`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/common.py#L203), [`:142`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/common.py#L142), [`:126`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/common.py#L126) |
| 19 | The agent | Fresh CrewAI Agent with the pack's tools; schema check and one repair | [`dps/src/dps/agent/document_agent.py:277`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py#L277) `make_agent`, [`:293`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py#L293) `read`, [`:50`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py#L50) `GatewayLLM` |
| 20 | Model gateway | Routes, retries, breaker, budget, masking, call log | [`dps/src/dps/agent/gateway.py:150`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/gateway.py#L150), [`:242`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/gateway.py#L242) `call_json`, [`:126`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/gateway.py#L126) `JobBudget`; [`dps/src/dps/agent/guard.py:144`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/guard.py#L144) |
| 21 | Grounding | The quote must be on its page | [`dps/src/dps/skills/common.py:53`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/common.py#L53) `grounded` |
| 22 | Clean and vote | Library names, rules against the agent | [`dps/src/dps/skills/covenants/names.py:132`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/names.py#L132); [`dps/src/dps/skills/runner.py:223`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L223) `_vote_covenants` |
| 23 | Ledger | Every covenant-shaped sentence gets one state | [`dps/src/dps/skills/covenants/ledger.py:121`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/ledger.py#L121) `candidates`, [`:146`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/ledger.py#L146) `settle`, [`:228`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/ledger.py#L228) `review_items` |
| 24 | Second read | The agent looks again at unexplained duty sentences | [`dps/src/dps/skills/covenants/omissions.py:82`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/omissions.py#L82) `read` |
| 25 | Java check | Formula validation and covenant library | [`dps/src/dps/skills/runner.py:116`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L116) `verify_with_spring`, [`dps/src/dps/integrations/spring_tools.py:151`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations/spring_tools.py#L151), [`:221`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations/spring_tools.py#L221) |
| 26 | Codes | Taxonomy code and category; suggestion only for missing values | [`dps/src/dps/skills/covenants/codes.py:34`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/codes.py#L34) `stamp`; [`dps/src/dps/skills/covenants/taxonomy/__init__.py:107`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/taxonomy/__init__.py#L107) |
| 27 | Score and route | Confidence per field, risk thresholds, 3% QA sample | [`dps/src/dps/scoring/__init__.py:211`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/scoring/__init__.py#L211) `Scorer`, [`:193`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/scoring/__init__.py#L193) `route` |
| 28 | Checks | Number in quote, page outcomes, budget warning | [`dps/src/dps/skills/runner.py:64`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L64) `check_numbers`, [`:86`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L86) `page_accounting`, [`:52`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L52) `budget_warning` |
| 29 | Save and event | Result in `dps_run` and the event in one transaction | [`dps/src/dps/skills/runner.py:557`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L557) `finish`; [`dps/src/dps/feed.py:39`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/feed.py#L39) |
| 30 | Spring pulls | Events, then the result | `GET /dps/v1/events`; [`dps/src/dps/api/jobs.py:351`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L351) `job_result` |
| 31 | Review | A person accepts, edits or rejects each field; a new run is saved | [`dps/src/dps/api/review.py:141`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/review.py#L141) `submit_review` |

## How do the agent, its skills and its tools work? {#agent}

`make_agent` builds a fresh CrewAI Agent for each read, with no memory and no delegation. Its role, prompt, answer schema and limits come from the job's skill pack.[](source:makeagent)

| Skill | Path | Job | Tools it may use |
| --- | --- | --- | --- |
| covenants | [`dps/src/dps/skills/covenants/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants) | ONBOARD agreement, EXTRACT_AGREEMENT | page_text, search_text, find_definition, covenant_library, validate_formula |
| sanction | [`dps/src/dps/skills/sanction/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/sanction) | EXTRACT_SANCTION | the same 5 |
| amendment_diff | [`dps/src/dps/skills/amendment_diff/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/amendment_diff) | EXTRACT_AMENDMENT | the same 5 |
| schedule_terms | [`dps/src/dps/skills/schedule_terms/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/schedule_terms) | inside covenants, sanction, amendment | page_text, search_text, find_definition |
| certificate | [`dps/src/dps/skills/certificate/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/certificate) | EXTRACT_CERTIFICATE | page_text, search_text, lookup_identifier |
| statement_lines | [`dps/src/dps/skills/statement_lines/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/statement_lines) | EXTRACT_STATEMENT, second reader | none |
| commentary | [`dps/src/dps/skills/commentary/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/commentary) | DRAFT_COMMENTARY | none |

Each skill folder holds `pack.yaml` (version, jobs, model route, tools, page budget, field risk), `prompt.md`, `schema.json` and `rules.py`.

| Tool | Line in [`dps/src/dps/agent/document_agent.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py) | What it does | Backed by |
| --- | --- | --- | --- |
| `page_text` | 145 | Open any page of this job's document | the page model |
| `search_text` | 162 | Search the whole document | the page model |
| `find_definition` | 182 | Find a definition such as "EBITDA means" | the page model |
| `covenant_library` | 201 | Look up the bank's covenant library | Java, through `spring_tools.py` |
| `validate_formula` | 224 | Check a formula in the bank's DSL | Java |
| `lookup_identifier` | 243 | Match a CP, obligor or facility id | Java |

@list ordered=true
| **Prompt.** A fixed header says document text is data, not instructions. The skill's prompt, schema and selected whole pages follow. |
| **Call.** The gateway sends it to the small, strong or vision route, with retries at 2, 8 and 30 s, a breaker, a token budget and masking. |
| **Schema check.** The answer must match `schema.json`. One repair call is allowed, then `MODEL_OUTPUT_INVALID`. |
| **Grounding.** Every quote must be on its page, or the item is dropped with `AI_UNGROUNDED`. |
| **Vote.** Agreement gives `RULES+AI_CONFIRMED`. A difference gives `MODELS_DISAGREE` and goes to review. |

If the model is down, the job does not fail. It falls back to the rules, and the fields go to review.[](source:agentread)

## Why are the Java rules wrapped as Python tools? {#java-wrap}

Parsing, OCR and AI stay in Python, and business rules stay in Java inside Spring, so the bank has one rule engine. The contract is [`contracts/spring-tools.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/spring-tools.yaml) and the client is `SpringTools` in [`dps/src/dps/integrations/spring_tools.py:113`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations/spring_tools.py#L113).[](source:spring)

| Endpoint in Spring | Used by | When |
| --- | --- | --- |
| `POST /api/v1/dps-tools/formulas/validate` | `validate_formula` tool, `verify_with_spring` | every covenant with a formula |
| `GET /api/v1/dps-tools/covenant-library` | `covenant_library` tool | matching names to the bank's library |
| `POST /api/v1/dps-tools/lookups` | `lookup_identifier`, [`dps/src/dps/analyze/identify.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/identify.py) | CP, obligor and facility ids in a document |
| `POST /api/v1/dps-tools/events` (optional) | [`dps/src/dps/worker.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/worker.py) | when Spring wants DPS to push events |

@facts
| Batched | one HTTP call per document, never one per field |
| Bounded | timeout, retries and a circuit breaker |
| Degrading | if Spring is down the field gets `SPRING_UNAVAILABLE` and goes to review; the job carries on |
| Test stand-in | [`dps/tests/stub_spring.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/tests/stub_spring.py) |

## Which libraries does DPS use, and why? {#libraries}

| Job | Library | Why this one | Where |
| --- | --- | --- | --- |
| API | FastAPI, uvicorn, pydantic | standard, fast, typed contracts | [`dps/src/dps/main.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/main.py), [`dps/src/dps/api/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api) |
| Database | SQLAlchemy 2, Alembic, SQL Server, SQLite in dev | same code on both; versioned migrations | [`dps/src/dps/db.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/db.py), [`dps/src/dps/models.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/models.py), [`dps/migrations/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/migrations) |
| Flows | CrewAI Flows | the architect's choice: one Flow per job, state per step | [`dps/src/dps/flows/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows) |
| Agent | CrewAI Agent and BaseTool | one agent, the job's skill and its tools | [`dps/src/dps/agent/document_agent.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py) |
| Models | bank Azure OpenAI (gpt-4.1, mini, 4o) | bank-approved private endpoint | [`dps/src/dps/agent/gateway.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/gateway.py) |
| PDF text | pypdf | fastest native text | [`dps/src/dps/parse/engine.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py) |
| PDF boxes | pdfplumber | a position for every line, so the reviewer sees the spot | [`dps/src/dps/parse/page_model.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py) |
| Page images | pypdfium2 | renders pages for OCR and the vision model | [`dps/src/dps/parse/engine.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py) |
| OCR | RapidOCR on onnxruntime | runs on the bank's machine, no cloud OCR | [`dps/src/dps/parse/engine.py:71`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py#L71) |
| Heavy layout | Docling, heavy pool only | poor layouts and scanned tables | [`dps/src/dps/parse/engine.py:207`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/engine.py#L207) |
| File safety | pikepdf, oletools | encrypted, broken, macro and script checks | [`dps/src/dps/ingest/inspect.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/inspect.py) |
| Excel, Word, CSV | openpyxl, xlrd, python-docx | cells and paragraphs with their place | [`dps/src/dps/parse/page_model.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py) |
| HTTP to Spring | httpx | timeouts, retries, breaker | [`dps/src/dps/integrations/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations) |
| Quality | pytest, ruff, bandit, pip-audit, semgrep, Trivy | the bank's bar, 95% coverage | [`dps/tests/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/tests), [`.github/workflows/`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/.github/workflows) |

## What is stored in the database, and where is everything else kept? {#database}

@diagram erd id=dps-database source=dps-database.json title="SQL Server schema dps: all 25 tables and 262 columns"

Solid lines are real foreign keys. Dashed lines join by id with no foreign key, so queue tables can be purged or archived alone.[](source:models)

| What | Where | Notes |
| --- | --- | --- |
| Original files | DMS store, read only | DPS never writes or moves an original |
| Page models | `derived/<tenant>/<version>/page-model/`, listed in `dps_artifact` | write-once; reused for the same bytes and pipeline |
| Jobs, attempts, batches | `dps_job`, `dps_job_attempt`, `dps_batch` | queue and history |
| Results | `dps_run`, one `dps.result/1.0` JSON per run | a review creates a new run; old runs never change |
| Agreement terms | `dps_extracted_term` | review state per term |
| Statement values | `dps_statement_value` | served through `POST /api/data/values` |
| Events for Spring | `dps_event_feed`, `dps_event_outbox` | same transaction as the change |
| Review decisions | `dps_review_decision` | learning loop and calibration |
| AI calls and cost | `dps_ai_call`, `dps_ai_usage`, `dps_ai_cache` | prompt text only where allowed |
| Audit trail | `dps_audit_event`, `dps_audit_seal`, `audit-seals/` | daily hash chain |
| Flow state | `dps_flow_state` | resume after review |
| Sent to Azure | only the selected page text of the job | allowed hosts only; nothing stored there |

## Where do the IDs come from? {#ids}

| ID | Comes from | Carried in | Used for |
| --- | --- | --- | --- |
| `tenantId` | Spring, server-side | `context.tenantId` | scoping every row and file |
| `cpIds` | Spring | `context.cpIds` | `dps_document_cp_link`, lookups, statement values |
| `obligorId`, `borrowerId` | Spring | `context.obligorId`, `context.borrowerId` | identifier matching in [`dps/src/dps/analyze/identify.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/identify.py) |
| `facilityIds` | Spring | `context.facilityIds` | identifier matching; conflicts give `MAPPING_REVIEW` |
| `versionId`, `objectKey`, `sha256` | DMS | `document` in the request | read the exact immutable file and check its hash |
| `appliedCovenantId` | Spring CTS DB | `context.baseCovenants` | which approved covenant an amendment touches |
| `covenant_master_id` | Spring covenant library | `field.libraryMatch` | link to the bank's covenant master |
| Taxonomy `code` such as `FIN_DSCR` | DPS taxonomy, 228 types | `field.value.code` | one name per covenant type |
| `jobId` | DPS | response of `POST /dps/v1/jobs` | status, result, events |
| `field.id` | DPS, hash of pack, group, name and page | each result field | review decisions point at it |
| `correlationId` | Spring header `X-Correlation-Id` | headers, logs, events | tracing one request across both apps |

## Which schemas define the contracts? {#schemas}

| Schema | File | What it is |
| --- | --- | --- |
| Job request | [`contracts/schemas/job-request.json`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/schemas/job-request.json) | what Spring sends |
| Job status | [`contracts/schemas/job-status.json`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/schemas/job-status.json) | `GET /dps/v1/jobs/{id}` |
| Result | [`contracts/schemas/result.json`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/schemas/result.json) | `dps.result/1.0`, the extraction output |
| Page model | [`contracts/schemas/page-model.json`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/schemas/page-model.json) | pages, blocks, boxes, tables |
| Event | [`contracts/schemas/event.json`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/schemas/event.json), [`contracts/catalogues/events.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/catalogues/events.yaml) | what Spring reads from the feed |
| Issue codes | [`contracts/catalogues/errors.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/catalogues/errors.yaml) | every code a result can carry |
| Job API | [`contracts/dps-jobs-api.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/dps-jobs-api.yaml) | every endpoint Spring calls |
| Java tools | [`contracts/spring-tools.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/contracts/spring-tools.yaml) | endpoints DPS calls in Spring |

```json title="One covenant field in dps.result/1.0"
{ "id": "cov-1a2b", "group": "FINANCIAL_COVENANT", "name": "DSCR",
  "value": { "metric": "DSCR", "code": "FIN_DSCR", "operator": ">=", "threshold": 1.25, "unit": "TIMES", "suggested": null },
  "source": { "page": 3, "quote": "maintain a DSCR of not less than 1.25 times", "bbox": [72, 410, 540, 424] },
  "method": "RULES+AI_CONFIRMED", "confidence": 0.95, "status": "NEEDS_REVIEW", "issues": [] }
```

## What happens after extraction? {#after}

@list ordered=true
| The result is saved in `dps_run` and its event in `dps_event_feed`, in one transaction. |
| Spring reads `GET /dps/v1/events`, then `GET /dps/v1/jobs/{id}/result`, and shows each field with its page, quote and box. |
| A person accepts, edits or rejects each field through `POST /dps/v1/jobs/{id}/review`; a new run is saved. |
| Review decisions feed `dps_review_decision`; an administrator can promote confirmed wording to rules in [`dps/src/dps/learning.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/learning.py). |
| Spring approves, persists the covenants as immutable, updates the schedule and runs the compliance calculation. |

@callout level=note title="The agent works during extraction, never after it"
The agent only proposes, and code checks every proposal. DPS never decides compliance or a breach.

## How does one document travel through DPS, end to end? {#journey}

@list ordered=true
| **Upload.** The RM uploads in CTS, the DMS keeps one immutable copy, and Spring posts a job with the file reference and the CP, borrower and facility IDs ([`dps/src/dps/api/jobs.py:312`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L312)). |
| **Queue.** DPS validates the request, checks the SHA-256, stops duplicates and writes one `dps_job` row ([`dps/src/dps/api/jobs.py:240`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/jobs.py#L240)). |
| **Claim.** A worker claims the job with an atomic update and keeps a lease with a heartbeat ([`dps/src/dps/jobs.py:85`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/jobs.py#L85)). |
| **Check the file.** The real type is checked, and encrypted, broken or macro files are refused ([`dps/src/dps/ingest/inspect.py:63`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ingest/inspect.py#L63)). |
| **Parse.** A child process with no network builds the page model: pypdf text, pdfplumber positions, and RapidOCR on pypdfium2 images for scans ([`dps/src/dps/parse/page_model.py:30`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/parse/page_model.py#L30)). |
| **Store the parse.** The page model is saved once in `derived/` and listed in `dps_artifact`; the original file never changes. |
| **Start the flow.** One CrewAI Flow runs per job and saves its state per step ([`dps/src/dps/flows/skill_job.py:27`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/flows/skill_job.py#L27)). |
| **Classify.** Rules score each type; the agent gives a quoted second opinion only when unsure ([`dps/src/dps/analyze/classify.py:294`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/analyze/classify.py#L294)). |
| **Choose the skill.** The type picks the pack in `ONBOARD_PACKS`; other job types pick the pack whose `pack.yaml` lists them ([`dps/src/dps/skills/runner.py:608`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L608), [`dps/src/dps/skills/__init__.py:80`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/__init__.py#L80)). |
| **Pack limits.** `pack.yaml` sets the model route, tools, 4 reads of 25 pages, 8 tool calls, 3 turns, 240 s, 400,000 tokens and field risk ([`dps/src/dps/skills/covenants/pack.yaml`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/pack.yaml)). |
| **Rules first.** The pack's rules read the whole document without a model ([`dps/src/dps/skills/covenants/rules.py:61`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/rules.py#L61)). |
| **Choose pages.** The agent never gets the whole document; the page finder sends ranked pages in 4 focused reads ([`dps/src/dps/skills/covenants/pages.py:98`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/pages.py#L98)). |
| **Agent read.** A fresh agent with the skill's prompt, schema and tools reads through the gateway to the bank's Azure ([`dps/src/dps/agent/document_agent.py:277`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/agent/document_agent.py#L277)). |
| **Check answers.** Schema check with one repair, the quote must be on its page, the number must be in the quote, then the vote ([`dps/src/dps/skills/common.py:53`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/common.py#L53)). |
| **Nothing missed.** The ledger gives every duty sentence one state and sends unexplained ones to a second read, then to a person ([`dps/src/dps/skills/covenants/ledger.py:146`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/ledger.py#L146)). |
| **Java rules.** Formulas and library names are checked by Spring in one batched call ([`dps/src/dps/integrations/spring_tools.py:151`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/integrations/spring_tools.py#L151)). |
| **Name and score.** A taxonomy code and a confidence per field; risky or unsure fields go to review ([`dps/src/dps/skills/covenants/codes.py:34`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/covenants/codes.py#L34)). |
| **Save.** `dps.result/1.0` goes to `dps_run` with its event in one transaction ([`dps/src/dps/skills/runner.py:557`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/skills/runner.py#L557)). |
| **Review.** Spring pulls the result; a person accepts, edits or rejects; Spring decides compliance ([`dps/src/dps/api/review.py:141`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/api/review.py#L141)). |

@callout level=note title="Is the extracted data sent to the agent?"
The parsed text stays on the bank's machine. The agent sees only the selected pages for its read, through the gateway, on the bank's own Azure endpoint. Code checks everything it returns, and the agent never writes anything.

## How do I upload a document, and which files can DPS read? {#upload}

In the bank, Spring and the DMS do the upload. On a laptop, two scripts play their part. Run them in `C:\cts-dps\dps`.

| Way | Command | What it shows |
| --- | --- | --- |
| A. Quick demo | `.venv\Scripts\python scripts\demo_onboard.py <file>` | the reading code of a job, printed in seconds |
| B. Real service | API: `.venv\Scripts\uvicorn --factory dps.main:create_app --port 9400`; worker: `.venv\Scripts\python -m dps.worker`; then `.venv\Scripts\python scripts\submit_job.py <file>` | file stored by SHA-256, Spring's `POST /dps/v1/jobs`, worker stages, the result |
| C. The API | browser at `http://localhost:9400/docs` | every endpoint Spring calls |

| Where it lands | Folder or table |
| --- | --- |
| Uploaded file | `dps\storage\<2 letters>\<sha256>.pdf` |
| Page model | `dps\derived\default\<version>\page-model\` |
| Jobs, results, events | SQLite file `dps\dps.db` |

| Format | Library | Notes |
| --- | --- | --- |
| PDF with text | pypdf, pdfplumber | the normal case |
| Scanned PDF | pypdfium2 and RapidOCR, local | no cloud OCR |
| Word `.docx` | python-docx | paragraphs and tables |
| Excel `.xlsx`, `.xls` | openpyxl, xlrd | cells with sheet and address |
| CSV | Python `csv` | statements |
| Poor layouts | Docling, heavy pool only | optional extra |
| HTML | not read | `scripts\fetch_public_samples.py` prints it to PDF |

## Which real files from the architect's list do I demo, and what do they show? {#real-files}

The architect's documents are in the repository folder `architect-documents\` (its README lists each source link). Results below were measured on 19 Nov 2026, rules only.

| File | Flow | Measured result |
| --- | --- | --- |
| `us_ex10_1_credit_agreement_2020.pdf` | Onboarding {good:Show} | 100 pages, LOAN_AGREEMENT; leverage ≤ 3.5 and ICR ≥ 2.5 on page 63; 47 s through the real service |
| `us_southcross_amendment_2018.pdf` | Amendment {good:Show} | AMENDMENT 0.9, skill `amendment_diff`; ICR ≥ 1.5 as an ADD change |
| `us_holly_amendment_2010.pdf` | Amendment | AMENDMENT 0.9; interest coverage change on page 16; 44 s through the real service |
| `us_core_labs_credit_agreement_2022.pdf` | Onboarding | 139 pages; leverage ≤ 2.75 on page 102 |
| `us_meet_group_credit_agreement_2019.pdf` | Onboarding | leverage thresholds and the pricing grid |
| `uk_dwf_senior_facilities_agreement.pdf` | Onboarding | 396-page UK LMA agreement; ICR and minimum EBITDA; 375 duty sentences |
| `in_au_emerging_enterprises_facility.pdf` | Onboarding | Indian bank form; minimum net worth, negative covenants |
| `in_idfc_first_*.pdf` (2 files) | Onboarding | standard forms with blanks; undertakings, no filled-in ratios |

@callout level=warning title="Say this plainly in the demo"
On real bank documents the rules alone find the clearly written covenants and put every other duty sentence in front of a person. Some rule quotes point at a contents line, and the reviewer rejects those. The agent settles most of the rest; it explained 100% on our 85-page test agreements on live Azure.

Four links on the list failed on 19 Nov 2026: three return "not found" (Workspace, Gresham House, DCB) and one fails its certificate check (South Indian Bank). The list has no statements, so the assessment flow uses our labelled statements in `golden-set\documents\`.

## How do I show the logs and the live demo? {#demo}

Follow the demo step by step with `docs/cts-dps/demo-walkthrough.md`: upload with `scripts\submit_job.py`, then show where everything is with `scripts\show_job.py` (add `--page N` for a page's parsed text).

| Log | What it shows | How to see it |
| --- | --- | --- |
| Run log | every DPS function a document enters, with file and line | `demo_onboard.py <file> --trace run.log`; sample [`docs/cts-dps/sample-run-AGR-001.txt`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/docs/cts-dps/sample-run-AGR-001.txt) |
| App logs | one JSON line per event with request, user, job and document | console of `dps-api` and `dps-worker`; [`dps/src/dps/ops/observability.py`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/dps/src/dps/ops/observability.py) |
| Document history | what DPS read, what the AI said, what people decided | `GET /dps/v1/documents/{id}/history` |
| Job steps | each step and attempt | `GET /dps/v1/jobs/{id}` |
| AI calls and cost | purpose, model, tokens, cost | `GET /dps/v1/ai-usage` |

```powershell title="Demo commands, run in cts-dps\dps"
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-001.pdf
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-001.pdf --trace run.log
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-007.pdf --json AGR-007.json
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-007.pdf --agent
.venv\Scripts\pytest -q
```

| What | Measured number |
| --- | --- |
| Live Azure, AGR-007 and AGR-008, 85 pages each | 100% of duty sentences explained, 0 unresolved; 165k and 241k tokens |
| Synthetic golden gate, 44 documents | recall, precision, thresholds and pages 1.0; values 0.9924 |
| 10 real agreements, draft labels, rules only | 72 of 75 covenants typed correctly; 74 of 75 reached a person |
| End-to-end run with a stand-in Spring | 88 jobs, 0 lost, 0 duplicated |

## Code to show: the job enters and is claimed {#code-queue}

@code source=submit language=python

@code source=claim language=python

@code source=fence language=python

## Code to show: the worker and the Flow {#code-worker}

@code source=runonce language=python

@code source=process language=python

@code source=flow language=python

## Code to show: onboarding and the extraction order {#code-runner}

@code source=onboard language=python

@code source=extract language=python

@code source=finish language=python

## Code to show: the agent and the model call {#code-agent}

@code source=makeagent language=python

@code source=agentread language=python

@code source=proposals language=python

@code source=gateway language=python

## Code to show: the checks that keep values honest {#code-checks}

@code source=grounded language=python

@code source=numbers language=python

@code source=ledger language=python

@code source=spring language=python

@code source=stamp language=python

## Code to show: the amendment rules {#code-amendment}

@code source=amend language=python

@code source=amendset language=python

## What is decided, and what is still open? {#decisions}

| Decision | Why |
| --- | --- |
| DPS-owned job table behind `POST /dps/v1/jobs` | validation, idempotency, no shared table layout; waiting for the architect's OK |
| Rules first, agent second, a person decides | rules are fast and repeatable; the agent finds what rules miss |
| Every AI answer must quote its page | no invented values |
| Whole-document ledger and second read | no covenant sentence disappears silently |
| Taxonomy defaults are suggestions only | an unconfirmed value is never written into a covenant |
| Java rules as batched tools | one rule engine in Spring |
| Local OCR, bank Azure for AI | files stay on premises; only selected page text goes to the bank's endpoint |

@callout level=open-question title="Open items that need another team"
The real Spring and DMS connection. Document sets that read an agreement with its amendments, proposed in [`docs/cts-dps/document-set-proposal.md`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/docs/cts-dps/document-set-proposal.md). Word and Excel locators for the review screen. Expert-verified labels for real agreements. The target VM and the SQL Server load test.

## Checklist before the handover session {#checklist}

- [ ] Install on the laptop with [`docs/cts-dps/install-and-demo.md`](https://github.com/vishwasshinde016-tech/cts-dps/blob/main/docs/cts-dps/install-and-demo.md), then run `pytest -q` and the golden gate.
- [ ] Run the AGR-001 demo and the run log.
- [ ] Start the API and the worker, and run `scripts\submit_job.py` on `us_ex10_1_credit_agreement_2020.pdf`.
- [ ] Walk the architecture panel step by step.
- [ ] Walk the onboarding and amendment panels against the two Miro boards.
- [ ] Open the database panel and the IDs table.
- [ ] Show the code panels in order: queue, worker, runner, agent, checks.

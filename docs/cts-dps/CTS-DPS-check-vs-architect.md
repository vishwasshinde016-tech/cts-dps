# CTS DPS: check against the architect's design

Checked on 17 Nov 2026 against 3 sources: the Miro flows (Onboarding, Amendment), the spec "01 - Document Upload"
and the Teams message (critical design decisions). DPS is reading only; Spring (green in Miro) and the user
(purple) are other teams. ✅ built · ⚠️ partly built, or works differently · ❌ missing.

## 1. Onboarding flow (Miro)

| # | Miro step | Owner | DPS status | Note |
| --- | --- | --- | --- | --- |
| 1 | Search for CP / Borrower → present in CTS DB? → fetch → get covenants | Spring | — | Spring screens and DB |
| 2 | Covenants are in stage → fetch extracted covenants from CTS DB | Spring | — | Spring keeps the staged covenants |
| 3 | Check if the doc is present in DMS → user uploads | DMS | — | DMS team |
| 4 | File upload checks: format, checksum, virus | DMS | ✅ second line | DMS scans first. DPS checks again before reading: real type, SHA-256, macros, scripts, encryption, zip bombs |
| 5 | Invoke the Onboarding flow → **check if a job is present in the DB / queue** → "extraction in progress" | Spring + DPS | ✅ | The same document version and job type returns the existing job (`duplicate: true`) with its status, so Spring can show "in progress" |
| 6 | **Create an entry in the Onboarding job table** | Spring (Miro) | ⚠️ different | In our design, Spring creates the entry through `POST /dps/v1/jobs`, and DPS owns the job table. Same result, but not a direct table insert. **Architect must confirm.** |
| 7 | Python worker invokes the CrewAI flow | DPS | ✅ | Workers poll the queue with leases. **Every job type runs as a CrewAI Flow**: agreement intake and statement processing have their own flows; ANALYZE, ONBOARD, sanction, certificate, amendment and commentary run through `SkillJobFlow` (run skills, then save the result). One flow run per job |
| 8 | AI functions 1: **classify and identify** (sanction, loan agreement, amendment) | DPS | ✅ | Inside the `ONBOARD` job (G1): rules decide, the Document Agent gives a second opinion (a different answer → `TYPE_DISPUTED`, a person confirms); image-only files are read by the vision route |
| 9 | AI functions 2: extract covenants | DPS | ✅ | Rules + the Document Agent (covenant skill, Java covenant tools); matched by metric; agent-only covenants go to a person |
| 10 | AI functions 3: extract schedule terms | DPS | ✅ | Rules + the Document Agent (schedule skill): testing calendar, reporting deadlines, repayment, interest reset, cure windows |
| 11 | Doc extraction completed → **call the API to update the covenants in the DB** for review | DPS → Spring | ✅ (G6, push or pull) | DPS sends an event and Spring **pulls** the result. Miro shows the extraction flow **calling Spring's API** (push). **Gap G6:** offer push as well. |
| 12 | User reviews and approves → persist as immutable → update schedule → end | Spring | ✅ supported | Spring keeps maker-checker. DPS gives page, quote, box and confidence for each value, plus a review API if the screen wants it |
| 13 | Exception flow | DPS | ✅ | Error codes, retry with back-off, DEAD list, quarantine flag, events to Spring |

## 2. Amendment flow (Miro)

| # | Miro step | Owner | DPS status | Note |
| --- | --- | --- | --- | --- |
| 1 | CP present? → fetch CP and covenants → fetch extracted covenants from CTS DB | Spring | — | |
| 2 | User requests amendment → uploads → file checks | User, DMS | ✅ second-line checks | |
| 3 | Invoke the Amendment flow → job entry → Python worker → AI functions | Spring + DPS | ✅ | `EXTRACT_AMENDMENT` |
| 4 | Find what the amendment changes | DPS | ✅ (G2) | Built: ADD / CHANGE / DELETE per covenant, clause, effective date, full set after the change. **But the base is DPS's own earlier extraction.** It should be the **approved covenants in the CTS DB**, which people may have corrected. **Gap G2.** |
| 5 | Call the API to update covenants for review → user approves amendments → persist → update schedule | Spring | ⚠️ | Same as onboarding step 11 (pull, not push: **G6**) |

## 3. Spec "01 - Document Upload": parts that touch DPS

| Spec rule | DPS status | Note |
| --- | --- | --- |
| Never overwrite; immutable version, SHA-256, object key | ✅ | DPS reads by version and key, checks SHA-256, never writes to `blobs/` |
| Folders: `derived/<tenant>/<version-id>/` | ✅ | Page models and page images, write-once |
| Folders: `audit-seals/` kept separately | ✅ (G5) | Seals are written under `derived/<tenant>/_seals/`. **Gap G5:** move them to their own `audit-seals/` folder. |
| Server-side authority; the filename is not trusted | ✅ | Tenant and scope come from the job; the filename is a label only |
| Local-first | ✅ | Files, page models and results stay on premises; only selected page text goes to the bank's own Azure OpenAI endpoint (allowed-host list) |
| Content and security checks; no parsing of unchecked files | ✅ | Inspection runs before any parser, inside a sandbox with no network |
| **Structural validation**: type, legibility, key pages, signature, reporting dates, identifiers, facility candidates, period, currency and units, **issuer / source**, **required fields** | ✅ | All built: type, legibility, **key pages** (`keyPages`, B1), signature, period, identifiers, currency, units, issuer (G3), required fields (G3) |
| Exact duplicate: a replayed request returns the original | ✅ | Idempotency key, plus the same bytes give the same ANALYZE |
| Uncertain → MAPPING_REVIEW; unsafe → quarantine | ✅ | Mapping problems (wrong entity, wrong type, another borrower's IDs) route `MAPPING_REVIEW` (B3); unsafe files carry `quarantine: true` |
| Mapping precedence: screen facility, then IDs, then OCR or fuzzy as suggestion only | ✅ | DPS returns match statuses; the DMS applies the precedence |
| Only vetted versions are extracted; human review governs covenant rules | ✅ | EXTRACT only on committed versions; high-risk fields always reviewed |
| Flow 3: restated evidence → new version, list what it affects | ✅ | `RESTATED` diff against the approved figures (G4) |
| Flow 3: validate period, legal entity, basis, units/currency, evidence type against the cycle; mismatch to review | ✅ | B2: `PERIOD_MISMATCH`, `BASIS_MISMATCH`, `ENTITY_MISMATCH`, `CURRENCY_MISMATCH`, `UNITS_MISMATCH` for ANALYZE, ONBOARD, certificates and statements (a statement is held before any value is sent) |
| Flow 4: escalate repeated failures and mapping-review backlog to operations | ✅ | B4: review backlog metric and alerts (`DpsReviewBacklogHigh`, `DpsReviewWaitLong`), `/ops/queues` → `review` |
| Flow 4 bulk: manifest; per-file results; error CSV; rerun only failed | ✅ | `POST /dps/v1/batches`, `errors.csv`, `rerun`. The e-mail notice is Spring's |
| Flow 5: AI may draft commentary, cannot decide compliance | ✅ | The agent drafts from the outcome manifest only (checked: no verdict it did not receive); the template is the fall-back |

## 4. Architect's Teams message

| Point | DPS status | Note |
| --- | --- | --- |
| Extraction schema | ✅ | `dps.result/1.0`, with page, quote, box, confidence and status on every value |
| PDF, Excel, Word; text or image | ✅ | Native text; local OCR and layout for scans; Excel and Word readers |
| Data quality, confidence | ✅ | Grounding, cross-checks, calibrated score, 3% random check |
| How the tools are designed | ✅ | Read-only tools; Java rules called in batches |
| Runtime, scalability, logging, exceptions | ✅ | One job per process, 2 pools, autoscaling signals, JSON logs and traces, error codes. End-to-end test: 88 jobs, 0 lost, 0 duplicated |
| 1. Same VM | ✅ | CPU and memory limits per container |
| 2. Communication via jobs | ✅ | Job API plus events (see G6 for push) |
| 3. Python worker polls the queue | ✅ | Lease-based claim; the race found in the end-to-end run is fixed |
| 4. Isolated context per job, small set of skills | ✅ | One process per job; one skill pack; sandbox |
| 5. One agent with multiple skills and tools | ✅ | **One CrewAI Document Agent** per job (`dps/agent/document_agent.py`): the job's skill gives the prompt, schema, tools and limits; tools are page tools and Java rules; model calls go through the gateway (Azure GPT, budgets, breaker, allowed hosts) |

## 5. Gaps found, and now built (17 Nov 2026)

| # | Gap | What we built | Status |
| --- | --- | --- | --- |
| G1 | Onboarding classifies and extracts in **one job** (Miro) | Job type `ONBOARD`: read once → classify → right pack (agreement, sanction, amendment) → covenants **and** schedule terms, classification and identifiers in one result. A document that is not an onboarding type (for example, a statement) comes back NEEDS_REVIEW with `TYPE_NOT_ONBOARDING`; a wrong chosen type is flagged `TYPE_MISMATCH`. Agreements also run the safety net: covenant-like sentences no rule explains go to a person. | ✅ Built, tested |
| G2 | Amendment must compare with the **approved** covenants | `context.baseCovenants` in the job request (Spring sends the approved covenants with `appliedCovenantId`). Every ADD / CHANGE / DELETE names the `appliedCovenantId` it touches. Without it, DPS falls back to its earlier extraction and flags `BASE_FROM_DPS`. | ✅ Built, tested |
| G3 | Structural validation: issuer and required fields | Identifier `ISSUER` with role LENDER, BANK or AUDITOR. A per-type required-fields check (`REQUIRED_MISSING`); for example, a certificate must show a period, a UDIN and a signature. Also in the `dps.analysis.completed` event. | ✅ Built, tested |
| G4 | Restated statements | Each line that differs from the figure approved earlier for the same borrower and period gets `RESTATED` (approved vs this version) and goes to review; one warning lists all affected lines. | ✅ Built, tested |
| G5 | Separate `audit-seals/` folder | Daily seals are written write-once to their own folder (`DPS_SEALS_DIR`, mounted separately in the compose file). | ✅ Built, tested |
| G6 | The flow **calls Spring's API** when extraction is done (Miro) | Optional push: with `DPS_EVENTS_PUSH_PATH` set, the leader worker POSTs every event, in order, to Spring (`POST /api/v1/dps-tools/events` in `spring-tools.yaml`). The cursor moves only after a 2xx (at least once; Spring de-duplicates by eventId). Pull stays available. | ✅ Built, tested |

## 5b. Built on 17 Nov 2026 (later): the architect's agent and the last spec gaps

| # | What | Status |
| --- | --- | --- |
| A1 | Document Agent: one CrewAI `Agent` per job with the job's skill (prompt, schema, tools, limits); page tools and Java tools; model calls through the gateway | ✅ Built, tested with a scripted model |
| A2 | Miro AI functions: classify (second opinion, vision for scans), covenants, schedule terms, amendment explanation | ✅ Built, tested |
| A3 | Quality: grounding, vote, challenger, Java formula check, scorer with agent signals; numbers never decided by the model | ✅ Built, tested |
| A4 | Safety: only selected pages; allowed hosts only; masking; no prompt text in logs; CrewAI telemetry off | ✅ Built, tested |
| A5 | Runtime: per-job token budget across CrewAI's threads, breaker, rate limits, cost per job, `AI_UNGROUNDED` and other codes, AI alerts | ✅ Built, tested |
| B1 | Key pages | ✅ Built, tested |
| B2 | Document vs what its cycle expects | ✅ Built, tested |
| B3 | `MAPPING_REVIEW` route | ✅ Built, tested |
| B4 | Review backlog for operations | ✅ Built, tested |
| — | One live run on the bank's Azure deployment, golden-set comparison rules vs rules + agent | To do (needs deployment names and key) |

## 6. Questions for the architect

| # | Question |
| --- | --- |
| 1 | Which Azure deployments for strong, challenger (another family if possible), small and vision? Bank approval to send selected pages, with zero data retention? |
| 2 | Job entry: may Spring create it through the DPS API instead of inserting into the table directly? |
| 3 | When extraction is done: should DPS call Spring's API (push), or should Spring read the result (pull)? Which Spring endpoint? |
| 4 | Onboarding: one job that classifies and extracts (Miro), and also the upload-time check before saving (spec)? We propose both. |
| 5 | Amendment: will Spring send the approved covenants with the job (G2)? |
| 6 | Flow 2 (borrower screen, ambiguous facilities) and the document roles are marked "to be discussed" in the spec: when? |
| 7 | Is a CP the same as a facility? Is the obligor the same as the borrower? |

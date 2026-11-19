# DPS review against the requirements and the Codex design (19 Nov 2026)

Documentation-led review of this DPS (Claude-built, repository `vishwasshinde016-tech/cts-dps`) against every
requirement source, and against the Codex DPS (design rev 1.3 and its paused fixture build in
`C:\Users\vishw\Documents\Codex\2026-11-18\can\outputs`, read only). It records what was read, what each requirement
asks, how each system answers it, the evidence, and what was changed here.

**Evidence labels:**
- **Measured:** run here, with the command and data named.
- **Built + tested:** code and an automated test exist.
- **Designed:** written down, not built.
- **Open:** an owner decision or an integration is missing.

Synthetic results and draft-labelled real documents are reported separately. Nothing here claims zero missed covenants.

## 1. Sources and review coverage

| Source | Kind | Read | Notes |
|---|---|---|---|
| `01 - Document Upload.docx` (architect) | Requirements | Full text and table | DMS principles, 5 flows, mapping precedence |
| Miro onboarding and amendment boards (`image-1794923894591.webp`, `image-1794923906313.webp`) | Requirements | Both images | Both boards also show **Assessment** as an entry, with no flow drawn |
| Architect's spoken decisions (Teams; recorded in `design.md`) | Requirements | Via `design.md` | Same VM, jobs, polling worker, isolated context, one agent with skills, 5 concerns |
| `docs/cts-dps/design.md` | Our design | §What the inputs ask, Decisions review, Boundaries, Fit with the bank app, Job model, Pipeline, Decision register, Gaps, Open questions | Agent, schema, confidence, API, security and tools sections read in earlier sessions only |
| `docs/cts-dps/CTS-DPS-check-vs-architect.md` | Our traceability | Full | |
| `docs/cts-dps/end-to-end-explained.md` | Our explainer | Full (written in this project) | |
| `go-live.md`, `runbook.md`, `model-inventory.md`, `CTS-DPS-overview-for-teams.md`, `CTS-DPS-schema-explained.md` | Our ops and explainer docs | Headings | No new requirements |
| `contracts/` (jobs API, spring-tools, core-additions, 9 schemas, 2 catalogues) | Our contracts | Schemas and catalogues edited and validated; YAML APIs by heading | |
| `benchmarks/held-out/README.md`, `scores-final-2026-11-08.txt` | Our evaluation | Headings and final scores | Statement silent-omission evidence (§3, F-S1) |
| `benchmarks/real-documents/README.md`, `golden-set/README.md`, `ANALYST-GUIDE.md` | Our evaluation | Headings | |
| Codex `dps-build-specification.md`, `dps-build-extraction.md`, `dps-build-document-set-and-skills.md`, `dps-design-decisions.md`, `dps-build-reconciliation-and-continuation.md` | Codex design | Full | |
| Codex `dps-build-traceability.md` | Codex requirement IDs | Requirement rows (A, W, O, M, C) | Used as the backbone of §2 |
| Codex `dps-design-final-findings.md`, `dps-design-recheck-2026-11-18.md` | Codex findings | Finding rows | |
| Codex `full-dps-audit.md` (audit of this DPS on 18 Nov), `cts-dps-architecture-review.md` | Codex review of ours | Full / §Decisions, Findings, Path | |
| Codex `dps-build-semantic-readiness.md`, `document_agent_profiles.yaml`, `document-agent-acceptance-cases.yaml`, `global-covenant-taxonomy-review.md` | Codex design | Readiness table / rules and profiles 1–310 / all 18 cases / "nothing will miss" sections | |
| Codex `dps-build-operations.md`, `dps-build-connectors.md`, `dps-build-data-and-queue.md`, `dps-build-tools-and-spikes.md` | Codex design | Headings, tables, operation names | |
| Codex `greenfield-*`, `dps-design-review-v1.2.md`, `dps-design-source-inventory.md`, `vendor-research.md`, `TAXONOMY-INTEGRATION.md`, `cts-dps-fix-and-verification.md` | Codex history and background | Headings / not re-read | Superseded by rev 1.3 per Codex's own register |
| Codex JSON schemas (result, tool, profile, reconciliation, openapi, semantic rules) | Codex contracts | Top-level keys only | Shapes summarised from the docs that define them |
| Codex `dps/` app: `README.md`, `REQUIREMENTS.md`, `IMPLEMENTATION_STATUS.md` | Codex build | Full / full / first 80 lines and checkpoint | |
| Codex `dps/src/dps/extraction.py`, `semantic_discovery.py`, `coverage_review.py`, `finding_skills.py`, `citations.py`, `taxonomy.py` | Codex code | Full or the functions named in §3 | `readers.py`, `processing.py`, `queue.py`, `reconcile.py`, `validation.py` by function list only |
| Codex `taxonomy-inventory.csv`, `taxonomy-type-audit.csv`, `default_taxonomy.original.yaml` | Data | Not read | The 228-type taxonomy is already merged here |
| **Unavailable** | | | Approved bank document corpus, live Spring, DMS and SQL Server endpoints, target VM, the bank's scanned agreements, expert-verified labels for real agreements (AGR-001 to 010 are `DRAFT_UNVERIFIED`) |

This DPS's code: every module changed in this review and its callers were read. The flows traced end to end are ONBOARD (runner), EXTRACT_AGREEMENT (intake flow), worker, job API and results. Other modules were read in earlier sessions and are not re-audited line by line here.

## 2. Requirement inventory and comparison

IDs follow Codex's traceability where it covers a source requirement. `CL-` rows are requirements from our own design that Codex's list does not separate out.

| ID | Requirement (source) | Claude design → implementation | Codex design → implementation | Evidence | Gap / decision |
|---|---|---|---|---|---|
| A01 | Spring and DPS on one VM, independent (architect) | Separate containers with limits → compose file | Separate service groups → local processes | Neither run on the target VM | **Open:** platform |
| A02 | Spring submits durable jobs; Python polls (architect) | `POST /dps/v1/jobs`, DPS-owned queue, lease claim → built | Same, SQLite → built (fixtures) | Ours: 88-job end-to-end run, CI on every PR | Kept ours. Architect to confirm the DPS-owned queue (Miro shows Spring writing the table) |
| A03 | CrewAI Flow per job, one Document Agent, scoped tools (architect) | Flow per job; one Agent role built per read with the pack's tools → built | Fresh agent per execution → built (fixtures) | Ours: live Azure runs | Kept. "One agent" (role vs instance) is still an architect question |
| A04 | Extraction schema and tool design (architect) | `dps.result/1.0`, tools as `BaseTool` → built | Typed skill envelopes, discriminated outputs → built | Ours: schema validated in tests | Kept ours. Adopted Codex's typed candidate states as the ledger (§4) |
| A05 | PDF text or image, Excel, Word (architect) | pypdf, RapidOCR, Docling, openpyxl, xlrd, python-docx → built | PDFium, RapidOCR/Tesseract, region OCR, rotation → built (fixtures) | Ours: 44-document golden set; Codex: 18/18 synthetic markers | **Gap (both):** no bank scans. Word gives no paragraph locator here (§5 R6) |
| A06 | Data quality and confidence (architect) | Grounding, vote, scorer, risk thresholds → built | Readiness truth table, field dispositions → built (fixtures) | Ours: golden gate | Adopted: zero findings → review, numbers re-checked, page accounting |
| A07 | Scalability, logging, exceptions (architect) | Pools, retries, DEAD, logs, alerts → built | Plan windows, shared budget → built (SQLite) | Ours: chaos test, CI | **Gap:** large-file windows are not built here (§5 R8) |
| A08 | Java tools if needed (architect) | Spring rules as batched tools → built (stub) | Conditional on a measured gap | – | Kept |
| W01 | Bounded intake, format, checksum, scan (DOCX) | DMS owns; DPS checks again (inspect, sandbox) → built | Same split | Ours: inspection tests | Kept. Added: submit hashes the stored file as a stream with a size cap |
| W02 | Immutable versions, SHA-256 (DOCX) | Read by version + hash, write-once `derived/` → built | Same | Tests | Kept |
| W03 | Many business links; same bytes in several scopes (DOCX) | Jobs dedupe on business context; tenant-scoped page models → built | Scoped identity → built | `test_document_release.py` | Fixed in this round (release hardening) |
| W04 | Candidate vs active; extraction only on vetted versions (DOCX) | Extract on commit; DMS owns activation → contract | Two explicit ordering profiles → designed | – | **Contradiction** (DOCX vs Miro) on when extraction starts. Open with the architect |
| W05 | Server-side authority and audit (DOCX) | Service roles, CP access via Core, audit seals → built | Grants on every read → built (fixtures) | Codex audit F04 | **Open:** tenant claims at the deployment |
| W06 | Local-first (DOCX) | Pages to the bank's Azure only → built | Same | – | Kept |
| W07 | Retire and purge (DOCX) | DMS → not DPS | Hold-aware derived artefacts → designed | – | **Open** (DMS and legal) |
| W08 | Mapping precedence (DOCX) | Identifiers + match status; DMS applies the order → built | Same | Tests | Kept |
| W09 | Assessment: entity, period, basis, units checks (DOCX Flow 3) | `PERIOD_/BASIS_/ENTITY_/CURRENCY_/UNITS_MISMATCH` → built | Same, plus header and cell evidence → built (fixtures) | Tests | **Gap (ours):** a statement line the reader did not find disappeared silently. Fixed here (§5 R5) |
| W10 | Bulk upload with manifest (DOCX Flow 4) | `POST /dps/v1/batches`, errors CSV, rerun → built | Spring owns batches → designed | Tests | Kept ours |
| W11–W12 | Frozen outcomes, restatement (DOCX Flow 5) | `RESTATED` lines; outcome freezing is Spring's → built | New linked drafts → built (fixtures) | Tests | Kept. Outcome freezing is open (Spring/DMS) |
| W13 | Roles (DOCX: open) | – | – | – | **Open** (architect) |
| O01–O03 | Search CP; staged covenants; DMS lookup (Miro) | Spring → not DPS | Same | – | Spring |
| O04 | Upload checks, re-upload (Miro) | DMS + DPS second line → built | Same | – | Kept |
| O05–O06 | Job present → "in progress"; else create the job (Miro) | Idempotent submit returns the running job → built | Same | Tests | Kept |
| O07 | Classify, extract covenants, extract schedule terms (Miro) | ONBOARD: classify (rules + agent), covenants (rules + 4 parts + residual), schedule terms → built | Fixture rules → built (fixtures) | §3 measurements | Kept ours (much stronger extraction). Adopted Codex's completeness controls |
| O08 | Completed → API to stage for review; else exception (Miro) | Event + pull; optional push → built | Pull and stage → designed | Tests | Kept |
| O09 | Review, persist immutable, update schedule (Miro) | Spring; DPS keeps a run per review → built | Spring | – | Spring |
| M01–M06 | Amendment flow (Miro) | `EXTRACT_AMENDMENT` against Spring's approved base; clause/id matching; schedule changes now read too → built | Baseline-pinned DIFF_AMENDMENT and waivers → built (fixtures) | Tests | **Gap (both):** waivers and amendment chains as one set (§5 R7) |
| C01 | Keep fixed runtime and three contracts (chat) | Kept | Kept | – | – |
| CL-1 | Never miss a covenant silently (our goal; Codex D01, D02) | Before: coverage only on pages sent to the agent; non-financial duties outside the rules were dropped in EXTRACT_AGREEMENT → **fixed** | Candidate ledger, residual route → built (fixtures) | §3 | Adopted and improved (§4, §5 R1–R2) |
| CL-2 | Qualifiers, conditions and references kept (Codex D06, profiles) | Not detected before → **built** | Built (fixtures) | Tests | Adopted |
| CL-3 | No invented values (Codex profiles) | Grounding; threshold check only in the AI reader → **now every pack** | Validator → built | Tests | Adopted. **Contradiction:** our approved decision fills taxonomy defaults (marked and reviewed); Codex never fills them. Kept the user's decision; listed for the owner |
| CL-4 | Incomplete processing never shown as complete (Codex readiness table) | Zero findings went to review in the agreement flow only → **every pack**; page accounting → built | Truth table → built (fixtures) | Tests | Adopted |
| CL-5 | Document sets: agreement + amendments + waivers (Codex D03, R16) | Not built | RECONCILE_SET → built (fixtures) | – | **Gap (ours).** Needs Spring's set manifest. Open (§6) |
| CL-6 | Large files: bounded windows, gaps listed (Codex D04, R17) | Batches inside one job; `PROMPT_LIMIT` and `PAGES_NOT_SENT` named | Windows with a shared budget → built (SQLite) | – | Partial. Windows not built (§6) |
| CL-7 | Evidence locators for Word and Excel (Codex A05) | Page and box for PDF; Word is one logical page | Paragraph, table and cell paths → built (fixtures) | – | **Gap (ours).** Open (§6) |

**Contradictions found between the documents:**
1. **When extraction starts.** The DOCX says after controlled activation. Miro starts it right after the upload checks. Both designs leave this to the architect.
2. **Who writes the job row.** Miro shows Spring creating an "Onboarding Job table" entry. Both designs use a DPS-owned queue behind an API.
3. **Taxonomy defaults.** This DPS fills them, marked and reviewed (the user's decision 5). Codex's profiles forbid filling. Recorded for the owner.
4. **Assessment.** It appears as a Miro entry point with no drawn flow. Only the DOCX (Flow 3) defines it.

**Missing in both systems:** the real Spring/DMS handoff, the bank's scanned and long agreements, expert-verified labels, target VM and SQL Server runs, borrower-screen mapping (Flow 2) and roles (W13).

## 3. Measurements on the same documents

The input is the same parsed page text from our parser and the same labels, for 20 labelled agreements. AGR-001 to 010 are real documents with `DRAFT_UNVERIFIED` labels (not expert-checked). AGR-011 to 020 are synthetic. Scripts are kept in the review scratch folder and their logic is described here.

- **Typed:** a covenant field on the labelled page (±1) with the same threshold.
- **Reached a person:** typed, or any field or review item there holding the threshold.
- **Review items:** fields sent to a person.

The Codex figures are **component level**: their clause router (`clause_route`) and typed reader (`local_covenant_terms`) on each sentence. Their PDF reader, windows and set reconciliation are not exercised.

| Set | Labels | Ours (rules only): typed / reached / review items | Codex: typed / reached / review items |
|---|---|---|---|
| Real, draft labels (AGR-001–010) | 75 | **72 / 74** / 215 | 0 / 73 / 1,490 |
| Synthetic (AGR-011–020) | 40 | **40 / 40** / 41 | 0 / 40 / 70 |

Reading the table:
- Codex's typed reader matches only its own fixture sentence shapes, so it typed none of these covenants.
- Its discovery reaches nearly every labelled covenant, but by sending about 600 sentences per 85-page agreement to review.
- Ours types 112 of 115 and holds the rest for review in a fraction of the volume.
- The one AGR-007 label "not reached" is a label artefact, not a miss. Its draft label cites the compliance-certificate form on page 55, which only restates the covenant. Ours extracted the covenant itself from clause 9.4 on page 35: minimum net worth INR 5,000 crore. The label should cite page 35. The table keeps the measured number.

**Live Azure** (rules + Document Agent, ONBOARD) on AGR-007 and AGR-008:

| | AGR-007 | AGR-008 |
|---|---|---|
| Duty sentences | 93 | 92 |
| Explained | 100%, 0 unresolved | 100%, 0 unresolved |
| Recovered by the residual read | 2 | 5 |
| Dismissed with a reason | 7 | 6 |
| Tokens | 165k | 241k |

Before this work, 77–82% were explained, and only on the pages sent to the agent.

**Synthetic gate** (CI): recall, precision, thresholds and pages 1.0; values 0.9924. Unchanged throughout.

## 4. What was adopted from Codex, what was kept, what was rejected

- **Adopted (and built on our engine):**
  - candidate ledger with one state per sentence;
  - residual second read;
  - qualifiers and references;
  - page accounting;
  - zero findings → review;
  - numbers re-checked outside the agent;
  - whole pages only in prompts;
  - acceptance cases as tests;
  - packaging smoke test;
  - shadow checks on legacy routes;
  - scoped dedupe;
  - streamed hashing.
- **Kept from ours:**
  - rules + agent + vote extraction;
  - 228-type taxonomy codes;
  - Spring tools;
  - scorer;
  - SQL Server-ready queue;
  - batches API;
  - golden gate;
  - live Azure path.

  On the same input ours types 112/115 labelled covenants vs 0/115.
- **Improved on both:**
  - the ledger groups unresolved sentences per page and kind, so review load stays bounded (Codex sends every candidate);
  - sentences covered by a covenant of the same kind are listed on it (`value.covers`) instead of folded in silently;
  - the agreement flow and ONBOARD now share one ledger implementation.
- **Rejected:**
  - Codex's fixture regex extraction (measured weaker);
  - a separate greenfield API (Spring already codes against ours);
  - copying their SQLite queue.

## 5. Changes made in this review

Each change has regression tests and passes the full suite and the golden gate.

| # | Defect or gap | Change | Files | Verification |
|---|---|---|---|---|
| R1 | ONBOARD: coverage checked only on pages sent to the agent, so a covenant elsewhere could be missed unreported | Whole-document candidate ledger; residual read; unresolved → review items | `skills/covenants/ledger.py`, `omissions.py`, `skills/runner.py` | `test_complete_coverage.py`; live AGR-007/008 |
| R2 | **EXTRACT_AGREEMENT: non-financial duties nothing explained were dropped silently** (safety net sees financial sentences only). Real case: AGR-001 end-use clause 2.1 | Same ledger in the agreement flow; residual read wired in the worker; coverage and page accounting as a job step the result reads | `flows/intake.py`, `worker.py`, `results.py` | `test_flows.py::test_build_runners_wires_real_parser` (AGR-001 end-use held for review) |
| R3 | A covered duty of the same kind could hide a distinct obligation | `value.covers` lists the sentences each covenant is taken to cover | `ledger.py`, result schema | `test_a_covered_duty_stays_visible...` |
| R4 | Prompt cut a page in the middle without saying so | Whole pages only; `PROMPT_LIMIT` names pages left out | `skills/common.py`, error catalogue | `test_a_page_that_does_not_fit...` |
| R5 | Statements: lines not found disappeared silently; only EBITDA, debt and net worth were required (held-out run: HU01, HU02, HU05 reported with missing lines) | Every catalogue period line not found is listed (`document.lines`, warning `LINES_NOT_FOUND`). It is visible but not blocking, so review load does not grow; the three required lines still hold the statement | `catalog.py`, `flows/statement.py`, `results.py`, result schema, error catalogue | `test_jobapi.py::test_statement_lines_not_found_are_listed_never_dropped` |
| R6–R8 | Word locators, document sets, large-file windows | Not built: need owner input or are larger slices (§6) | | |

Earlier in this round (PRs #1–#3):
- release hardening and the coverage controls;
- the package layout;
- the first-lien leverage fix and the prepayment-trigger fix;
- legacy shadow, dedupe and hashing fixes.

## 6. Remaining gaps and production dependencies

| Gap | Why not done | Owner / next step |
|---|---|---|
| Document sets (agreement + amendments + waivers, dated precedence) | Needs Spring's set manifest and the approved baseline per member | Spring/CTS; then a RECONCILE_SET slice |
| Large-file windows | Today one job reads within a 400k-token budget; no labelled 300+ page agreement to test on | DPS, when such documents arrive |
| Word paragraph and table locators; Excel cell locators | Contract change for the review screen | DPS + UI team |
| OCR resolution and rotation | No bank scans to measure on | Bank scans; then a measured bake-off |
| Taxonomy defaults vs never-fill | Owner decision | User / credit |
| Live Spring, DMS, SQL Server, VM, security sign-offs | External | Teams named in `design.md` |
| Expert-verified labels for real agreements | AGR-001–010 labels are drafts | Credit analysts (`golden-set/ANALYST-GUIDE.md`) |

## 7. Checklist and resume point

- [x] Sources read and inventoried (§1)
- [x] Requirement inventory and comparison (§2)
- [x] Same-input measurements, ours vs Codex components (§3)
- [x] R1–R4 built and tested
- [x] R5 statements: list every catalogue line not found
- [x] Investigate the AGR-007 labelled covenant that reached no one (label artefact, §3)
- [x] Update `design.md` decision register and `end-to-end-explained.md`
- [ ] Full suite, golden gate, PR, CI, merge

**Resume:** branch `review-slices`. If the PR is not merged yet, run the full suite and the golden gate, then ship with the ship script (push, PR, CI, merge). After that, §6 lists the next slices: document sets, large-file windows, Word and Excel locators, OCR on bank scans. Each needs the owner input named there first.

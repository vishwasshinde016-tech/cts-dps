# CTS DPS: what we are building, and how

For the architect and the DMS, Spring and UI teams. 17 Nov 2026.

## 1. In one line

**DPS** is a separate Python service. It reads loan documents (PDF, Excel, Word) with **rules first and the
architect's Document Agent** (a CrewAI agent on the bank's Azure GPT), and returns checked values with page, quote
and confidence.

- The agent **proposes**; rules, a second model, Java rules and a person **decide**. Every value must quote its page.
- Only the pages a job needs go to the model, and only to the bank's own Azure endpoint.
- What neither can read with certainty is **never guessed**: it goes to a person.
- The **DMS** (upload, virus scan, storage) stays inside Spring.

## 2. The architect's design decisions: our view

| What the architect said | Right? | What can go wrong | What we do |
| --- | --- | --- | --- |
| Spring and the reader on the same VM | ✅ | A heavy scan slows Spring down | CPU and memory limits per container; DPS can move to its own VM later with no code change |
| Talk through jobs | ✅ | If Spring writes into DPS tables, two teams own one table | Spring calls the DPS job API; DPS owns its tables and replies with events |
| Python worker polls the queue | ✅ | Two workers take the same job, or a crash loses it | Each job is locked to one worker (lease); after a crash, another worker finishes it exactly once (tested) |
| Isolated context per job, with a small set of skills | ✅ | A harmful file crashes or attacks the worker | One job per process, only its skill pack; files read in a sandbox with no network |
| One agent with many skills and tools | ✅ | The agent invents a value, or a document tells it to ignore its rules | **One CrewAI Document Agent**, fresh per job, with only that job's skill (prompt, schema, tools, limits). Every value must quote its page or it is dropped; rules vote; a second model re-checks high-risk fields; document text is treated as data, not instructions. |
| "All Python may not scale; use Java as a tool" | ⚠️ Half | Rewriting in Java gives no speed (the slow part is file reading and OCR, which already run in C code) and costs months | **Hybrid:** Python reads. Business rules stay in Java (formula check, borrower lookup, covenant list), called as **one batched tool call per document**. If Spring is down, the value goes to review and the job still finishes. |

**Why the hybrid is best**

| Option | Problem | Verdict |
| --- | --- | --- |
| All Python | The same rule lives in Java and Python and drifts apart | ❌ |
| All Java | No good Java libraries for OCR, tables and PDF safety; not faster | ❌ |
| Python reading + Java rules as tools | One copy of each rule; each language does its best job | ✅ |

## 3. How a document flows

| Step | Who | What |
| --- | --- | --- |
| 1 | DMS → DPS | `ANALYZE`: what type is this file, which borrower and period? (optional check before saving) |
| 2 | DPS → DMS | Type, IDs and match status; a wrong type or borrower is shown to the user |
| 3 | Spring → DPS | `ONBOARD` job after the file is saved (Miro onboarding: classify, then covenants and schedule terms), or `EXTRACT_*` for a known type; amendments carry the approved covenants |
| 4 | DPS | Rules read the file → each value checked on its page → Java tools check formulas and IDs → cross-checks → confidence |
| 5 | DPS → Spring | Event "completed" or "needs review": DPS pushes it to Spring's API, or Spring pulls it, then reads the result |
| 6 | Reviewer | Confirms, corrects or adds what the rules missed; DPS keeps every version and learns the wording |

## 4. Schema (the result every team reads)

One format for every document type: `dps.result/1.0` (`contracts/schemas/result.json`).

| Part | Holds |
| --- | --- |
| `jobId`, `jobType`, `status` | The job, and DONE / NEEDS_REVIEW / FAILED |
| `classification` | Document type, expected type, mismatch, basis, period type, confidence, evidence |
| `identifiers[]` | PAN, GSTIN, CIN, LEI, UDIN, account, facility, borrower, period, currency, unit, signature, with a match status: EXACT / STRONG / FUZZY / CONFLICT / NONE |
| `fields[]` | The extracted values (see below) |
| `document` | Overall route (AUTO_ACCEPTED / NEEDS_REVIEW) and reasons |
| `warnings[]` | Cross-check problems (for example, totals do not add up) |
| `pipeline` | Pipeline version and skill pack version (audit) |

**One field:**

| Key | Meaning | Example |
| --- | --- | --- |
| `group` | Kind of value | FINANCIAL_COVENANT, STATEMENT_LINE, CERTIFIED_FIGURE, AGREEMENT_TERM, SCHEDULE_TERM |
| `name` | What it is | DSCR |
| `value` | The value, typed by group | `{"metric": "DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES"}` |
| `source` | Where it was found | page 27, the quote, the box `[x0,y0,x1,y1]` or the Excel cell |
| `method` | Who found it | RULES (the rule that read it), MANUAL (a reviewer) |
| `confidence` | 0–1, with its top reasons | 0.71 (LOW_OCR_QUALITY, HIGH_RISK_FIELD) |
| `issues[]` | Problems found | FORMULA_INVALID, TOTALS_DO_NOT_ADD |
| `status` | Review state | AUTO_ACCEPTED, NEEDS_REVIEW, CONFIRMED, CORRECTED, REJECTED |

```json
{
  "schema": "dps.result/1.0", "jobId": "job-123", "jobType": "EXTRACT_AGREEMENT", "status": "NEEDS_REVIEW",
  "classification": {"type": "LOAN_AGREEMENT", "mismatch": false, "method": "RULES", "confidence": 0.97},
  "identifiers": [{"kind": "PAN", "value": "AAPFU0939F", "source": {"page": 1, "quote": "PAN AAPFU0939F"},
                   "match": {"status": "STRONG", "candidateIds": ["B-17"]}}],
  "fields": [{
    "id": "f-7", "group": "FINANCIAL_COVENANT", "name": "DSCR",
    "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES", "expression": "DSCR >= 1.2"},
    "source": {"page": 27, "quote": "shall maintain a DSCR of not less than 1.20 times", "bbox": [72, 410, 523, 468]},
    "method": "RULES", "confidence": 0.71,
    "issues": [{"code": "LOW_OCR_QUALITY"}], "status": "NEEDS_REVIEW"}],
  "document": {"route": "NEEDS_REVIEW", "reasons": ["1 field(s) need review"]}
}
```

**Other contracts:**

| File | For |
| --- | --- |
| `dps-jobs-api.yaml` | The API (submit a job, status, result, events, review) |
| `job-request.json` | What Spring sends |
| `review.json`, `review-decisions.json` | The UI review screen |
| `event.json`, `errors.yaml` | Events and error codes |
| `spring-tools.yaml` | 3 Java tools the Spring team adds |

## 5. Reading files and confidence

| Input | How it is read |
| --- | --- |
| PDF with text | Native text with positions |
| Scanned PDF (image) | Local OCR on our server; a local layout model for difficult tables; if still unreadable, the vision model reads the page image and a person confirms every value |
| Excel, CSV | Values, formulas, merged and hidden cells |
| Word | Paragraphs, tables, tracked changes |
| Every file first | Safety checks: macros, scripts, encryption, zip bombs, size |

| Confidence rule | How |
| --- | --- |
| Grounded | The quote must be found on its page (rules and agent alike) |
| Vote | Rules and agent agree → higher confidence; they disagree → a person sees both; agent-only high-risk values → a person |
| Cross-checks | Accounting checks (assets = liabilities + equity, PBT − tax = PAT), formula valid in Java, step-down dates in order, operator matches the wording |
| Score | Built from measured signals: grounded, OCR quality, rules found, agent found, rules and agent agree, cross-checks, wording seen before, risk level; calibrated on reviews |
| Routing | High-risk values always reviewed; others auto-accepted at ≥ 0.95 (medium risk) or ≥ 0.90 (low risk); 3% random check |
| Not readable | Left empty with the reason, and sent to review. **Never guessed.** |
| Learning | Reviewer corrections teach the rules new wordings (label and wording memory) |

## 6. Tools we use, step by step

| # | Step | Tool | What it does | Why this tool | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Receive the job | FastAPI, Pydantic v2 | The job API; checks every request against the schema | Fast, typed, generates OpenAPI | Final |
| 2 | Queue and state | SQL Server + SQLAlchemy, Alembic | Job queue with leases, results, audit; migrations | The bank already runs SQL Server | Final |
| 3 | Read the file safely | SHA-256 check, magic bytes | The right file, really the type it claims | Stops tampered or renamed files | Final |
| 4 | Safety check: PDF | pikepdf (qpdf) | Finds encryption, JavaScript, launch actions, embedded files; repairs broken PDFs | Industry-standard PDF engine | Final |
| 5 | Safety check: Office | oletools, zipfile limits | Finds macros, DDE fields, external links, zip bombs | Standard malware-analysis tools | Final |
| 6 | Sandbox | Separate process or container, no network | Parses the untrusted file with memory and time limits | A bad file harms one job only | Final |
| 7 | PDF text | pypdf, pdfplumber, pypdfium2 | Text, word positions and tables | Free, fast; 44/44 on the golden set | Provisional (tested) |
| 8 | Scanned pages | RapidOCR (runs locally) | Reads text from images, with confidence | Runs on CPU; no data leaves the server | Provisional (vs PaddleOCR, Tesseract) |
| 9 | Complex tables | Docling (runs locally, HEAVY pool) | Layout model for difficult tables | Strong on tables; runs on our server | Provisional (vs Marker, MinerU) |
| 10 | Excel | openpyxl (XLSX), xlrd (XLS) | Values, formulas, merged and hidden cells | Standard readers | Final |
| 11 | Word and CSV | python-docx, csv module | Paragraphs, tables, tracked changes; CSV with limits | Standard readers | Final |
| 12 | Page model | Our own JSON (`page-model.json`) | One common format for all file types, saved once | Every later step reads the same input | Final |
| 13 | Classify and identify | Our rules + validators (PAN, GSTIN, LEI, CIN, UDIN) | Document type and borrower IDs | Free, explainable; 41/41 on the golden set | Final (weights tuned) |
| 14 | Workflow | CrewAI Flows | Fixed steps per job; every job type runs as a Flow | Same path every time; the architect's choice | Final |
| 15 | Agent | CrewAI Agent (the Document Agent) on Azure OpenAI GPT | One agent per job with the job's skill, tools and prompt; proposes values with page and quote | The architect's design (1 agent, many skills and tools) | Final (models per route provisional) |
| 15a | Extraction | Skill packs: rules + the agent per document type | Covenants, statement lines, sanction terms, certificates, amendments, schedule terms | Rules are exact; the agent catches new wording | Final (rules grow with reviews) |
| 15b | Model gateway | DPS gateway over CrewAI `LLM` (LiteLLM) | Routes small, strong, vision; retries, breaker, budgets, masking, call log | One place to control cost and safety | Final |
| 16 | Business rules | Spring (Java) tools | Formula check, borrower lookup, covenant list | Rules already live in Java | Final (Spring team builds) |
| 17 | Answer check | Grounding + cross-checks | Every value found on its page; accounting and formula checks | Nothing invented | Final |
| 18 | Confidence | Our scorer (rule table, then logistic regression + isotonic calibration) | Score with reasons for every value | Explainable, measurable | Thresholds provisional |
| 19 | Logs and traces | JSON logs, OpenTelemetry, Grafana | Logs, traces, metrics and alerts | Already in the platform | Final |
| 20 | Security scans | bandit, semgrep, pip-audit, Trivy | Checks the code, dependencies and images on every build | Standard CI gates | Final |
| 21 | Deploy | Docker / Podman compose | Hardened containers on the bank VM | Same as the platform today | Final |

**Not used:** any model outside the bank's Azure tenant, Azure Document Intelligence, CrewAI telemetry. The agent
runs only with `DPS_LLM_ENABLED=true` and a model host on `DPS_AI_ALLOWED_HOSTS`. PyMuPDF is used only if its AGPL
licence is cleared.

## 7. Scaling and load testing

| Item | Plan |
| --- | --- |
| Workers | One job per process; add copies when the queue grows (oldest job waits over 60 s) |
| Pools | LIGHT (normal files) and HEAVY (scans: OCR and layout), scaled separately |
| Cost | CPU and memory on our servers, plus Azure tokens: a per-job token budget, a monthly budget and alerts; cost per job is logged |
| Model quota | Azure tokens per minute per route; workers back off on 429 and fall back to rules when the model is down |
| Sizing example | 100 documents in the peak hour → about 5 LIGHT + 5 HEAVY workers |

| Test | Result |
| --- | --- |
| Bad files blocked (scripts, exe, zip bomb, macros) | ✅ Passed |
| Worker crash mid-job: finished exactly once | ✅ Passed |
| 44 golden PDFs read the same as today | ✅ 44 / 44 |
| Document type found by rules only | ✅ 41 / 41 text documents |
| Agent with tools, grounding, vote, vision, budgets and outage fall-back (scripted model in CI) | ✅ Passed |
| Model calls refused to any host not on the allowed list | ✅ Passed |
| Live run on the bank's Azure deployment | To do (needs the deployment names and key) |
| End to end on this laptop: 88 real jobs (44 upload checks + 44 extractions) through the API, 4 workers | ✅ 0 lost, 0 duplicated; type correct 41/41 readable files; 0 false mismatch alarms |
| Processing time per job (laptop, 4 workers sharing the CPU) | p50 about 10 s, p95 about 45–70 s; slow scans move to the HEAVY pool |
| Bugs the end-to-end run found and we fixed | 2 workers could take the same job; held statements showed no values; slow scans failed instead of moving to HEAVY |
| Load at 2× peak for 1 hour; 24-hour soak | Scripts ready; to run on the bank VM |

## 8. Build status

| Phase | What | Status |
| --- | --- | --- |
| 1 | API, job queue, events | ✅ Built |
| 2 | Safe file reading, page model | ✅ Built |
| 3 | Type and borrower check (rules) | ✅ Built |
| 4 | Rule packs per document type + Java tools | ✅ Built |
| 5 | Confidence, review API, learning | ✅ Built |
| 6 | Production setup, alerts, audit | ✅ Built; load test on the bank VM pending |
| 7 | Pilot: shadow → assisted → normal | ✅ Built; pilot needs the bank |

## 9. Questions for the architect

| # | Question |
| --- | --- |
| 1 | Do you agree with the hybrid (Python reads, Java rules as tools)? |
| 2 | Will the Spring team build the 3 Java tools, and by when? |
| 3 | Spring sends jobs through the DPS API, and pulls events? |
| 4 | Is a CP the same as a facility? Is the obligor the same as the borrower? |
| 5 | Documents per day and at the peak hour; share of scans? |
| 6 | VM size for the OCR (HEAVY) workers? |
| 7 | Production database: a separate `cts_dps`? |
| 8 | Who may upload, approve and download? How long do we keep files? |
| 9 | Which documents and analysts for the golden set? |
| 10 | Which Azure deployments for the routes (strong, small, vision)? Has the bank approved sending selected pages to them? |

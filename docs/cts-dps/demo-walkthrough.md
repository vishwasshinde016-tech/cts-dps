# DPS demo walkthrough: upload a document and follow it through every step

*Read this file while you demo. It goes flow by flow. Each flow uploads a real document, and each step answers five questions: **Do** (what to type), **Inside** (what DPS does, in which file, with which library), **Where it is now** (folder or table), **Show** (how to prove it on screen) and **Say** (words you can read out). Lines marked **For you** are notes only, not for reading out.*

*Every file link opens the file at that line in VS Code (open this file with Ctrl+Shift+V, then Ctrl+click a link). Commands run in `C:\cts-dps\dps`.*

**The flows in this file:**

| Part | Flow | Document you upload | Minutes |
|---|---|---|---|
| 1 | Get ready (once, before the session) | none | 15, before |
| 2 | The picture in one minute | none | 2 |
| 3 | **Onboarding** (architect's onboarding board) | [`architect-documents\us_ex10_1_credit_agreement_2020.pdf`](../../architect-documents/us_ex10_1_credit_agreement_2020.pdf) (architect's list) | 20 |
| 4 | **Amendment** (architect's amendment board) | [`architect-documents\us_southcross_amendment_2018.pdf`](../../architect-documents/us_southcross_amendment_2018.pdf) (architect's list) | 8 |
| 5 | **Assessment** (statement) | [`golden-set\documents\FS-012.pdf`](../../golden-set/documents/FS-012.pdf) (ours: the architect's list has no statement) | 8 |
| 6 | When things go wrong | the same files | 5 |
| 7 | Where everything is: one table | none | 2 |
| 8 | Reset for a fresh demo | none | 1 |

**The documents you upload** are in the repository, so the demo needs no internet.
- **The architect's documents** are in [`architect-documents\`](../../architect-documents/README.md). Its README lists every file with its source link and the flow it shows.
- **Our own documents** are in `golden-set\documents\`. They are used only where the architect's list has nothing, which is the assessment flow.

In the commands, the path starts with `..\` because you run them from `C:\cts-dps\dps`.

---

## 1. Get ready (once, before the session)

### 1.1 Install

**Do** (once on the laptop):

```powershell
git clone https://github.com/vishwasshinde016-tech/cts-dps.git C:\cts-dps
cd C:\cts-dps\dps
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

**What setup created**, and where:

| Created | Where | What it is |
|---|---|---|
| Python environment | `C:\cts-dps\dps\.venv\` | all libraries: FastAPI, CrewAI, pypdf, pdfplumber, RapidOCR … |
| Settings | `C:\cts-dps\dps\.env` | local settings: SQLite database, local folders, dev sign-in, AI off |
| Database | `C:\cts-dps\dps\dps.db` | SQLite file with all 25 DPS tables (in the bank this is SQL Server schema `dps`) |
| Demo files (already in the clone) | `C:\cts-dps\architect-documents\` | the architect's list of public agreements, as PDF |

The folders `storage\` (the uploaded files) and `derived\` (what DPS makes from them) appear at the first upload.

### 1.2 What to open before you start

| Window | What | Command or address |
|---|---|---|
| Browser tab 1 | The one-page picture | open `C:\cts-dps\docs\cts-dps\diagrams\dps-complete.html` |
| Browser tab 2 | The API page (opens after step 3.1) | `http://localhost:9400/docs` |
| VS Code | The repository and this file | `code C:\cts-dps`, then open this file and press Ctrl+Shift+V |
| **Terminal A** | The DPS API | started in step 3.1 |
| **Terminal B** | The worker | started in step 3.2 |
| **Terminal C** | Your commands: upload, show | `cd C:\cts-dps\dps` |

**Shortcuts:**
- **VS Code:** Ctrl+` opens the terminal; Ctrl+Shift+5 splits it so A, B and C sit side by side. Ctrl+P opens a file and Ctrl+G goes to a line.
- **Terminal:** Up arrow repeats a command; Ctrl+C stops the API or the worker.
- **Browser:** Ctrl+plus zooms for the room.

> **For you:** no screen needs building for this demo. DPS is a service with no screen of its own; the review screen belongs to the Spring team's CTS UI. You show DPS with three things: the terminal (it really runs), the API page at `/docs` (what Spring calls), and the one-page picture (how it fits together).

---

## 2. The picture in one minute

> **For you:** to explain the flow block by block, open [`diagrams/dps-flow-explained.html`](diagrams/dps-flow-explained.html). Click a block to see what it does, what to say and its code; **Open in VS Code** opens the file at that line on this laptop.
>
> To explain the whole flow in one picture, open [`diagrams/dps-full-flow.html`](diagrams/dps-full-flow.html). It has 9 stages from upload to review, and every box shows its file:line. Use the view buttons to step through the stages.

**Show:** browser tab 1, the architecture diagram at the top of `dps-complete.html`.

**Say:**
> "In the bank, the RM uploads a document in the CTS screen. Spring stores it in the DMS and sends DPS a **job**. DPS reads the document in the background and proposes covenants, each with the page and the exact sentence it came from. Spring shows them to a person, who approves or corrects them.
>
> On this laptop there is no Spring and no DMS, so one small script plays their part: `scripts\submit_job.py`. Everything after that is the real DPS code, exactly as it will run in the bank."

---

## 3. Flow 1: Onboarding (the architect's onboarding board)

**Document:** [`architect-documents\us_ex10_1_credit_agreement_2020.pdf`](../../architect-documents/us_ex10_1_credit_agreement_2020.pdf). It is a real 100-page US credit agreement from the architect's list (SEC EDGAR, 2020).

### 3.1 Start the DPS API

**Do** (Terminal A):

```powershell
.venv\Scripts\uvicorn --factory dps.main:create_app --port 9400
```

Wait for `Uvicorn running on http://127.0.0.1:9400`. Then open browser tab 2 at `http://localhost:9400/docs`.

**Inside:** [`dps/src/dps/main.py:28`](../../dps/src/dps/main.py#L28) `create_app` builds the FastAPI app. The job endpoints are in [`dps/src/dps/api/jobs.py`](../../dps/src/dps/api/jobs.py).

**Show:** on the `/docs` page, scroll to `POST /dps/v1/jobs`. Open it and show the request body: `jobType`, `document`, `context`.

**Say:**
> "This is the DPS API, the door Spring knocks on. This one call, `POST /dps/v1/jobs`, is how every document enters DPS. Spring sends three things:
> - the job type, here ONBOARD;
> - where the file is, with its SHA-256 fingerprint;
> - the context: which CP, borrower and facility it belongs to.
>
> The API does no heavy work. It checks the request, saves the job and answers straight away."

### 3.2 Start the worker

**Do** (Terminal B):

```powershell
.venv\Scripts\python -m dps.worker
```

Wait for `DPS worker … started (pool=STANDARD)`.

**Inside:** [`dps/src/dps/worker.py:580`](../../dps/src/dps/worker.py#L580) `main` starts a loop. Each turn calls `run_once` ([`dps/src/dps/worker.py:131`](../../dps/src/dps/worker.py#L131)). When there is no job, it waits 2 seconds and looks again.

**Say:**
> "This is the Python worker, the architect's 'worker that polls the job table'. It asks the database every 2 seconds whether there is a job waiting. In the bank there can be several workers, and they never take the same job twice. I'll show why in a moment."

### 3.3 Upload the agreement (what Spring and the DMS do)

**Do** (Terminal C):

```powershell
.venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf --type LOAN_AGREEMENT
```

> **For you:** if DPS has already read this file (for example in a rehearsal), it returns that earlier job at once, marked `duplicate`, and you do not see the stages. Reset before the session (part 8) so the audience sees the full run.

It prints 4 numbered lines. Stop after line 2 and explain it.

**Line `1. DMS stored … as cb/cba19…pdf`.**
- **Inside:** the file is saved once under its SHA-256 by `FileStore.put` ([`dps/src/dps/ingest/file_store.py:14`](../../dps/src/dps/ingest/file_store.py#L14)).
- **Where it is now:** `C:\cts-dps\dps\storage\cb\cba19007…fecfa.pdf`, the laptop's stand-in for the DMS.
- **Say:** "The file is stored under its fingerprint, its SHA-256. The same bytes always get the same name, and the file is never changed again. The bank's DMS works the same way: one fixed copy per version."

**Line `2. Spring POST /dps/v1/jobs ONBOARD -> 202 jobId …`.**
- **Inside**, in order:
  1. [`dps/src/dps/api/jobs.py:312`](../../dps/src/dps/api/jobs.py#L312) `submit_job` receives the call.
  2. [`:166`](../../dps/src/dps/api/jobs.py#L166) `validate_job` checks the fields, and the stored file's SHA-256 is checked against the one Spring sent.
  3. [`:268`](../../dps/src/dps/api/jobs.py#L268) `_input_key` builds the duplicate key from the document and its context.
  4. [`:240`](../../dps/src/dps/api/jobs.py#L240) `create_job` writes one row in `dps_job` with status `QUEUED`, plus a row in `dps_document`.
  5. [`dps/src/dps/feed.py:39`](../../dps/src/dps/feed.py#L39) writes the event `dps.job.accepted` in the same transaction.
- **Where it is now:** table `dps_job` (status QUEUED), table `dps_document`, table `dps_event_feed`.
- **Say:** "Spring gets back 202, which means 'accepted, working on it', and a job ID. That took milliseconds. Spring is never kept waiting while DPS reads a 100-page document."

**Show where it is now** (Terminal C, while the job runs or after it):

```powershell
.venv\Scripts\python scripts\show_job.py
```

Point at section 1 (the `dps_job` row with the type, status and context Spring sent) and section 2 (the stored file's full path). Then open that folder in Explorer.

### 3.4 The worker claims the job

**Look at Terminal B:** log lines appear with the job ID.

**Inside:**
- [`dps/src/dps/jobs.py:85`](../../dps/src/dps/jobs.py#L85) `claim` runs one UPDATE: "set this job to RUNNING, owned by me, *only if it is still QUEUED*". Only one worker can win.
- The worker takes a **lease** (`locked_until`) and renews it with a heartbeat ([`dps/src/dps/jobs.py:162`](../../dps/src/dps/jobs.py#L162)).
- Every later write checks the attempt number ([`dps/src/dps/jobs.py:54`](../../dps/src/dps/jobs.py#L54) `guard_attempt`).
- One row goes into `dps_job_attempt`.

**Where it is now:** `dps_job.status` = RUNNING, `locked_by` = the worker's name, and a new row in `dps_job_attempt`.

**Say:**
> "The worker took the job with one atomic update. Two workers can never both get it. While it works, it renews its lease. If it crashes, the lease runs out and another worker takes the job over. The crashed worker's late writes are refused, because every write checks the attempt number."

### 3.5 The file is read, checked and parsed

**Inside**, in order:
1. **Read.** [`dps/src/dps/worker.py:330`](../../dps/src/dps/worker.py#L330) `_ingest` gets the bytes by object key, and [`dps/src/dps/ingest/pipeline.py:48`](../../dps/src/dps/ingest/pipeline.py#L48) `read` checks the SHA-256 again. A changed file is refused.
2. **Inspect.** [`dps/src/dps/ingest/inspect.py:63`](../../dps/src/dps/ingest/inspect.py#L63) checks the real file type (not the name). It refuses encrypted, broken, macro-carrying and zip-bomb files and checks the size and page limits. It uses **pikepdf** and **oletools**.
3. **Parse in a sandbox.** [`dps/src/dps/parse/sandbox.py:157`](../../dps/src/dps/parse/sandbox.py#L157) starts a separate child process with CPU, memory and time limits and **no network**.
4. **Build the page model.** In that process, [`dps/src/dps/parse/page_model.py:30`](../../dps/src/dps/parse/page_model.py#L30) `build` reads each page:
   - **pypdf** for the text layer;
   - **pdfplumber** for the position of every line, so the reviewer can see the exact spot;
   - for a scanned page with no text, **pypdfium2** draws it as an image and **RapidOCR** reads it on this machine;
   - Word goes through **python-docx**, Excel through **openpyxl** or **xlrd**, and CSV through Python's `csv`.
5. **Save the page model.** It is written once in `derived\` and listed in `dps_artifact`. If the same file comes again, the page model is reused, not rebuilt.

**Where it is now:**
- the page model file: `C:\cts-dps\dps\derived\default\v-cba19007448c\page-model\1\page-model.json.gz`;
- a row in table `dps_artifact`;
- the original in `storage\`, untouched.

**Show** the parsed text of the page that holds the covenants:

```powershell
.venv\Scripts\python scripts\show_job.py --page 63
```

**Say:**
> "This is what DPS extracted from the PDF: the text of every page, with the position of every line. It is stored once, in the `derived` folder, and the original file is never touched.
>
> Parsing runs in a locked-down child process with no network, so even a malicious PDF cannot reach anything.
>
> If a page were a scan, DPS would read it with RapidOCR, locally. No document goes to a cloud OCR."

### 3.6 The CrewAI Flow starts, classifies and picks the skill

**Inside**, in order:
1. **The Flow.** The worker starts one CrewAI Flow for this job: [`dps/src/dps/flows/skill_job.py:27`](../../dps/src/dps/flows/skill_job.py#L27) `SkillJobFlow`. This is the architect's "own context per job", and it saves its state after each step.
2. **The entry point.** [`dps/src/dps/skills/runner.py:611`](../../dps/src/dps/skills/runner.py#L611) `run_onboard`.
3. **Classify (AI 1 on the board).** [`dps/src/dps/analyze/classify.py:294`](../../dps/src/dps/analyze/classify.py#L294) scores the first pages for each document type with rules. Words like "Credit Agreement", "the Borrower" or "Events of Default" point to a loan agreement. If the score is clear, that is the type. If not, the Document Agent gives a second opinion and must quote the lines that prove it.
4. **Pick the skill.** [`dps/src/dps/skills/runner.py:608`](../../dps/src/dps/skills/runner.py#L608) `ONBOARD_PACKS` maps the type to a skill pack:
   - loan agreement → `covenants`;
   - sanction letter → `sanction`;
   - amendment → `amendment_diff`.

   For other job types, the pack whose `pack.yaml` lists that job type is used ([`dps/src/dps/skills/__init__.py:80`](../../dps/src/dps/skills/__init__.py#L80)).
5. **What the pack sets.** [`dps/src/dps/skills/covenants/pack.yaml`](../../dps/src/dps/skills/covenants/pack.yaml) holds the model route, the 5 tools allowed, up to 4 reads of 25 pages, the limits (8 tool calls, 3 model turns, 240 seconds, 400,000 tokens) and the risk of each field. `prompt.md` holds the instructions, and `schema.json` the answer shape.

**Where it is now:** in memory, inside the Flow. The type and the skill name will be written into the result.

**Show:** open [`dps/src/dps/skills/covenants/pack.yaml`](../../dps/src/dps/skills/covenants/pack.yaml) in VS Code.

**Say:**
> "Each job gets its own CrewAI Flow. First DPS decides what the document is; here the rules are sure it is a loan agreement. The type picks the **skill pack**, here `covenants`. A skill pack is a small folder:
> - `pack.yaml` sets what the agent may use and how much: which model, which tools, how many pages, how many tokens;
> - `prompt.md` is the instruction;
> - `schema.json` is the exact answer shape;
> - `rules.py` is what we find without any AI.
>
> Same agent, different skill per document type. That is the architect's 'one agent, many skills'."

### 3.7 Rules first, then the agent reads selected pages

**Inside**, in order:
1. **Rules (AI 2 on the board starts here).** [`dps/src/dps/skills/covenants/rules.py:61`](../../dps/src/dps/skills/covenants/rules.py#L61) reads the whole document with no model. It finds numbered clauses, ratio names, words like "not less than" or "shall not exceed", numbers and units. Here it finds Consolidated Leverage ≤ 3.5 and Interest Coverage ≥ 2.5 on page 63.
2. **Page finder.** [`dps/src/dps/skills/covenants/pages.py:98`](../../dps/src/dps/skills/covenants/pages.py#L98) ranks pages: where the rules found something, covenant sections, sentences with duty words ("shall maintain", "will not"), and the definitions they use. It packs them into up to 4 reads, one per part: financial, undertakings, reporting, events of default.
3. **The agent reads** (only when the AI is switched on):
   - [`dps/src/dps/agent/document_agent.py:277`](../../dps/src/dps/agent/document_agent.py#L277) `make_agent` builds a fresh CrewAI agent with the skill's prompt, schema and tools.
   - The agent may call its tools: `page_text` opens another page, `search_text` searches the document, `find_definition` finds "EBITDA means …", `covenant_library` and `validate_formula` ask Spring's Java rules.
   - Each model call goes through the gateway ([`dps/src/dps/agent/gateway.py:242`](../../dps/src/dps/agent/gateway.py#L242) `call_json`), which applies the budget, retries, circuit breaker, masking and allowed hosts, and sends the call to the bank's Azure OpenAI.

**Where it is now:**
- the selected pages go to Azure only when the AI is on, and only those pages;
- every model call is logged in table `dps_ai_call`;
- on this laptop the AI is off, so `show_job.py` section 5 says `0 call(s)`.

**Say:**
> "Rules go first. They are fast and free, and they give the same answer every time.
>
> Then the agent. It does **not** get the whole document. The page finder picks the pages that matter and sends them in four focused reads.
>
> The agent can open more pages, search, look up definitions, and ask Spring's Java rules. Every call goes through our gateway, with a token budget, and only to the bank's own Azure endpoint.
>
> On this laptop the AI is off, so you see the rules-only result. With the VPN, I add `--agent` and the agent's answers join in."

> **For you:** to show the agent, the laptop needs the VPN and the Azure settings in `.env` (see `install-and-demo.md`, section 5). Then run `.venv\Scripts\python scripts\demo_onboard.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf --agent`.

### 3.8 Every answer is checked, and nothing is missed

**Inside**, in order:
1. **Schema check.** The agent's answer must match `schema.json`, or it gets one repair ([`dps/src/dps/agent/document_agent.py:293`](../../dps/src/dps/agent/document_agent.py#L293)).
2. **Grounding.** Every value must quote its page, and the quote must really be on that page. If it is not, the value is dropped ([`dps/src/dps/skills/common.py:53`](../../dps/src/dps/skills/common.py#L53)).
3. **The number must be in the quote** ([`dps/src/dps/skills/runner.py:64`](../../dps/src/dps/skills/runner.py#L64) `check_numbers`).
4. **Vote.** If rules and agent agree, the field is `RULES+AI_CONFIRMED`. If they differ, it goes to review ([`dps/src/dps/skills/runner.py:223`](../../dps/src/dps/skills/runner.py#L223)).
5. **Ledger.** Every duty-like sentence in the **whole** document gets one state: found, covered, not a covenant, or unresolved ([`dps/src/dps/skills/covenants/ledger.py:146`](../../dps/src/dps/skills/covenants/ledger.py#L146)). Unresolved ones go to the agent for a second look ([`dps/src/dps/skills/covenants/omissions.py:82`](../../dps/src/dps/skills/covenants/omissions.py#L82)), then to a person.
6. **Java rules.** Formulas go to Spring's formula engine and names to the bank's covenant library, in one batched call ([`dps/src/dps/integrations/spring_tools.py:151`](../../dps/src/dps/integrations/spring_tools.py#L151)).
7. **Code and score.** Each covenant gets a taxonomy code such as `FIN_TOTAL_LEVERAGE` ([`dps/src/dps/skills/covenants/codes.py:34`](../../dps/src/dps/skills/covenants/codes.py#L34)). Each field gets a confidence, and risky or unsure fields always go to review ([`dps/src/dps/scoring/__init__.py:211`](../../dps/src/dps/scoring/__init__.py#L211)).

**Show** Terminal B. Because Spring is not running, you see `covenant library unavailable, using cached copy` and then `spring tools circuit opened after 5 failures`.

**Say:**
> "Nothing the AI says is trusted as it is. The quote must really be on that page, and the number must really be in the quote. The ledger lists every duty sentence in the whole document, so nothing can disappear without a person seeing it.
>
> Look at the worker log: Spring's Java rules are not running on this laptop, so DPS used its cached library, opened the circuit breaker, marked those checks for review and carried on. That is the designed behaviour when Spring is down."

### 3.9 The result is saved, and Spring pulls it

**Line `3. Worker QUEUED -> RUNNING -> NEEDS_REVIEW`** shows the stages. **Line `4. Result …`** shows the result.

**Inside:**
- [`dps/src/dps/skills/runner.py:557`](../../dps/src/dps/skills/runner.py#L557) `finish` saves the result as JSON (`dps.result/1.0`) in table `dps_run` and writes the event `dps.extraction.needs_review` in the same transaction.
- Spring reads `GET /dps/v1/events`, then `GET /dps/v1/jobs/{id}/result` ([`dps/src/dps/api/jobs.py:351`](../../dps/src/dps/api/jobs.py#L351)).

**Where it is now:** table `dps_run` (the result JSON), table `dps_event_feed` (the event), and `dps_job.status` = NEEDS_REVIEW.

**Show:**

```powershell
.venv\Scripts\python scripts\show_job.py
```

Point at sections 6 (the result: type, skill, how many fields of each group) and 7 (the two events). Then save the full result exactly as Spring receives it, and open it:

```powershell
curl.exe -H "X-User: cts-spring" -H "X-Roles: SERVICE" http://localhost:9400/dps/v1/jobs/<jobId>/result -o result.json
code result.json
```

In `result.json`, find the field `FIN_TOTAL_LEVERAGE` and show `value` (operator `<=`, threshold `3.5`), `source` (page 63 and the quote), `method`, `confidence` and `status`.

**Say:**
> "This is the result Spring receives. Each covenant carries its value, the page, the exact sentence, how it was found and how sure we are. Status is NEEDS_REVIEW: DPS never approves anything itself.
>
> The result and its event are saved together, in one transaction, so Spring never misses one."

### 3.10 What happens next (Spring's part)

**Say:**
> "From here it is Spring's side of the board:
> 1. Spring shows each field on its review screen with the page and the highlighted sentence.
> 2. The person accepts, edits or rejects each field. That comes back to DPS as `POST /dps/v1/jobs/{id}/review` ([`dps/src/dps/api/review.py:141`](../../dps/src/dps/api/review.py#L141)) and is saved as a new run; the old run never changes.
> 3. Spring saves the approved covenants and updates the schedule.
> 4. Spring runs the compliance calculation. **DPS never decides compliance.**"

### 3.11 The "extraction in progress" box: upload the same file twice

**Do** (Terminal C): run the same upload twice, quickly, one after the other:

```powershell
.venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf --wait 5
.venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf --wait 5
```

The second run prints the **same job ID** with `(duplicate: DPS already has a job for this document and context …)`.

**Inside:** [`dps/src/dps/api/jobs.py:268`](../../dps/src/dps/api/jobs.py#L268) `_input_key` builds a key from the document, its context and the pipeline version. If a job with that key already exists, queued, running or finished, DPS returns it instead of making a second one ([`dps/src/dps/api/jobs.py:284`](../../dps/src/dps/api/jobs.py#L284)).

**Say:** "This is the box on the architect's board, 'check if a job is present: covenant extraction is in progress'. The same document is never read twice at the same time."

---

## 4. Flow 2: Amendment (the architect's amendment board)

**Document:** [`architect-documents\us_southcross_amendment_2018.pdf`](../../architect-documents/us_southcross_amendment_2018.pdf), from the architect's list: the Sixth Amendment to a US revolving credit agreement (SEC EDGAR, 2018). A second amendment from the list, [`us_holly_amendment_2010.pdf`](../../architect-documents/us_holly_amendment_2010.pdf), works the same way.

**Do** (Terminal C):

```powershell
.venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_southcross_amendment_2018.pdf --job EXTRACT_AMENDMENT --base ..\docs\cts-dps\demo-kit\southcross-base.json
.venv\Scripts\python scripts\show_job.py
```

**What is different from onboarding:**

| Step | Onboarding | Amendment |
|---|---|---|
| Job type Spring sends | `ONBOARD` | `EXTRACT_AMENDMENT` |
| Extra context from Spring | none | `context.baseCovenants`: the covenants already approved in the CTS database, each with its `appliedCovenantId` |
| Skill pack | `covenants` | `amendment_diff` ([`dps/src/dps/skills/amendment_diff/`](../../dps/src/dps/skills/amendment_diff/)) |
| What it returns | covenants | each change as **ADD**, **CHANGE** or **DELETE**, plus `amendedSet`, the full covenant set after the amendment |

**Inside:**
1. Steps 3.3 to 3.5 are the same: store, job, claim, inspect, parse.
2. The skill pack `amendment_diff` reads the amendment. [`dps/src/dps/skills/amendment_diff/rules.py:56`](../../dps/src/dps/skills/amendment_diff/rules.py#L56) finds sentences such as "Section 7.11 is amended by deleting … and replacing it with …".
3. Each change is matched to an approved covenant by `appliedCovenantId` and clause. If the target is unclear, DPS does not guess: it raises `AMBIGUOUS_AMENDMENT_TARGET` for a person.
4. [`dps/src/dps/skills/amendment_diff/rules.py:145`](../../dps/src/dps/skills/amendment_diff/rules.py#L145) `amended_set` builds the covenant set after the change.

**Result on this file:** skill `amendment_diff/1.0.0`. With the sample approved covenants in [`demo-kit/southcross-base.json`](demo-kit/southcross-base.json), Interest Coverage ≥ 1.5 is reported as a **CHANGE** of the approved covenant `AC-SX-1` (≥ 2.0), with its page. Without `--base` the job is refused with `400 BASE_REQUIRED`: an amendment needs the approved covenants (or `--amends <agreement version>`).

**Say:**
> "Same pipeline, a different skill. For an amendment, Spring also sends the covenants that are already approved. DPS says, for each one, whether the amendment adds, changes or deletes it, and gives the full set after the change. If it cannot tell which covenant a change touches, it asks a person instead of guessing.
>
> On the laptop a small file plays the CTS database: it holds the approved covenants Spring would send. So the change shows as a CHANGE of the approved covenant, exactly as in the bank."

---

## 5. Flow 3: Assessment (a financial statement)

**Document:** [`golden-set\documents\FS-012.pdf`](../../golden-set/documents/FS-012.pdf), a balance sheet from our labelled test set. **The architect's list has no financial statements, so this one is ours.** Say so when you show it.

**Do** (Terminal C):

```powershell
.venv\Scripts\python scripts\submit_job.py ..\golden-set\documents\FS-012.pdf --job EXTRACT_STATEMENT --as-of 2026-03-31
.venv\Scripts\python scripts\show_job.py
```

`--as-of` is the period end of the statement, which Spring knows from the testing cycle.

**Inside:**
1. Steps 3.3 to 3.5 are the same: store, job, claim, inspect, parse.
2. The flow is the Statement Processing Flow, [`dps/src/dps/flows/statement.py:133`](../../dps/src/dps/flows/statement.py#L133). It finds each statement line (cash, inventory, total assets, long-term debt and the rest) with its value and unit.
3. It **derives** lines the covenants need. Here TOTAL_DEBT = LT_DEBT 2342.93 + ST_DEBT 618.32 = **2961.25**, and NET_DEBT = 2961.25 − CASH 487.34 = **2473.91** (INR crore).
4. Every expected line that is **not** found is listed (`LINES_NOT_FOUND`, [`dps/src/dps/catalog.py:65`](../../dps/src/dps/catalog.py#L65)), so a missing line is never silently zero.
5. Figures that differ from an earlier statement for the same CP and period are flagged `RESTATED`.
6. When a person confirms the figures, they are stored in table `dps_statement_value` and served to Spring through `POST /dps/v1/values`.

**Result on this file:** 15 lines with values in INR crore, and the warnings `LINES_NOT_FOUND` and `RESTATED`.

**Say:**
> "For assessment, DPS reads the borrower's statements into named lines with their values, and works out the totals the covenants need, like total and net debt. It lists every line it could not find, so nothing is silently zero. It also flags figures that changed from an earlier statement.
>
> Spring then uses these confirmed numbers to test the covenants. DPS gives the numbers; it does not decide compliance."

### More documents from the architect's list

You can upload any of these the same way with `submit_job.py`:

| File | What it shows |
|---|---|
| [`uk_dwf_senior_facilities_agreement.pdf`](../../architect-documents/uk_dwf_senior_facilities_agreement.pdf) | a 396-page UK LMA agreement: ICR and minimum EBITDA, and 375 duty sentences for the ledger |
| [`us_core_labs_credit_agreement_2022.pdf`](../../architect-documents/us_core_labs_credit_agreement_2022.pdf) | leverage ≤ 2.75 on page 102 |
| [`us_meet_group_credit_agreement_2019.pdf`](../../architect-documents/us_meet_group_credit_agreement_2019.pdf) | leverage thresholds and a pricing grid |
| [`in_au_emerging_enterprises_facility.pdf`](../../architect-documents/in_au_emerging_enterprises_facility.pdf) | an Indian bank's form: minimum net worth, negative covenants |
| [`in_idfc_first_bbg_term_loan_agreement.pdf`](../../architect-documents/in_idfc_first_bbg_term_loan_agreement.pdf) | an Indian standard form with blanks: undertakings only |

Four links on the architect's list could not be downloaded (Workspace, Gresham House, DCB, South Indian Bank). The list also has no sanction letter or compliance certificate, so those two flows have no demo file yet.

---

## 6. When things go wrong

| Situation | What DPS does | Where to see it | Say |
|---|---|---|---|
| Spring's Java tools are down | uses the cached library, opens the circuit breaker, marks the checks for review, finishes the job | Terminal B: `circuit opened` | "The job carries on; a person checks those fields." |
| Azure OpenAI is down or slow | falls back to the rules answer; the fields go to review | result warnings | "A model problem never fails a job." |
| The same file is sent twice | returns the running job (`duplicate`) | `submit_job.py` line 2 | "No double work." |
| A temporary error (database, network) | the job goes back to the queue with a growing wait, then retries | `show_job.py` section 4 (attempts) | "It retries by itself." |
| A file that crashes the parser again and again | the job becomes DEAD with `PARSER_CRASH`, and an event tells Spring | `dps_job.status`, event `dps.job.dead` | "One bad file can never block the queue." |
| A wrong or unsupported file | refused before parsing (`UNSUPPORTED_FORMAT`, encrypted, macro) | job error code | "Unsafe files are never opened." |

The error and issue codes are all listed in [`contracts/catalogues/errors.yaml`](../../contracts/catalogues/errors.yaml).

---

## 7. Where everything is: one table

| What | On the laptop | In the bank | Show with |
|---|---|---|---|
| The uploaded file | `dps\storage\<2 letters>\<sha256>.pdf` | the DMS (DPS only reads it) | `show_job.py` section 2 |
| The parsed page model | `dps\derived\default\<version>\page-model\1\page-model.json.gz` | the `derived/` volume | `show_job.py --page N` |
| The job | `dps.db` table `dps_job` | SQL Server schema `dps` | `show_job.py --list` |
| Each attempt | table `dps_job_attempt` | same | section 4 |
| Model calls and tokens | table `dps_ai_call` | same | section 5 |
| The result Spring pulls | table `dps_run` (`dps.result/1.0` JSON) | same | section 6, `GET /dps/v1/jobs/{id}/result` |
| Events for Spring | table `dps_event_feed` | same | section 7, `GET /dps/v1/events` |
| Document history | table `dps_trace_event` | same | section 8, `GET /dps/v1/documents/{id}/history` |
| Confirmed statement figures | table `dps_statement_value` | same | `POST /dps/v1/values` |
| Review decisions | table `dps_review_decision` | same | after Spring's review |
| Settings | `dps\.env` | the vault and container settings | VS Code |
| Logs | Terminals A and B | one JSON line per event, per container | the terminals |

The full database (25 tables, every column) is in the database panel of `dps-complete.html`.

---

## 8. Reset for a fresh demo

Do this after a rehearsal, so every upload runs in full instead of returning the earlier job as a duplicate.

Stop the API and the worker with Ctrl+C. Then:

```powershell
Remove-Item dps.db, storage, derived -Recurse -Force
.venv\Scripts\alembic upgrade head
```

This deletes only local demo data (the database and the two folders). Your settings (`.env`) and the `architect-documents\` folder stay.

---

*More detail on any step: `docs/cts-dps/presenter-guide.md` (each part of the code, Q&A, glossary). Install help: `docs/cts-dps/install-and-demo.md`.*

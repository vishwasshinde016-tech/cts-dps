# Install the DPS on a new laptop, and demo it

About 20 minutes. Windows commands shown (PowerShell); Linux and macOS use `.venv/bin/` instead of `.venv\Scripts\`.

## 1. What you need

- **Python 3.12** (3.11–3.13 work): <https://www.python.org/downloads/>. Tick "Add to PATH".
- **Git:** <https://git-scm.com/download/win>
- **Repository access:** `vishwasshinde016-tech/cts-dps` is private. Either:
  - the owner invites your GitHub user (Settings → Collaborators), or
  - you copy the repository folder from the owner.
- **Office networks:** if `pip` cannot reach the internet, ask IT for the proxy, then set it:

  ```powershell
  pip config set global.proxy http://proxy:port
  ```

- **Only for the AI part (optional):** network access to the bank's Azure OpenAI endpoint, and the Azure key from the vault. Everything else runs without them.
- **Only for the traces (optional):** Docker Desktop and about 6 GB of free memory, for Langfuse ([observability-demo.md](observability-demo.md)).
- **No internet on the laptop** (or PyPI, modelscope.cn or Docker Hub blocked): make an offline bundle on a laptop with internet first (section 2, "Offline").

## 2. Install

**Quick way (one command).** Clone into a short path such as `C:\cts-dps`. A long path, for example under OneDrive, can break `pip install` because of Windows' 260-character path limit.

```powershell
git clone https://github.com/vishwasshinde016-tech/cts-dps.git C:\cts-dps
cd C:\cts-dps\dps
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`scripts\setup.ps1` runs steps 2 to 4 below. It also puts the PP-OCRv6 English OCR model where DPS looks for it, checking its SHA-256; without it, scanned pages run words together. Then it runs the one-document demo and prints what is installed and what is missing. Options:
- `-Heavy` also installs the HEAVY reader: Docling in `.venv-heavy` and its layout, table and OCR models (about 2 GB).
- `-Observability` also starts Langfuse (traces) in Docker on this laptop and writes the `OTEL_*` lines into `.env` ([observability-demo.md](observability-demo.md)).
- `-Offline` installs everything from `..\offline-bundle` with no internet (see below).
- `-Tests` also runs the full test suite.
- `-Samples` also downloads the demo files (section 4).
- `-All` is `-Heavy -Observability -Tests` together.

**Offline (office laptop).**
1. On a laptop with internet and the same Python version (3.12), run `scripts\make-offline-bundle.ps1` (add `-Heavy` for Docling). It writes `..\offline-bundle\`: the Python wheels, the OCR model and the Docling models. It also writes the Langfuse images to `..\deploy\observability\images\`.
2. Copy the project together with those folders.
3. On the office laptop run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Offline -Observability
```

GitHub Copilot follows the same steps from `.github/copilot-instructions.md`.

**Step by step (the same, by hand):**

1. Get the code:

   ```powershell
   git clone https://github.com/vishwasshinde016-tech/cts-dps.git C:\cts-dps
   cd C:\cts-dps\dps
   ```

2. Create the virtual environment and install the packages:

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e ".[dev]"
   ```

   This installs the API, the worker extras (CrewAI, RapidOCR, PDF and Office readers) and the test tools.
3. Create the local settings file:

   ```powershell
   copy .env.example .env
   ```

   It is set up for local work: a SQLite database, local file storage, dev-header sign-in, AI off.
4. Create the database tables:

   ```powershell
   .venv\Scripts\alembic upgrade head
   ```

`.env` is ignored by Git. **Never commit it and never put keys in any other file.**

## 3. Check it works

1. Run the tests (about 10 minutes; they must say passed, with coverage at 90% or more):

   ```powershell
   .venv\Scripts\pytest -q
   ```

2. Run the code checks:

   ```powershell
   .venv\Scripts\ruff check .
   ```

3. Run the quality gate over 44 labelled documents. Expect `gate True`, recall 1.0.

   ```powershell
   .venv\Scripts\python ..\golden-set\predict.py --out ..\golden-predictions
   .venv\Scripts\python ..\golden-set\evaluate.py --predictions ..\golden-predictions --statuses SYNTHETIC
   ```

## 4. Demo (no Spring, DMS or database needed)

One command reads a document the way an `ONBOARD` job does and prints what DPS found:

```powershell
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-001.pdf
```

On AGR-001 (a 6-page loan agreement) it shows:

- **Classification:** LOAN_AGREEMENT (confidence 1.0).
- **Covenants:** 7, each with its taxonomy code, operator, threshold, page and status:
  - DSCR ≥ 1.25;
  - ICR ≥ 2.0;
  - Debt/EBITDA ≤ 3.5;
  - Debt/Net worth ≤ 2.5;
  - EBITDA/EMI ≥ 1.2;
  - reporting;
  - negative pledge.
- **A review item:** the end-use clause on page 2 ("proceeds shall be used exclusively for…"). The rules do not read it, and the whole-document ledger stops it being missed.
- **Coverage:** every duty sentence in the document is accounted for.

Other documents to show:

| Command | What it shows |
|---|---|
| `scripts\demo_onboard.py ..\golden-set\documents\AGR-007.pdf` | An 85-page agreement: many covenants, coverage across the whole document |
| `scripts\demo_onboard.py ..\golden-set\documents\AGR-007.pdf --agent` | The same with the Document Agent, if the `.env` Azure settings are in place: classification second opinion, 4 focused reads, second read of what is unexplained |
| `scripts\demo_onboard.py <file> --json result.json` | Writes the full `dps.result/1.0` JSON, the exact output Spring receives |

### Real agreements from the architect's list

The architect's list of public agreements is in the repository, in `C:\cts-dps\architect-documents\`. The list has 13 files, of which 9 could be downloaded:
- US SEC EDGAR credit agreements and amendments;
- UK LMA-style facility agreements;
- Indian banks' standard loan forms.

Its README lists each file with its source link. To fetch them again, run `scripts\fetch_public_samples.py` (it prints SEC pages to PDF with Edge or Chrome).

```powershell
.venv\Scripts\python scripts\demo_onboard.py ..\architect-documents\uk_dwf_senior_facilities_agreement.pdf --trace run.txt
```

What each file shows, with measured results, is in the demo panel of `docs/cts-dps/diagrams/dps-complete.html`.

## 5. Run the service (API + worker)

Use two terminals, both in `cts-dps\dps`.

1. In the first terminal, start the API:

   ```powershell
   .venv\Scripts\uvicorn --factory dps.main:create_app --port 9400
   ```

   API docs: <http://localhost:9400/docs>. Health check: <http://localhost:9400/health/ready>.
2. In the second terminal, start the worker. It polls the job queue and runs one CrewAI Flow per job.

   ```powershell
   .venv\Scripts\python -m dps.worker
   ```

3. In a third terminal, upload a file and run a real job, the way Spring and the DMS do. The script stores the file under its SHA-256 in `storage\`, posts the job, prints each worker stage, then prints the result:

   ```powershell
   .venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf --type LOAN_AGREEMENT
   .venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_holly_amendment_2010.pdf --job EXTRACT_AMENDMENT
   ```

Calls in dev mode use the headers `X-User: rm-anil` and `X-Roles: SERVICE`. The job API Spring calls is in `contracts/dps-jobs-api.yaml`.

**To switch on the Document Agent** (bank Azure), set these in `.env`:
- `DPS_LLM_ENABLED=true`;
- `DPS_AI_MODEL=azure/<deployment>`, or a routes file;
- `DPS_AI_ALLOWED_HOSTS=<the endpoint host>`;
- `AZURE_API_KEY`, `AZURE_API_BASE` and `AZURE_API_VERSION`, from the vault.

## 6. What to read when explaining the code

1. **The demo walkthrough (read it while you demo):** `docs/cts-dps/demo-walkthrough.md`. It covers uploading, where each file and table is, and what to say, flow by flow.
2. **The presenter guide (read it while explaining):** `docs/cts-dps/presenter-guide.md`. It covers the boards mapped to the code, each step with file and line, the agent, the Java wrap, the database, IDs, schemas, logs and code excerpts. Show `docs/cts-dps/diagrams/dps-complete.html` on screen beside it: the whole DPS on one page (open it in a browser).
3. **The whole flow in one diagram, with file paths:** `docs/cts-dps/end-to-end-explained.md`.
4. **Where each package lives:** `dps/README.md`, section "Code layout".
5. **The contract Spring calls:** `contracts/dps-jobs-api.yaml`. The result schema is explained in `docs/cts-dps/CTS-DPS-schema-explained.md`.
6. **Next step for the Spring team:** `docs/cts-dps/document-set-proposal.md`.

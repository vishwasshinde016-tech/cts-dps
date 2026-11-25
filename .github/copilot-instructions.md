# Instructions for GitHub Copilot (and other coding agents) in this repository

This is the CTS DPS (Document Processing Service), a Python 3.12 service. The Python package is in `dps/`
(source `dps/src/dps/`, tests `dps/tests/`). The contracts Spring uses are in `contracts/`. Documentation is in
`docs/cts-dps/`; start with `docs/cts-dps/demo-walkthrough.md`, `docs/cts-dps/presenter-guide.md` and `docs/cts-dps/diagrams/dps-complete.html`.

## Installing on a Windows laptop

Follow these steps exactly. Do not improvise other install methods.

1. Python 3.12 must be installed (3.11 and 3.13 also work). Git must be installed.
2. Clone into a **short path** to stay under Windows' 260-character path limit, for example `C:\cts-dps`:

   ```powershell
   git clone https://github.com/vishwasshinde016-tech/cts-dps.git C:\cts-dps
   cd C:\cts-dps\dps
   ```

3. Run the installer with `-Full`. It installs everything the full app needs:
   - `.venv` with the API, worker and dev packages;
   - `.venv-heavy` with Docling for the HEAVY worker (scans, long documents), plus its models (about 2 GB);
   - the PP-OCRv6 OCR model (SHA-256 checked);
   - `.env` from `.env.example`, and a prompt for the Azure OpenAI key;
   - the database tables (`alembic upgrade head`);
   - a one-document smoke demo.

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Full
   ```

   Other flags:
   - `-Tests` runs the full suite.
   - `-Samples` downloads the demo documents.
   - `-Observability` starts Langfuse; it needs Docker.
   - `-NoKeyPrompt` skips the key prompt; set `AZURE_API_KEY` in `dps\.env` yourself.
4. **Azure OpenAI key (required).** The API and workers will not start without it. The installer asks for it.
   Never type it into a file other than `dps\.env`, and never print it.
5. Start the full app (API + STANDARD worker + HEAVY worker, in the background, logs in `dps\logs\`):

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-dps.ps1
   ```

   It checks the key, starts the 3 processes, waits until `http://127.0.0.1:9400/health/live` answers, and prints
   where everything is. Then send one real job and look at its result:

   ```powershell
   .venv\Scripts\python scripts\submit_job.py ..\golden-set\documents\AGR-001.pdf
   ```

   Stop everything with:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-dps.ps1
   ```

6. If `pip` cannot reach the internet, ask for the office proxy and run
   `.venv\Scripts\pip config set global.proxy http://<proxy>:<port>`, then run the installer again.
   With no internet at all, use the offline bundle (`scripts\make-offline-bundle.ps1` on a connected laptop, then
   `setup.ps1 -Full -Offline`).

Every step is described by hand in `docs/cts-dps/install-and-demo.md`.

## Checks that must pass (run in `dps/`)

```powershell
.venv\Scripts\pytest -q                       # about 900 tests, coverage 90% or more (currently about 95%)
.venv\Scripts\ruff check .
.venv\Scripts\python ..\golden-set\predict.py --out ..\golden-predictions
.venv\Scripts\python ..\golden-set\evaluate.py --predictions ..\golden-predictions --statuses SYNTHETIC   # gate True
```

## Demo

```powershell
.venv\Scripts\python scripts\demo_onboard.py ..\golden-set\documents\AGR-001.pdf
.venv\Scripts\python scripts\fetch_public_samples.py           # the architect's list of real public agreements -> architect-documents\
.venv\Scripts\python scripts\demo_onboard.py ..\architect-documents\uk_dwf_senior_facilities_agreement.pdf --trace run.txt
.venv\Scripts\python scripts\submit_job.py ..\architect-documents\us_ex10_1_credit_agreement_2020.pdf   # a real job through the running API and worker
```

## Rules

- **Never commit `.env`** and never write keys, passwords or tokens into any file. The Azure keys come from the
  bank's vault and go only into `dps/.env`, which Git ignores.
- `architect-documents/` holds the architect's public demo documents (PDF), committed so the demo works offline. Add new third-party documents only when the owner asks.
- The API and workers require Azure OpenAI (`DPS_LLM_ENABLED=true` and `AZURE_API_KEY`), with network access to
  the bank's endpoint. Only the smoke demo (`scripts\demo_onboard.py`) and the tests run without it.
- Keep line length 120 (ruff). Tests must keep coverage at 90% or more.
- When a contract changes, keep `contracts/` and `dps/src/dps/contract/` identical.

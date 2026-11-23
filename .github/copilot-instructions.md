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

3. Run the installer. It creates `.venv`, installs `pip install -e ".[dev]"`, creates `.env` from `.env.example`,
   runs `alembic upgrade head` and a one-document demo:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```

   Add `-Tests` to run the full suite, and `-Samples` to download the demo documents.
4. If `pip` cannot reach the internet, ask for the office proxy and run
   `.venv\Scripts\pip config set global.proxy http://<proxy>:<port>`, then run the installer again.

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
- The AI part (`DPS_LLM_ENABLED=true`) needs network access to the bank's Azure OpenAI endpoint. Everything else runs
  without it.
- Keep line length 120 (ruff). Tests must keep coverage at 90% or more.
- When a contract changes, keep `contracts/` and `dps/src/dps/contract/` identical.

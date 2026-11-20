# DPS demo: every file in one place

Start here. This folder holds the demo script. Every other file the demo uses stays where the code and commands expect
it (moving them would break the commands and the document-set fingerprints), and is linked below.

Links are relative to the repository root. Commands are for the demo laptop, where the repository is cloned to `C:\cts-dps`: open PowerShell, run `cd C:\cts-dps\dps` once, then paste any command as it is.

## 1. The script

| File | Use |
|---|---|
| [DPS-demo-master-script.html](DPS-demo-master-script.html) | The master script: what to open, run, show and say, phase by phase. The links open the files below. |
| [DPS-demo-master-script.pdf](DPS-demo-master-script.pdf) | The same script as a PDF (its links point to the files on the presenter's laptop). |

## 2. Files you upload (live runs)

| Phase | File | Command (PowerShell, in `C:\cts-dps\dps`) |
|---|---|---|
| 3 | [architect-documents/us_ex10_1_credit_agreement_2020.pdf](../../../architect-documents/us_ex10_1_credit_agreement_2020.pdf) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\architect-documents\us_ex10_1_credit_agreement_2020.pdf --type LOAN_AGREEMENT --json C:\cts-dps\dps\ex10-result.json` |
| 3.5 | the same file again (duplicate check) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\architect-documents\us_ex10_1_credit_agreement_2020.pdf --type LOAN_AGREEMENT --wait 5` |
| 4a | [architect-documents/us_southcross_amendment_2018.pdf](../../../architect-documents/us_southcross_amendment_2018.pdf) + [demo-kit/southcross-base.json](../demo-kit/southcross-base.json) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\architect-documents\us_southcross_amendment_2018.pdf --job EXTRACT_AMENDMENT --base C:\cts-dps\docs\cts-dps\demo-kit\southcross-base.json` |
| 4b | [architect-documents/us_holly_amendment_2010.pdf](../../../architect-documents/us_holly_amendment_2010.pdf) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\architect-documents\us_holly_amendment_2010.pdf --amends v-holly-restated-2008` |
| 4c | [demo-kit/waiver-letter.pdf](../demo-kit/waiver-letter.pdf) + [demo-kit/base-covenants.json](../demo-kit/base-covenants.json) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\waiver-letter.pdf --job EXTRACT_WAIVER --base C:\cts-dps\docs\cts-dps\demo-kit\base-covenants.json` |
| 4c, 4d | [demo-kit/amendment-demo.pdf](../demo-kit/amendment-demo.pdf) + `base-covenants.json` | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\amendment-demo.pdf --job EXTRACT_AMENDMENT --base C:\cts-dps\docs\cts-dps\demo-kit\base-covenants.json` |
| 4d | [demo-kit/set-manifest.json](../demo-kit/set-manifest.json) + `base-covenants.json` (after 4c) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\set-manifest.json --job RECONCILE_SET --base C:\cts-dps\docs\cts-dps\demo-kit\base-covenants.json` |
| 4e | [demo-kit/agreement-two-companies.pdf](../demo-kit/agreement-two-companies.pdf) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\agreement-two-companies.pdf` |
| 4e | [demo-kit/guarantee.pdf](../demo-kit/guarantee.pdf) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\guarantee.pdf` |
| 4e | [demo-kit/restated-agreement.pdf](../demo-kit/restated-agreement.pdf) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\restated-agreement.pdf --restates v-agreement-2024` |
| 4f | [golden-set/documents/FS-012.pdf](../../../golden-set/documents/FS-012.pdf) (statement) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\golden-set\documents\FS-012.pdf --job EXTRACT_STATEMENT --as-of 2026-03-31` |
| 4g | [demo-kit/fake.pdf](../demo-kit/fake.pdf) (refused as `TYPE_SPOOFED`) | `C:\cts-dps\dps\.venv\Scripts\python.exe C:\cts-dps\dps\scripts\submit_job.py C:\cts-dps\docs\cts-dps\demo-kit\fake.pdf` |

More on the samples: [demo-kit/README.md](../demo-kit/README.md). The architect's list with sources:
[architect-documents/README.md](../../../architect-documents/README.md).

## 3. Pages you show the room

| Phase | File |
|---|---|
| 2, 5, 7 | [diagrams/dps-complete.html](../diagrams/dps-complete.html) (the Archify picture) |
| 5 | [diagrams/dps-full-flow.html](../diagrams/dps-full-flow.html) (the full flow: 14 rows, every skill, rules vs agent, DB tables) |
| 3, 6 | Swagger: `http://localhost:9400/docs` (after the API starts) |
| 3.4 | `dps/ex10-result.json` (written by the phase 3 run) |
| 3, 7 | `dps/dps.db` in DB Browser for SQLite: tables `dps_job`, `dps_event_feed`, `dps_run` |
| 6 | [contracts/spring-tools.yaml](../../../contracts/spring-tools.yaml) (what Spring builds) |
| 6 | [contracts/dps-jobs-api.yaml](../../../contracts/dps-jobs-api.yaml) (the job API) |
| 6 | [contracts/schemas/result.json](../../../contracts/schemas/result.json) (the result format) |
| 6 | [contracts/catalogues/errors.yaml](../../../contracts/catalogues/errors.yaml) (every error and warning code) |

## 4. For the presenter only

| File | Use |
|---|---|
| [diagrams/dps-flow-explained.html](../diagrams/dps-flow-explained.html) | Each block of the picture: what it does, what to say, its code |
| [demo-walkthrough.md](../demo-walkthrough.md) | Backup: the long version of every step |
| [presenter-guide.md](../presenter-guide.md) | Backup: code tour, questions and answers, glossary |
| [design.md](../design.md) | The full design, if asked |
| [document-set-proposal.md](../document-set-proposal.md) | Phases 4d and 8 |
| [install-and-demo.md](../install-and-demo.md) | Setup on a new machine |

## 5. Scripts and setup

| File | Use |
|---|---|
| [dps/scripts/submit_job.py](../../../dps/scripts/submit_job.py) | Uploads a file the way Spring and the DMS will, and prints the result |
| [dps/scripts/show_job.py](../../../dps/scripts/show_job.py) | Shows the latest job and its events |
| [dps/scripts/demo_onboard.py](../../../dps/scripts/demo_onboard.py) | Fallback: runs the same code in-process when a job is too slow for the room |
| [dps/.env.example](../../../dps/.env.example) | Copied to `dps/.env` once (SQLite, local storage, dev sign-in, AI off) |

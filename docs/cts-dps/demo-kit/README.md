# Demo kit

Small inputs for the live demo, for the flows the architect's document list has no file for. The live flows use the
architect's own documents in `architect-documents/` wherever one exists (onboarding, amendments).

| File | What it is | Command (in `dps/`) |
|---|---|---|
| `southcross-base.json` | Sample approved covenants Spring would send with the architect's Southcross amendment (values illustrative) | `python scripts\submit_job.py ..\architect-documents\us_southcross_amendment_2018.pdf --job EXTRACT_AMENDMENT --base ..\docs\cts-dps\demo-kit\southcross-base.json` |
| `base-covenants.json` | Approved covenants of the sample loan: DSCR `AC-11` (>= 1.20, clause 21.2), ICR `AC-12` (>= 2.00, clause 21.5) | used with the three files below |
| `amendment-demo.pdf` | Amendment of the sample loan: DSCR to 1.10, ICR deleted, current ratio added | `... amendment-demo.pdf --job EXTRACT_AMENDMENT --base ..\docs\cts-dps\demo-kit\base-covenants.json` |
| `waiver-letter.pdf` | Waiver of the DSCR breach for the quarter ending 31 Mar 2026, with a condition and an expiry | `... waiver-letter.pdf --job EXTRACT_WAIVER --base ..\docs\cts-dps\demo-kit\base-covenants.json` |
| `set-manifest.json` | Document set: agreement, the amendment and the waiver, in force on 30 Jun 2026. Its version ids are `v-` + the first 12 hex of each file's SHA-256, as `submit_job.py` sends them | `... set-manifest.json --job RECONCILE_SET --base ..\docs\cts-dps\demo-kit\base-covenants.json` (after the amendment and the waiver) |
| `agreement-two-companies.pdf` | A Borrower's and a Guarantor's DSCR, and a springing FCCR | `... agreement-two-companies.pdf` |
| `guarantee.pdf` | A deed of guarantee with a net worth covenant | `... guarantee.pdf` |
| `restated-agreement.pdf` | An amended and restated agreement | `... restated-agreement.pdf --restates v-agreement-2024` |
| `fake.pdf` | A text file named `.pdf` (refused as `TYPE_SPOOFED`) | `... fake.pdf` |

Regenerating a PDF changes its SHA-256; update `set-manifest.json` if you do.

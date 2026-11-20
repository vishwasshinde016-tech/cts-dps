# Held-out measurement (phase 5): how to run it, and the monthly fresh sample

The first benchmark (`../real-documents/`) was used to tune DPS, so its numbers are optimistic. This folder
measures DPS on documents it has **never seen**: no rule, wording or test was written from them. Re-run it every
month on a fresh sample, so a change that helps the old documents but hurts new ones shows up.

## What is measured

| Metric | Agreements | Statements |
|---|---|---|
| **Missed** | a true covenant that no proposal, flag or possible covenant shows to a person | a true figure not read at all |
| **Silent wrong** | a wrong covenant or number offered with **no flag** | a wrong figure in a statement that **published without review** |
| **Review rate** | share of agreements held for review | share of statements held for review |

Target before production: at most **1 silent wrong in 200** auto-published figures or clean terms.

## Steps

All downloads use SEC's public endpoints or the company's / exchange's own site, with the honest User-Agent
`cts-dps-benchmark research@cts-dps.local`. The documents are not committed (third-party copyright).

1. **Pick new documents.** `harness/fetch.py <dir> agreements 20` takes credit agreements from EDGAR, filed in the
   window it states, from companies in neither earlier sample (it reads `../real-documents/sources.tsv`; add each
   month's companies to the exclusion list). `harness/fetch.py <dir> statements` takes the latest 10-K of the
   companies in `TEN_K`; change them every month. Add India and Sri Lanka statements by hand from company or
   exchange sites (list them in a TSV and fetch with `../real-documents/harness/fetch.py`).
2. **Make PDFs** of HTML filings with the network blocked:
   `node html2pdf.cjs ui/node_modules/playwright <dir> <pdf_dir>`. An exhibit whose tables are images loses them;
   note it in the answer key and leave that covenant out.
3. **Write the answer keys before running DPS.** US statements: `harness/truth_us.py` (from XBRL, no person).
   Agreements and India / Sri Lanka statements: read the documents and write `truth/*.json` by hand, following the
   `rules` field at the top of each file. Do not look at DPS's output first.
4. **Run the real flows** in the worker containers (a throwaway SQLite database, Core read only):
   `python run_flows.py out.json agreement <pdfs>` and `... statement <pdfs>`; add `--ai` for the optional AI
   readers. Documents with outcome `HEAVY` are run again in `dps-worker-heavy` with `--heavy`.
5. **Score**: `python harness/score.py out.json [heavy.json]`. Write the numbers in `README.md` with the month,
   and save every miss and silent error as a test case.

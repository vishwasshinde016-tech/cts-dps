# Real-document benchmarks

Measurements of DPS on real documents it was not written from: credit agreements filed on SEC EDGAR, US 10-K
statements, and Indian and Sri Lankan statements. Brought over from the earlier repository on 18 Nov 2026.

| Folder | What |
| --- | --- |
| `real-documents/` | The first benchmark (50 EDGAR agreements and statements). DPS was tuned on it, so its numbers are optimistic. |
| `held-out/` | 20 agreements and 10 statements DPS had never seen. The honest measure; repeat monthly on a fresh sample. |

Each folder has `sources.tsv` (where each document comes from), `truth/` (the answer keys, written before DPS ran),
`harness/` (fetch, run and score scripts) and `results/` (earlier runs). See `held-out/HOW-TO-RUN.md`.

The documents themselves are not in this repository (third-party copyright): `harness/fetch.py` downloads them from
SEC EDGAR with a generic User-Agent, and HTML filings are turned into PDFs before a run.

The measure that matters: **missed** (a true covenant no proposal, flag or possible covenant shows a person) and
**silent wrong** (a wrong value with no flag). Target: 0 missed, at most 1 silent wrong in 200.

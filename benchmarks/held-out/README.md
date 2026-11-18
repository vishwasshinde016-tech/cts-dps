# Held-out measurement, November 2026 (phase 5 of "never miss silently")

**Question:** how does DPS do on documents it has never seen? The first benchmark (`../real-documents/`) was used to
tune DPS, so its numbers flatter it. Here no rule, wording or test had been written from these documents before
the run.

**Answer, in one line:** a person sees almost everything (41 of 44 covenants after the fixes, and all 44 once
thresholds given by defined words were read), and DPS holds most
documents for review. But on the first run 5 of 60 statement figures were published wrong with no warning, and
1 covenant term was wrong with no flag. Each of those had a general cause, and all are fixed. On the same documents after the fixes, 1
published figure is still off (a definition difference). This sample is far too small to prove the target of at
most 1 wrong in 200 published figures, so the measurement must be repeated each month on fresh documents
(`HOW-TO-RUN.md`).

## The documents (30, `sources.tsv`)

| Set | Count | What |
|---|---|---|
| Agreements | 20 | Credit and loan agreements filed on EDGAR in 2025-2026, from 20 companies not in the first benchmark (mortgage and mezzanine loans, bank revolvers, term loans, a Thai bank loan, a railcar financing). HA06 is not scored: its covenant section is an image in the filing. |
| US statements | 6 | The latest 10-K of Hershey, Kimberly-Clark, Lennox, Toro, Leggett & Platt and Brunswick. Answer key from SEC XBRL (`harness/truth_us.py`), no person involved. |
| India | 2 | Reliance SED Limited (figures in "Rs. In Hundreds"); an MCA AOC-4 filing form of Vitagreen Products, which holds **no** figures. |
| Sri Lanka | 2 | Kotagala Plantations PLC annual report (group and company columns, three balance-sheet dates); Alliance Finance PLC's newspaper-style publication (English and Tamil, one page). |

Answer keys: `truth/`. The agreement and India / Sri Lanka keys were written by reading the documents, before
DPS was run. Every document went through DPS's real flows (Flow A, Flow B) with the production settings (AI
readers off) in a throwaway database (`harness/run_flows.py`). Documents handed to the heavy pool were run again
there, one at a time.

## Results

### What a person sees, agreements (18 read, 44 true covenants)

The answer key first had 43 covenants. The AI run (below) showed one that the key had missed: HA19's "(f)
Obligors' Net Worth Test ... not less than $384,526,500". It was added on 8 November 2026, and every run below
was scored again with it.

| | First run | After the fixes |
|---|---|---|
| Found with no flag (offered as clean) | 2 | 2 |
| Found and flagged for a check | 12 | 12 |
| Shown as a possible covenant or nearby term | 24 | 27 |
| **Missed (nothing shown)** | **6** | **3** |
| **Wrong term with no flag (silent)** | **1** | **0** |
| Agreements held for review | 18 of 18 | 18 of 18 |

Example of a miss that is now shown: Stewards' loan (HA01) says the guarantor "fails to maintain at all times
Liquid Assets of at least $1,500,000". No rule knew "Liquid Assets", and the safety net did not count it as a
covenant word. Now it appears as a possible covenant on page 73.

The 3 still missed after the first fixes (now shown, see below): HA16 (railcar loan) states its limits through
defined words ("the LTV Maximum Ratio" means 73.0%, "the DSCR Minimum" means 1.15), so the covenant sentence holds
no number. HA10 writes "a Liability to Equity Ratio of not greater than one-to-one" in words.

### Thresholds given by defined words (8 November 2026)

The last 3 misses had no number in the covenant sentence:
- HA16 (a railcar term loan) says "If the Loan-to-Value Ratio ... exceeds the LTV Maximum Ratio" and "If the DSCR ...
  is less than the DSCR Minimum". The numbers are in the definitions: "LTV Maximum Ratio" means 73.0%, "DSCR
  Minimum" means one point one five to one (1.15:1.00).
- HA10 (Biglari) writes its limit in words: "a Liability to Equity Ratio of not greater than one-to-one".

What DPS does now:
- It collects every definition whose whole meaning is one number. A formula ("the sum of $105,000,000 and ..."), a
  cash-sweep trigger or a pricing margin does not count.
- Where a covenant compares against such a word ("exceeds the LTV Maximum Ratio"), DPS proposes the covenant with
  that number. It always sends it to a person with the flag `THRESHOLD_FROM_DEFINITION`, and the review screen says
  where the number came from.
- A definition that is one number is no longer read as a covenant by itself. Before, "'LTV Cash Sweep Ratio
  Threshold' means 71.0%" became an LTV covenant of 71%.
- Ratios in words ("one-to-one", "one and one-half to one") are read as numbers, and the safety net counts them.

Result on the 18 agreements: **44 of 44 covenants shown to a person**: HA16's two limits read and flagged, and
HA10's ratio shown as a possible covenant (DPS has no "liabilities to equity" metric). There is still no wrong term
without a flag. On the first benchmark's 50 agreements the change adds one right covenant (AG31, leverage 3.75 set
through "Maximum Total Leverage Ratio" means 3.75:1.00) and loses none. Files: `results/scores-after-defined-thresholds.txt`,
`results/run-defined-thresholds.json`.

### Statements (10 documents, 133 true figures)

| | First run | After the fixes |
|---|---|---|
| Figures published right (statement not held) | 55 | 60 |
| **Figures published wrong (silent)** | **5 of 60** | **1 of 61** |
| Figures right, statement held for review | 43 | 51 |
| Figures wrong, statement held for review | 12 | 6 |
| Figures not read | 18 | 15 |
| Statements held for review | 6 of 10 | 6 of 10 |
| The form with no figures published anything | no | no |

### What was wrong, and the fix (each one is a test in `dps/tests/test_held_out.py`)

| Document | What happened | Cause | Fix |
|---|---|---|---|
| Kotagala (HL01) | Operating cash flow published as 120.3 (1,202,826 thousand); right is 45.6 | "Cash Generated from Operations" (before interest, tax and gratuity paid) was taken; "Net Cash from Operating Activities" came later | the net line replaces it |
| Kotagala (HL01) | Long-term debt published as 8.0; right is 44.9 | "Proceeds from Long Term Borrowings", a cash-flow line, was read as the debt | proceeds and repayments are never a balance |
| Kotagala (HL01) | Current liabilities published as 77.0; right is 231.0 | the balance sheet went on to a second page, which was not read; a segment note's "Current liabilities" (tea only) was taken | the next page is read when the balance sheet has no liabilities total |
| Kimberly-Clark (HU02) | Net income published as 19.6; right is 202.1 | "Share of net income of equity companies" was read as the net income | a share of associates' or minority income is not the company's |
| Reliance SED (HI02) | Every figure 100 times too small (held for review) | "Rs. In Hundreds" was not a known unit | "hundred" is a unit |
| KB Home (HA09) | "Fixed Charge Coverage Ratio ≥ 9" with no flag | read from "to at least 9 decimals" in an assignment form | a number that counts decimals, days, months... is not a limit |
| Several (HA02...) | The quote shown with a term was unrelated text ("flood insurance...") | a paragraph with no line breaks was cut to its last 300 characters | the quote keeps the words around the match |
| HA04, HA08, HA01 | Three covenants not shown at all | "shall not permit X to be less than 2.50", a "(f) ... of less than 1.15" list item and "Liquid Assets" were not recognised as covenant-shaped | the safety net recognises them. On the first benchmark this adds 7 possible covenants, all real covenants |

The one figure that was still published wrong: Hershey's (HU01) "Interest expense, net" is 190.2 million, while
the XBRL key holds gross interest expense, 224.8 million. DPS read the line the statement prints; which interest the
covenant uses is the agreement's definition. **Fixed 8 November 2026:** an interest line printed net
("Interest expense, net", "Net finance costs") now holds the statement with `INTEREST_IS_NET`, and the reviewer
enters the gross figure or keeps the net one. When they keep it, **Review done** remembers that wording for the
borrower, so its next statement is not held for it again. Rerun of the 10 statements
(`results/run-net-interest-statements.json`, `results/scores-net-interest.txt`): **0 wrong published** (was 1),
47 right published, 7 of 10 held (was 6; HU01 now waits, HU03 was already held).

### Other findings

- **Fixed 9 November 2026 (BUG-50):** the four agreements sent to the heavy pool were a third blank pages
  (printed web filings); blank pages no longer count, so all 20 stay in the standard pool (HA02 40 s, HA05 93 s,
  HA06 62 s, HA10 4 s). Rerun with bug hunt 3 (`results/scores-bug-hunt-3.txt`, 19 scored): 45 of 45 covenants
  reach a person, 0 missed, 0 wrong without a flag, 28 wrong proposals a person rejects. Before the fix:
- **The heavy pool is too slow for long agreements on this machine.** A 52-page agreement took 13 minutes there,
  and a 316-page one took 18 minutes. Two agreements (595 and 721 pages) were stopped by the operating system for lack of memory
  (7.6 GB shared by the whole stack). A person sees such a job as failed, so it is not silent. But production
  needs more memory for the heavy workers, or a page limit before the heavy pool.
- **Few agreements publish on their own.** All 18 were held, mostly for US-dollar amounts (`AMOUNT_NOT_IN_INR`)
  and possible covenants. That is safe, but the review load is high.

## With the AI readers on (8 November 2026)

The same 28 scored documents again, with `DPS_AGREEMENT_AI_CHECK` and `DPS_STATEMENT_AI_CHECK` on (Azure OpenAI,
one model call per document). The comparison is against the rules alone with all fixes so far.
`results/scores-ai-readers-on.txt`, `results/run-ai-readers-on.json`.

**Agreements (44 covenants)**

| | Rules alone | Rules + AI |
|---|---|---|
| Covenants offered clean (no flag) | 2 | **8** |
| Flagged for a check | 14 | 13 |
| Shown as a possible covenant or nearby term | 28 | 23 |
| Missed | 0 | 0 |
| Wrong term with no flag | 0 | 0 |
| Agreements that published on their own | 0 of 18 | 1 of 18 (HA15, both covenants right) |

What the AI reader did:
- It agreed with the rules on 11 terms, and all 11 are right.
- It did not confirm 25 rule terms: 21 of them were wrong, 4 were right. "Not confirmed by the AI" is therefore a
  strong hint that a term is wrong.
- It questioned 6 terms with another number, and all 6 were wrong.
- It added 7 covenants the rules did not have: 6 right, 1 wrong. The wrong one is HA07's "Priority Indebtedness to
  exceed 15% of Consolidated Total Assets" filed as debt to EBITDA, and it is flagged `AI_ONLY`.

**Statements (133 figures)**

| | Rules alone | Rules + AI |
|---|---|---|
| Figures published wrong (silent) | 1 of 61 | **0 of 0** |
| Statements held for review | 6 of 10 | **10 of 10** |

What the second reader did:
- It confirmed 52 figures: 51 right, 1 wrong. The wrong one was Reliance SED's cash, which the rules had read from
  "Cash ... as at the beginning of the year"; that rule is now fixed too.
- It questioned 19 figures: 3 were really wrong (Alliance Finance's profit and cash, read 10^7 too small).
  16 were right:
  - 12 are Kotagala, where the rules read the group column and the AI the company column. Both are right numbers;
    which one the covenant tests is the bank's choice.
  - Most of the rest are interest (net or gross) and equity (with or without non-controlling interests): the same
    definition question as Hershey's interest above.
- It suggested 7 lines the rules did not find, all 7 right.

**What this means for the decision to turn the AI readers on.**
- *Agreements:* a clear gain. More covenants come out right and clean, nothing is missed, nothing wrong passes
  without a flag, and "not confirmed by the AI" points at the wrong terms.
- *Statements:* safer. No figure goes out wrong unseen. But every statement now waits for a person, mostly because
  the two readers pick different columns or definitions. Telling the second reader which column (group or company)
  and which definitions the bank uses would remove most of that review load. That is the next improvement before
  this setting is worth its review cost.
- *Cost:* one model call per document, more for long documents. Token usage was not recorded in this run; the AI
  usage page (`/dps/v1/ai-usage`) shows it in normal use.

## Caveat

"After the fixes" re-uses the same 30 documents the fixes came from, so it shows that the fixes work, not how DPS
does on new documents. The next month's fresh sample measures that. The size is small: 61 published figures cannot
show "1 in 200". The monthly sample should grow until the published figures across months reach several hundred.

Files: `results/scores-before-fixes.txt`, `results/scores-after-fixes.txt`, and the raw runs `results/run-*.json`.
The documents themselves are not committed (third-party copyright); `sources.tsv` lists where each came from.

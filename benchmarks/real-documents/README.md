


## 17. Wording stress tests and bug hunt (8 November 2026)

Generated wordings instead of real documents: 11,919 covenant wordings, 72 non-financial ones, 1,080 statement
labels and 136 look-alikes, each run through DPS's readers. Before: 1,575 covenant wordings, 62 non-financial
wordings and 194 labels missed, and 16 labels read as another line. After: none missed and none wrong without a
flag. On this benchmark, one more right covenant and no figure changed. Details in `../wording-stress/README.md`.

## 16. Phase 5: held-out measurement (7 November 2026)

30 documents DPS had never seen (20 EDGAR agreements, 6 US 10-Ks, 2 Indian and 2 Sri Lankan statements), run
through the real flows with production settings. Full report: `../held-out/README.md`; how to repeat it monthly:
`../held-out/HOW-TO-RUN.md`.

- Agreements (18 read, 43 covenants): a person saw 37 on the first run, 6 were missed and 1 wrong term had no
  flag. After the fixes: 40 seen, 3 missed, no silent wrong term. All 18 agreements are held for review.
- Statements (10, 133 figures): 5 of the 60 published figures were wrong on the first run (a cash-flow line read as
  debt, "cash generated from operations" as the operating cash flow, a segment note's current liabilities, an
  associates' share as net income). After the fixes: 1 of 61, a net-versus-gross interest definition.
- 8 November: thresholds given by defined words ("'LTV Maximum Ratio' means 73.0%") and ratios in words
  ("one-to-one") are read and always flagged (`THRESHOLD_FROM_DEFINITION`). All 43 held-out covenants now reach a
  person; on this benchmark one more covenant is found (AG31) and none is lost.
- 8 November, AI readers on: agreements give 8 clean right covenants instead of 2, with none missed and no wrong term
  without a flag (the AI's 11 confirmations were all right; 21 of the 25 terms it did not confirm were wrong).
  Statements publish nothing wrong unseen, but all 10 wait for review, mostly for group-against-company columns and
  net-against-gross definitions. Details in `../held-out/README.md`.
- Eight general fixes, each a test in `dps/tests/test_held_out.py`. The "after" numbers re-use the documents the
  fixes came from; next month's fresh sample is the real check. 61 published figures cannot yet show the target of
  at most 1 wrong in 200.

## 15. Phase 4: the learning loop (7 November 2026)

Phases 1 to 3 make DPS say what it did not understand. Phase 4 makes people's answers count for the next document.

- **The weekly picture** (`GET /dps/v1/learning`, UI **Operations → Learning**): per week, documents read, possible
  covenants shown and how they were decided, covenants only the AI found, AI disagreements, lines not found,
  statements held by an accounting or unit check, and wordings remembered. "Not understood" adds up the first kind.
- **Suggestions from decisions**: a statement wording reviewers confirmed (borrower memory, phase 3) that the catalog
  does not read that way, ranked by how many borrowers confirmed it; a possible covenant a reviewer approved; and per
  covenant rule, how often its proposals are kept (approved / (approved + rejected)).
- **Learned wordings** (`dps_learned_wording`, migration `f8b2d5e0a3c7`): an administrator promotes a suggestion. The
  worker loads the active ones before each document, so "Borrowings repayable after one year" becomes long-term debt
  for every borrower, or "Debt Yield" a DSCR-type covenant. A wording the catalog already reads as something else is
  refused (409 `CONFLICT`), so learning only adds and never overrules. Retiring stops it on the next job. Both are
  audited.
- **Test cases** (`GET /dps/v1/learning/test-cases`): confirmed wordings, approved and rejected covenants with their
  quotes, and the learned wordings, as JSON for the golden set.

The benchmark cannot measure this yet: it has one period per company and no reviewer decisions. What the tests prove
(`dps/tests/test_learning.py`): the counts, the suggestions (a known wording is not suggested), promote, the
conflict refusal, that a promoted wording changes how a new statement or agreement is read and a retired one stops,
and the export. The golden gate is unchanged (value accuracy 0.9924). Phase 5 measures on new documents DPS has never
seen.

## 14. Phase 3: meaning-based matching and borrower memory (7 November 2026)

### 14.1 Matching a line by meaning (`extraction/similarity.py`)

A new statement may word a line in a way the catalog has never seen: "Borrowings - non-current", "Net cash inflow
from trading activities", "Stock-in-trade", "Sundry debtors". The matcher rewrites both the line and every catalog
wording into one financial vocabulary (debt, loans and indebtedness are borrowings; net income and net earnings
are net profit; debtors are receivables; turnover is revenue; non-current is long-term ...), then scores the words
they share (70%) and their spelling (30%, character trigrams, which forgive OCR slips and "amortisation" /
"amortization"). It runs inside DPS with no model and no new library.

**On 931 real line labels** the catalog maps by exact wording (benchmark statements), the matcher alone, without the
exact wording, named the same line for **72.2%**, another line for **4.4%**, and was unsure (score under 0.6) for
23.4%. That is good enough to **suggest**, not to decide: a match fills nothing on its own. It now ranks the
"Possibly ..." hints for lines not found (section 12.3), which stay a hint (on the benchmark 2 right, 3 wrong:
most missing values are not on the pages read at all; when the AI second reader is on, its grounded suggestions,
10 of 10 right, take precedence).

### 14.2 Borrower memory (`dps_label_memory`, migration `e7a1c4d9f2b6`)

When a reviewer takes a suggestion with **Use this** and saves, DPS remembers that this borrower's statements print
that wording for that line (stored in its meaning-normalised form, per CP, with who confirmed it, audited as
`LABEL_REMEMBERED`). The next statement for the same CP maps that wording on its own, even with small changes
("Borrowings (non-current)"), and the spread says "Read as you confirmed for this borrower before". Memory
never crosses borrowers. If the catalog later reads the same wording as a different line, the statement waits for a
person (`LABEL_MEMORY_CONFLICT`), so a wrong confirmation cannot spread silently.

The benchmark has one period per company, so memory cannot be measured on it; `dps/tests/test_borrower_memory.py`
covers learning, reuse next period, isolation between borrowers, and the conflict check. Phase 4 (the learning
loop) will turn confirmations that repeat across borrowers into catalog wordings for everyone.

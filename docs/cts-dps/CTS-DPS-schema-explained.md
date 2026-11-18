# The DPS result schema, explained simply

File: `contracts/schemas/result.json`, version `dps.result/1.0`.
**One result format for every document type**, so Spring and the UI write their code once.
DPS fills it with **rules first and the Document Agent** (a CrewAI agent on the bank's Azure GPT). Every value
quotes its page; a value neither can read with certainty is left empty with the reason and sent to a person.

## 1. The idea in one line

For every document, DPS returns: **what the document is**, **who it belongs to**, **the values it found**, and
**for each value: where it was found, how sure we are, and whether a person must check it**.

## 2. The result, top level

Think of it as one envelope with 7 parts:

| Part | Plain meaning | Why it is there | Who uses it |
| --- | --- | --- | --- |
| `schema`, `jobId`, `jobType`, `status` | Which job this is and whether it finished (DONE, NEEDS_REVIEW, FAILED) | Spring links the result to its upload | Spring |
| `classification` | What kind of document this is | Catch a wrong file before it is saved | DMS, UI |
| `identifiers` | Borrower, facility and period details found in the document | Catch a file uploaded against the wrong borrower | DMS, UI |
| `fields` | **The values DPS extracted** (the main part) | What the bank actually needs | Spring, UI |
| `document` | The overall decision: auto-accepted or needs review, and why | One quick answer for the screen | Spring, UI |
| `warnings` | Problems in the whole document (for example, totals do not add up) | The reviewer sees them first | UI |
| `pipeline` | Which version of DPS and which skill pack produced this | Audit: we can always explain how a value was produced | Audit, support |

## 3. `classification`: what the document is

| Key | Meaning | Example |
| --- | --- | --- |
| `type` | What DPS thinks the document is | LOAN_AGREEMENT |
| `expected` | What the user said it was | FINANCIAL_STATEMENT |
| `mismatch` | true when they differ and DPS is sure (≥ 0.8) | true, so the user is warned |
| `basis` | For statements: STANDALONE or CONSOLIDATED | CONSOLIDATED |
| `periodType` | ANNUAL, HALF_YEARLY, QUARTERLY, MONTHLY | QUARTERLY |
| `method` | How it was decided: RULES, RULES+AI_CONFIRMED (the agent agreed), MODEL, VISION (read from page images: a person confirms) | RULES+AI_CONFIRMED |
| `aiSecondOpinion` | Only when the agent read a different type than the rules; the job goes to a person | SANCTION_LETTER |
| `confidence` | How sure, from 0 to 1 | 0.97 |
| `evidence` | 2–3 lines from the document that prove it | "TERM LOAN AGREEMENT", page 1 |

Types: SANCTION_LETTER, LOAN_AGREEMENT, AMENDMENT, FINANCIAL_STATEMENT, COMPLIANCE_CERTIFICATE, SECURITY_DOCUMENT,
OTHER_EVIDENCE, UNKNOWN.

## 4. `identifiers`: who and when

| Key | Meaning | Example |
| --- | --- | --- |
| `kind` | What was found | PAN, GSTIN, CIN, LEI, UDIN, LOAN_ACCOUNT, FACILITY_ID, BORROWER_NAME, PERIOD_END, CURRENCY, UNIT, SIGNATURE |
| `value` | The value, already checked by its validator (for example, the GSTIN check digit) | AAPFU0939F |
| `source` | Page and line where it was found | page 1, "PAN AAPFU0939F" |
| `match.status` | How it matches the bank's records (from the Spring lookup) | see below |
| `match.candidateIds` | The bank record ids it points to | ["B-17"] |

| Match status | Plain meaning |
| --- | --- |
| EXACT | The account or facility number exists, for this borrower |
| STRONG | The PAN or CIN matches one borrower |
| FUZZY | The name looks the same (≥ 90% similar): a suggestion only |
| CONFLICT | It points to a **different** borrower than the screen: warn the user |
| NONE | Not found in bank records |

## 5. `fields`: the values (main part)

Every extracted value is one field, and **every field has the same 9 keys**:

| Key | Plain meaning | Example | Why it matters |
| --- | --- | --- | --- |
| `id` | Unique id of this value | f-7 | The reviewer confirms or corrects by this id |
| `group` | Kind of value (decides the shape of `value`) | FINANCIAL_COVENANT | Spring knows where to store it |
| `name` | What the value is | DSCR | Shown on the screen |
| `value` | The value itself, typed by its group | `{"operator": ">=", "threshold": 1.2}` | The data the bank uses |
| `source` | **Where** it is in the document: page, exact quote, box on the page, or Excel cell | page 27, "not less than 1.20 times" | **Proof**: the reviewer sees it highlighted, and nothing is invented |
| `method` | **Who** found it | RULES (the rule that read it), MANUAL (a reviewer) | Shows whether a person set the value |
| `confidence` | **How sure**, from 0 to 1, with the top reasons | 0.71: LOW_OCR_QUALITY | Decides auto-accept or review |
| `issues` | Problems found for this value | FORMULA_INVALID | Tells the reviewer what to check |
| `status` | Where it is in the review | see below | The workflow |

**Status of a field:**

| Status | Plain meaning | Set by |
| --- | --- | --- |
| AUTO_ACCEPTED | Safe and sure; no review needed | DPS |
| NEEDS_REVIEW | A person must check it | DPS |
| CONFIRMED | The reviewer said it is right | Reviewer |
| CORRECTED | The reviewer changed the value (method becomes MANUAL) | Reviewer |
| REJECTED | Not a real value (for example, not a covenant) | Reviewer |

**`value` per group (the shape changes with the group):**

| Group | Used for | `value` holds |
| --- | --- | --- |
| FINANCIAL_COVENANT, NON_FINANCIAL_COVENANT, INFO_COVENANT, POSSIBLE_COVENANT | Covenants in agreements and sanction letters | metric, operator (>= > <= < =), threshold, unit, expression (in the bank's formula format), schedule (step-downs by date), cure, change (for amendments: ADD / CHANGE / DELETE) |
| STATEMENT_LINE | Figures from financial statements | amount, unit, currency, asOfDate, periodMonths, derived |
| CERTIFIED_FIGURE | Figures from compliance certificates | covenant, figure, unit, declaredCompliant, signatory, designation, signedOn, udin |
| AGREEMENT_TERM | Sanction terms | key (SANCTIONED_AMOUNT, TENOR_MONTHS, INTEREST_RATE, FACILITY_TYPE, SECURITY, VALIDITY), text, amount, currency, date |
| SCHEDULE_TERM | Dates and deadlines | kind (TESTING_CALENDAR, REPORTING_DEADLINE, REPAYMENT, INTEREST_RESET, CURE_WINDOW), rule, frequency, daysAfterPeriodEnd, dates, amount |

## 6. One example, read line by line

```json
{
  "schema": "dps.result/1.0",
  "jobId": "job-123",
  "jobType": "EXTRACT_AGREEMENT",
  "status": "NEEDS_REVIEW",
  "classification": {"type": "LOAN_AGREEMENT", "mismatch": false, "confidence": 0.97},
  "identifiers": [
    {"kind": "PAN", "value": "AAPFU0939F",
     "source": {"page": 1, "quote": "PAN AAPFU0939F"},
     "match": {"status": "STRONG", "candidateIds": ["B-17"]}}
  ],
  "fields": [
    {"id": "f-7", "group": "FINANCIAL_COVENANT", "name": "DSCR",
     "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES",
               "expression": "DSCR >= 1.2"},
     "source": {"page": 27, "quote": "shall maintain a DSCR of not less than 1.20 times",
                "bbox": [72, 410, 523, 468]},
     "method": "RULES",
     "confidence": 0.71,
     "issues": [{"code": "LOW_OCR_QUALITY", "message": "page 27 is a scan read at 72% OCR confidence"}],
     "status": "NEEDS_REVIEW"}
  ],
  "document": {"route": "NEEDS_REVIEW", "reasons": ["1 field(s) need review"]}
}
```

| Line | How to explain it |
| --- | --- |
| `status: NEEDS_REVIEW` | DPS finished, but a person must check something |
| `classification` | It is a loan agreement, as the user said (no mismatch); 97% sure |
| `identifiers` | PAN found on page 1; it matches one borrower in the bank (STRONG), record B-17 |
| `group, name` | A financial covenant: DSCR |
| `value` | DSCR must be ≥ 1.2 times; `expression` is the same rule in the bank's formula format, already checked by Spring |
| `source` | Found on page 27, with the exact sentence and its box, so the screen can highlight it |
| `method` | Found by the rules (no AI) |
| `confidence: 0.71` + `issues` | But page 27 is a scan read with low OCR quality, so the score dropped to 0.71 |
| `status: NEEDS_REVIEW` | So a person decides; DSCR is high-risk and is always reviewed anyway |
| `document.route` | One field needs review |

## 7. Other schemas (short)

| Schema | Plain meaning |
| --- | --- |
| `job-request.json` | What Spring sends: job type, file reference (version, key, SHA-256) and context (CP, borrower, expected type) |
| `job-status.json` | The state of a job: QUEUED, RUNNING, RETRY_WAIT, DONE, NEEDS_REVIEW, FAILED, DEAD, CANCELLED |
| `event.json` | The message DPS sends when something happens (job accepted, analysis completed, extraction completed, failed) |
| `review.json` | What the review screen shows: fields to check, each reader's value, the reasons, the page image |
| `review-decisions.json` | What the reviewer sends back: CONFIRM, CORRECT, REJECT, ADD |
| `page-model.json` | DPS internal: the text, positions and tables read from the file; saved once and reused |
| `errors.yaml` | Every error code, with whether to retry |

## 8. Rules that never change

| Rule | Why |
| --- | --- |
| Every value has a `source` (page + quote, or cell) | Nothing is invented; everything can be checked |
| A risky value is never auto-accepted in the first release | A person decides what matters |
| Results are never overwritten; a review creates a new version | Full audit trail |
| The schema has a version (`dps.result/1.0`) | Old consumers keep working when it grows |

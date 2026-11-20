# Proposal: reading a document set (agreement, amendments, waivers) together

*For the Spring (CTS) team. Status: **proposal**, nothing built until the open questions below are agreed. 19 Nov 2026.*

> **Update 20 Nov 2026: a first version is built** (full review WP9). It differs from this proposal in two ways, both open for the Spring team to change:
> - the set is sent as a JSON manifest *document* (`mediaType: application/json`, like `DRAFT_COMMENTARY`): `{setId, asOfDate, documents: [{versionId, role, effectiveDate, jobId}], baseCovenants}`, not as `context.documentSet`;
> - it returns the covenants **in force on `asOfDate`** (each with its change trail and waivers), not the full timeline with effective-from and effective-to per value. A timeline is a number of in-force calls, one per date, until the timeline itself is agreed.
> Waivers have their own job, `EXTRACT_WAIVER` (it needs `baseCovenants`).

## 1. The problem

CTS tracks the covenant **in force on a date**. That depends on a chain of documents, not on one file:

| Date | Document | Effect on the DSCR covenant |
|---|---|---|
| Jan 2024 | Loan agreement | DSCR ≥ 1.25x, tested quarterly |
| Mar 2025 | Amendment 1 | DSCR ≥ 1.10x from Q2 2025 |
| Sep 2025 | Waiver | Q3 2025 test waived; all other rights reserved |
| Jan 2026 | Amendment 2 | DSCR back to 1.20x from Q1 2026 |

Today DPS reads **one document per job**:
- `EXTRACT_AGREEMENT` reads the agreement.
- `EXTRACT_AMENDMENT` compares one amendment with the approved covenants Spring sends (`context.baseCovenants`).

Nobody computes the combined timeline, and a waiver is not modelled at all. Spring would have to merge the readings itself, which is error-prone and leaves no evidence trail.

## 2. What we propose

A new job type **`RECONCILE_SET`** on the existing API, `POST /dps/v1/jobs`, with the same auth, idempotency, queue, events and result endpoint. No new API.

- **Spring sends a pinned set:**
  - the members (document version ids, already extracted by their own jobs);
  - the order and role of each member;
  - the approved baseline from the CTS DB.
- **DPS returns a covenant timeline:**
  - for each covenant, every value it has had, with effective-from, effective-to and the clause it came from;
  - every conflict or gap, flagged for review.
- **Spring stays the authority:**
  - DPS proposes the timeline;
  - a person approves it in CTS;
  - CTS stores the effective-dated covenant versions and recalculates schedules.

DPS never overwrites an approved covenant.

### Request (new fields under `context`)

```json
{
  "jobType": "RECONCILE_SET",
  "document": { "versionId": "<the newest member, the one that triggered the reconciliation>", "...": "as today" },
  "context": {
    "tenantId": "default", "cpIds": ["CP-1"], "facilityIds": ["FAC-101"], "requestedBy": "rm-anil",
    "documentSet": {
      "setId": "SET-CP-1-FAC-101", "revision": 3,
      "members": [
        { "versionId": "v-agr-2024", "role": "AGREEMENT", "executedOn": "2024-01-15", "resultJobId": "job-..." },
        { "versionId": "v-amd-2025", "role": "AMENDMENT", "executedOn": "2025-03-10", "resultJobId": "job-..." },
        { "versionId": "v-wvr-2025", "role": "WAIVER",    "executedOn": "2025-09-02", "resultJobId": null },
        { "versionId": "v-amd-2026", "role": "AMENDMENT", "executedOn": "2026-01-20", "resultJobId": "job-..." }
      ]
    },
    "baseCovenants": [ { "appliedCovenantId": "AC-1", "metric": "DSCR", "operator": ">=", "threshold": 1.25,
                         "frequency": "QUARTERLY", "effectiveFrom": "2024-01-15" } ]
  }
}
```

**Rules for the request:**
- Every member names its DMS version.
- `resultJobId` is the member's completed DPS job, or `null`, in which case DPS reads that member as part of this job.
- `role` is one of `AGREEMENT`, `AMENDMENT`, `WAIVER`, `SIDE_LETTER`, `SCHEDULE`.
- A set revision is immutable. A new member gives a new revision, and so a new job.
- The idempotency key covers the set revision and the baseline, so a retry returns the same job.

### Result (additions to `dps.result/1.0`)

```json
"timeline": [
  { "appliedCovenantId": "AC-1", "code": "FIN_DSCR",
    "versions": [
      { "operator": ">=", "threshold": 1.25, "from": "2024-01-15", "to": "2025-04-01",
        "source": { "versionId": "v-agr-2024", "page": 34, "quote": "..." } },
      { "operator": ">=", "threshold": 1.10, "from": "2025-04-01", "to": "2026-01-01",
        "source": { "versionId": "v-amd-2025", "page": 2, "quote": "..." } },
      { "operator": ">=", "threshold": 1.20, "from": "2026-01-01", "to": null,
        "source": { "versionId": "v-amd-2026", "page": 1, "quote": "..." } }
    ],
    "waivers": [ { "periods": ["2025-Q3"], "conditions": "all other rights reserved",
                   "source": { "versionId": "v-wvr-2025", "page": 1, "quote": "..." } } ] }
],
"conflicts": [ { "code": "PRECEDENCE_UNCLEAR", "appliedCovenantId": "AC-1", "members": ["v-amd-2025", "v-amd-2026"],
                 "message": "two amendments set DSCR for Q1 2026" } ]
```

Every value keeps the page and quote it came from, as today, plus the member version id.

## 3. Behaviour

1. **Order by legal effect, not by upload time.** The effective date comes from the document text ("with effect from…"), with the execution date as a fallback. An unclear or contradictory date → `PRECEDENCE_UNCLEAR` → review.
2. **A waiver never deletes a covenant.** It marks the named test periods waived and keeps the failed test on record. A waiver with no stated end or period → `WAIVER_SCOPE_UNCLEAR` → review.
3. **Changes match by `appliedCovenantId` and clause.** They never match by metric name alone; two DSCR covenants for two facilities stay separate. This is what `EXTRACT_AMENDMENT` already does. An unclear target → `AMBIGUOUS_AMENDMENT_TARGET` → review.
4. **Missing or unreadable members are named, never skipped.** A member that cannot be read → `MEMBER_MISSING` or `MEMBER_UNREADABLE`. The timeline is still returned and held for review.
5. **A draft or unsigned member is shown but never applied.** `DRAFT_NOT_APPLIED` if the text says draft, proposed or subject to execution.
6. **Nothing is final in DPS.** The whole result goes to review, and CTS decides.

## 4. What this reuses

- **Job API, queue, events and review:** unchanged.
- **Member extraction:** the existing extraction jobs, through their stored results.
- **Matching:** amendment matching by clause and id (`skills/amendment_diff/rules.py`).
- **Effective dates and schedules:** the schedule-terms skill.

The new code is the timeline builder, the waiver reader, and the precedence and conflict checks.

## 5. Questions for the Spring team

1. Can CTS hold a **set** (its members and their roles) per CP and facility, and send it with the approved baseline?
2. Where should the trigger come from: Spring on each new member upload, or a "reconcile" button?
3. Can CTS store **effective-dated covenant versions** (from and to), and waivers per test period? Or does it keep only the current value today?
4. Is the CP the same as the facility for sets? A side letter may apply to one facility only.
5. Who approves a reconciled timeline, and does approval recalculate the future schedules automatically?

## 6. Effort once agreed

- **DPS:** about 1–2 weeks for the timeline builder, waiver reader, precedence and conflicts, schema, tests, and a golden set of 3–5 real document chains.
- **Spring:** the set store, the effective-dated versions and the review screen.

The golden chains must come from the bank: an agreement with its real amendments and waivers.

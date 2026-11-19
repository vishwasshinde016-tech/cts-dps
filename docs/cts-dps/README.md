# CTS Document Processing Service (DPS)

The DPS is the separate Python AI service that reads covenant documents for the bank's covenant tracking system
(CTS) with rules first and the architect's **Document Agent** (one CrewAI agent per job with the job's skill, tools
and prompt, on the bank's Azure OpenAI GPT). Only selected pages go to the model, only to the bank's endpoint; a
person decides what the rules and the agent do not agree on. The DMS (upload, storage, versions) is a different
component inside the bank's Spring app.

| File | What it is |
| --- | --- |
| [install-and-demo.md](install-and-demo.md) | **Start here on a new laptop:** install, check, one-command demo, run the service, what to read |
| [demo-walkthrough.md](demo-walkthrough.md) | **Read this while you demo:** upload real documents and follow each one through every step: what to type, what DPS does inside, where the file and data are now, how to show it, what to say (onboarding, amendment, assessment) |
| [presenter-guide.md](presenter-guide.md) | **Read while explaining the code:** boards to code, step-by-step flow with file:line, agent, skills, tools, Java wrap, DB, IDs, schemas, logs, demo, code excerpts |
| [diagrams/dps-complete.html](diagrams/dps-complete.html) | **The whole DPS on one page (open in a browser):** 4 diagrams with steps, every table, file:line references and real code excerpts |
| [sample-run-AGR-001.txt](sample-run-AGR-001.txt) | A run log of the demo: every DPS function entered, with file and line, plus the log lines |
| [CTS-DPS-overview-for-teams.md](CTS-DPS-overview-for-teams.md) | Short overview for the other teams |
| [CTS-DPS-schema-explained.md](CTS-DPS-schema-explained.md) | The result schema in simple words |
| [end-to-end-explained.md](end-to-end-explained.md) | Upload to covenant result in one diagram: libraries, flows, agent, skills, tools, Java rules, with code paths |
| [dps-review-2026-11-19.md](dps-review-2026-11-19.md) | Review against every requirement source and the Codex DPS: inventory, comparison, measurements, changes, open gaps |
| [document-set-proposal.md](document-set-proposal.md) | Proposal for the Spring team: reading an agreement with its amendments and waivers as one set (RECONCILE_SET, covenant timeline) |
| [design.md](design.md) | End-to-end design, v0.6 (17 Nov 2026), including the scaling decision and the Java tools boundary |
| [../../contracts/dps-jobs-api.yaml](../../contracts/dps-jobs-api.yaml) | The job API the Spring app calls |
| [../../contracts/spring-tools.yaml](../../contracts/spring-tools.yaml) | The Java business-rule tools the DPS calls in Spring |
| [../../contracts/schemas/](../../contracts/schemas/) | Result, page model, review and compatibility JSON schemas |
| [runbook.md](runbook.md) | Alerts and first actions, sizing, deploy and rollback, backup and DR, chaos expectations |
| [go-live.md](go-live.md) | Rollout modes (shadow, assisted, normal), go-live gate, golden set, checklist |
| [model-inventory.md](model-inventory.md) | Model routes and controls, for the model risk team |

## What is built (17 Nov 2026)

| Phase | In code | Waits on the bank |
| --- | --- | --- |
| 1 Contract, job API, queue | Job API, batches, events, queue (priorities, aging, limits, retries, DEAD, cancel) | — |
| 2 Ingest and page model | DMS store reader with hash check, inspection rules, sandboxed parser (child process or parser container), page models for PDF/XLSX/XLS/CSV/DOCX, segmentation, page images | OCR and layout bake-off on the bank's scans |
| 3 ANALYZE | Classification by rules with the agent's second opinion (vision for image-only files), identifiers and validators, Spring lookup matching, key pages, cycle checks | Azure deployment names and key |
| 4 Skill packs | Rule-based generic flow; sanction, certificate, amendment, schedule terms, commentary packs; statement identities; Java formula validation and covenant library | Labelled sanction letters, certificates, amendments for per-pack targets |
| 5 Confidence and review | Rule-table and logistic scorers, isotonic calibration, routing, 3% QA sample, review / reclassify APIs, learning queue, AI status contract | 200 labelled fields per type to switch on the logistic scorer |
| 6 Operations | Hardened compose with parser sidecars, leader lock, audit hash-chain seals, scaling signals, alert rules, runbook, load script, chaos test | Load and soak runs on the target VM, threat model review, DR test, image signing in the bank pipeline |
| 7 Pilot and go-live | Rollout modes per type, go-live readiness report, model inventory, checklist | Golden set labelling, pilot weeks, sign-offs |

Phase plans (1, 1B, 2–7) and the DMS design are kept as PDFs alongside the design's local copy; the DMS is built by the
DMS team from its own tab.

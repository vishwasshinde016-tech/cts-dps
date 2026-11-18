# CTS DPS pilot and go-live

## Rollout modes (per job type, by configuration)

`DPS_ROLLOUT_DEFAULT` and `DPS_ROLLOUT_MODES='{"EXTRACT_SANCTION": "SHADOW", ...}'`; each job records the mode it
ran under (`dps_job.rollout`), so a pilot report always knows what the analyst saw.

| Mode | What happens | Event to Spring |
| --- | --- | --- |
| SHADOW | DPS reads every real upload; results are stored, not shown; analysts work as today | `dps.extraction.shadow` |
| ASSISTED | DPS pre-fills the review screen; every field goes to review | `dps.extraction.needs_review` |
| NORMAL | Routing by thresholds; high-risk fields still reviewed; 3% QA sample | `dps.extraction.completed` / `needs_review` |

Rolling a type back to shadow is a configuration change and a restart; no deploy.

## Go-live gate per document type

`GET /dps/v1/ops/go-live` measures, per job type: grounding rate of stored values (target 100%), sampled error rate
of auto-accepted fields (<= 1%), p95 time (native <= 5 min, scan <= 15 min). Classification accuracy (>= 98%) and
calibration error (<= 0.05) come from the golden-set runs and are added to the report by the evaluation job. A type
goes live only when every check is PASS; the others stay in SHADOW.

## Golden set (bank documents never leave the bank)

220 documents to start (40 agreements, 30 sanction letters, 20 amendments with their base, 60 statements, 30
certificates, 10 security documents, 30 hard cases) plus a 20% held-out set never used for tuning. Two analysts
label each document in the review screen; a senior analyst settles disagreements; accepted corrections from the
learning loop (`/dps/v1/ops/golden-candidates`) grow the set.

## Go-live checklist

- [ ] Targets met for the types going live, signed by the business owner
- [ ] Model inventory approved by model risk (`model-inventory.md`: Document Agent routes on Azure GPT, OCR, layout, scorer)
- [ ] Bank approval to send selected pages to its Azure OpenAI deployments; zero data retention confirmed
- [ ] `DPS_AI_ALLOWED_HOSTS`, routes file and Azure key from the vault set; one live run on the golden set
- [ ] Security review closed; DR test done (`runbook.md`)
- [ ] Runbook and on-call rota in place; alerts wired (`deploy/alerts/cts-dps-alerts.yaml`)
- [ ] Rollback tested (switch a type back to SHADOW by configuration)
- [ ] Users trained on the review screen

## After go-live

Two weeks of hypercare (daily: DEAD jobs, overrides, timings), then the monthly model review. Any rule, pack, OCR or
scorer change, and any prompt, skill or model route change: golden-set gate, then a 2-week shadow run.

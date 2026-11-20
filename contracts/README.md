# API contracts

| File | What it is |
| --- | --- |
| `dps-jobs-api.yaml` | **CTS job API** (Phase 1 of the CTS DPS design): jobs, batches, event feed, catalogue, ops, plus the 2 bank-app compatibility endpoints |
| `schemas/*.json` | JSON Schemas of the CTS job API: `job-request`, `job-status`, `result`, `page-model`, `event` |
| `catalogues/errors.yaml` | Every error code the CTS job API returns or records, with its class, HTTP status and user message |
| `catalogues/events.yaml` | Every event on `GET /dps/v1/events` |

`dps/src/dps/contract/` holds a packaged copy of `schemas/` and `catalogues/` (the DPS image is built from `dps/`
and serves them at `GET /dps/v1/schemas`). Edit here, then copy; `dps/tests/test_jobapi.py` fails when they differ.

Field names are camelCase, like the bank app. Errors use the bank app's envelope `{code, message, details, timestamp}`.

## Lint

```bash
npx @redocly/cli@1 lint contracts/dps-jobs-api.yaml contracts/spring-tools.yaml --config contracts/redocly.yaml
```

## Mock server for the Spring team

Serves made-up responses that follow `dps-jobs-api.yaml`, so the Spring app can be built before DPS is ready:

```bash
npx @stoplight/prism-cli@5 mock contracts/dps-jobs-api.yaml --port 9401 --dynamic
```

Then point the Spring app's DPS base URL at `http://localhost:9401`.

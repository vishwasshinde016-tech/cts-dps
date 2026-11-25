# CTS and DPS integration guide

Reviewed on 24 November 2026. DPS revision `5686931`. CTS revision `aed92dc` **plus the local working changes** in `without DPS/cwca-service`, including the onboarding document upload and mock recommendation flow.

For the implementation sequence, proposed CTS contracts, database decisions, tests and change log, see [sections 7–12](#7-implementation-handoff-for-the-next-engineer-or-ai).

## Decision

**We can start integration in UAT. The two reviewed builds do not yet run the full flow together.** DPS has the job API. CTS has a document upload flow and two clients that match DPS compatibility endpoints. CTS still needs to submit the extraction job after upload, track the job, and show real results in place of its mock document recommendations.

Start with **one PDF agreement**, using polling and an approved shared folder. Test certificates, financial statements, and direct SharePoint access separately. Keep the pilot in `SHADOW` until the team has reviewed the results and approved the next stage.

**First path**  
CTS upload → DPS job ID → poll status → show result and evidence → record review in CTS.

| Finding | What to do |
| --- | --- |
| **CTS onboarding still reads mock results.** `RecommendationController` calls `DocumentAiMockService`. The upload already generates the document version, file key, SHA-256 and size. | **CTS** adds a Spring DPS client, submits after the upload is committed, and maps DPS fields and evidence into the CTS screen. Existing master and ML recommendations are a separate source. |
| **The current CTS user token is incompatible with DPS JWT mode.** The local CTS auth provider signs with HS256. DPS JWT mode verifies RS256. The existing compatibility WebClient calls do not set a DPS bearer header. | **CTS and Ops** configure a dedicated service credential or an approved RS256 service token. Add authentication to the CTS outbound clients and verify borrower access in CTS before any DPS call. |
| **SharePoint links are not readable as DPS file keys.** The local CTS onboarding uploader writes to the filesystem. DPS validates that the committed file exists under a mounted root and checks its SHA-256. | **CTS and Ops** make an exact copy of the accepted SharePoint version available through the DPS filesystem for UAT. If direct SharePoint reads or writes are required, scope and implement a connector, then test it at the expected volume. |
| **Certificates use a different upload path.** Monitoring keeps an external upload `fileId`. The DPS matching API expects the version ID submitted with the extraction job. | **CTS** keeps an explicit attachment-to-version mapping and uses the mapped ID. Test an original and a corrected upload. |
| **The CTS checkout has two migration files with version V13.** `V13__financial_document_upload.sql` is empty. The other defines `cov_document_upload`. The checked covenant-service configuration validates its schema. | **CTS and DBA** confirm the actual migration runner and deployed history, choose the approved migration, and verify the upload table. A Flyway runner scanning both files can reject the duplicate version. Do not change an already applied migration. |
| **CTS root build scripts still point to a missing backend directory.** Both `build.bat` and `start.bat` target `backend`, while this checkout has `covenant-service` plus separate gateway and auth modules. | **CTS** confirms the supported module build and startup commands for the UAT release. |
| **DPS generated-file recovery is incomplete.** A file write can succeed before its MySQL index row commits. Some retries skip saving when the path already exists. The document Q&A file lookup also lacks the tenant and pipeline-version filters used by the worker. | **DPS** makes file/index saves recoverable and tests failure after file creation. Leave the Q&A endpoint out of scope until its lookup uses the same tenant and version checks. |

### Follow-up review and rollout gates

| When to address it | Finding and owner | How to verify the fix |
| --- | --- | --- |
| **Before the first pilot** | **CTS identity and borrower checks.** The reviewed gateway verifies the JWT signature and issuer. The covenant service reads the user from `X-User`. The checked gateway code does not show it replacing that header with the verified identity or checking token purpose. Follow section 7.2. | A valid access token works. A refresh, expired, invalid, or missing token is refused. A browser-supplied header cannot change the acting user. Test access separately for upload, status, evidence, review, and apply. |
| **Before showing real findings** | **CTS grid and apply behavior.** The mock callback replaces the grid. A late result can overwrite manual edits or a newer upload if the same pattern is used for DPS. Bulk apply can mark unrelated suggested rows as rejected if CTS sends only the DPS selection. Follow section 7.4. | Upload two versions, let the older job finish last, and check that the current grid and manual edits stay correct. Test the final apply action with both existing CTS suggestions and DPS findings. |
| **Before using results for business decisions** | **CTS shadow mode.** DPS can return a completed shadow result. CTS must enforce comparison-only use on the server based on the recorded rollout mode. [DPS result endpoint](../../../dps/src/dps/api/jobs.py#L388) · [Stored rollout mode](../../../dps/src/dps/skills/runner.py#L686) | Authorized testers can inspect a shadow result. The regular CTS business apply flow cannot apply the findings, even when the DPS job is `DONE`. |
| **Before production** | **DPS file recovery.** The file and its MySQL index can get out of sync. Concurrent preview requests can also compete to create the same file. Follow section 8. | Simulate a database failure after the file is written, then retry. Check that two simultaneous preview requests both return the correct page. |
| **Before sharing a DPS database across tenants or enabling Q&A** | **DPS lookup scope.** Single-job replay looks up the idempotency key before validating the request tenant. Q&A selects a page model without the tenant and parser-version filters used by extraction. Follow section 8. | A tenant mismatch returns `403 TENANT_MISMATCH` before the replay lookup. A valid retry still returns the original job ID. Q&A selects the correct tenant and parser version. Keep Q&A out of the first pilot. |
| **Before enabling bulk import** | **DPS interrupted batches.** DPS can return an existing batch after saving only some items. Completion checks recorded items without comparing their count with the expected count. Follow section 8. | Interrupt a batch after some items are saved, then replay it. Every expected item is recorded once, and the completion event fires only once after all expected items are accounted for. |

## 1. Set up access and storage

1. **DBA** runs the DB Team handoff scripts in order against the approved CTS **test MySQL** database `cov_monitoring`: read-only inventory, DPS table creation, and verification. Use a separate DPS runtime account with access to the DPS tables. Keep CTS business tables under CTS ownership. The handoff create script matches the [version in the DPS checkout](../../../dps/migrations/02-create-dps-in-cov-monitoring.sql). Do not apply a second schema installer to the same database.
2. **Ops** deploys the DPS API, worker pools, and their parser sidecars using the approved release images. Configure `DPS_DATABASE_URL`, `DPS_CORE_BASE_URL`, model routes, and secrets. Use the configuration in the [CTS Compose file](../../../deploy/docker-compose.cts-dps.yml). Verify the API and worker workloads can reach MySQL and read the same committed source files.
3. **CTS and Ops** set the host path `DMS_BLOBS` to the root of the committed file tree. If using the current CTS uploader, this is the configured storage base plus `accepted`. The DPS API and workers see the mount at `/dms/blobs`. Give them a separate writable `DMS_DERIVED` mount for page-model files and previews, and configure the audit-seal mount. Keep the mounts persistent across restarts. With SharePoint as the source, the integration must arrange the exact version's bytes in the mounted source tree before submitting. A SharePoint item ID or URL alone cannot supply the bytes.
4. **CTS and Ops** agree on HTTPS, the DPS base URL, and credentials in both directions. For the initial backend-to-backend connection, use `SERVICE_TOKEN` mode if approved by Ops. Spring sends `Authorization: Bearer <service-token>`. DPS needs `DPS_SERVICE_TOKEN_SHA256` and `DPS_SERVICE_ALLOWED_NETWORKS`. Verify what source address DPS sees behind the trusted proxy. If choosing `JWT` mode, use an approved RS256 issuer, correct audience and key configuration, and a DPS role allowed for the endpoint. Use a separate credential accepted by the CTS gateway for DPS-to-CTS calls. Setting `DPS_CORE_SERVICE_TOKEN` sends that bearer value, but does not change the CTS gateway's authentication policy. Keep all service credentials in the backend.
5. **CTS** checks borrower access before submission, polling, reading results or evidence, and sending review decisions. Store and verify a mapping from the CTS tenant, credit proposal, and document version to the returned DPS job ID. The DPS service identity is privileged. Send `X-User` from the authenticated CTS session for attribution where needed. It does not grant permission. Match the request `context.tenantId` to the DPS deployment tenant.

After the database and mounts are ready, run from the DPS repository root on the approved test VM with the secure deployment settings already loaded.

```bash
docker compose -f deploy/docker-compose.cts-dps.yml config -q
docker compose -f deploy/docker-compose.cts-dps.yml up -d
```

Check `GET /health/live`, `GET /health/ready`, and authenticated `GET /dps/v1/capabilities`. API readiness alone only makes MySQL connectivity and the source directory part of the HTTP readiness decision. Also check worker status, parser health, derived-folder writes, and a real queued job. [Authentication](../../../dps/src/dps/api/auth.py) · [Readiness implementation](../../../dps/src/dps/api/jobs.py#L698)

## 2. Connect one CTS document upload

1. The user uploads through CTS `POST /api/v1/credit-proposals/{cpId}/document-upload`. CTS commits the PDF and keeps the receipt. Submit to DPS **after** that commit. Add a durable pending-submit record or equivalent retry mechanism in CTS so a network failure does not lose the request.
2. Spring sends the JSON below to `POST /dps/v1/jobs`. Use the values from the upload receipt. `versionId` comes from `documentVersionId`, `sizeBytes` from `fileSizeBytes`, and `objectKey` is the relative file path under the mounted root. Use a supported MIME type such as `application/pdf`. An upload receipt with a generic `application/octet-stream` needs a verified type before submitting. Set `requestedBy` from the caller identity.
3. Save the returned `jobId` against that receipt. A new job returns `202`. A replay can return `200` with `duplicate: true`. Reuse the same idempotency key and body when retrying a request with an uncertain outcome. A key reused with a different body returns `409 IDEMPOTENCY_KEY_REUSED`.

```http
POST /dps/v1/jobs
Authorization: Bearer <DPS-service-credential>
Content-Type: application/json
Idempotency-Key: cts-onboard-example-v1
X-Correlation-Id: cts-demo-request-001
```

```json
{
  "jobType": "ONBOARD",
  "document": {
    "versionId": "00000000-0000-4000-8000-000000000001",
    "objectKey": "CP-100/00000000-0000-4000-8000-000000000001/agreement.pdf",
    "sha256": "<replace with the 64-character lowercase SHA-256>",
    "fileName": "agreement.pdf",
    "mediaType": "application/pdf",
    "sizeBytes": 50000
  },
  "context": {
    "tenantId": "default",
    "cpIds": ["CP-100"],
    "requestedBy": "cts-reviewer"
  },
  "priority": "INTERACTIVE"
}
```

This example uses placeholders. Replace the digest, size, tenant, identity, and IDs with the real receipt and deployment values. The same file key must resolve to readable bytes for both API and workers. [Job handler and validation](../../../dps/src/dps/api/jobs.py#L196)

## 3. Read, review, and apply results

1. Spring polls `GET /dps/v1/jobs/{jobId}` with backoff. Show `QUEUED`, `RUNNING`, and `RETRY_WAIT` as progress. For `DONE` or `NEEDS_REVIEW`, call `GET /dps/v1/jobs/{jobId}/result`. For `FAILED`, `DEAD`, or `CANCELLED`, stop polling and show the relevant status with the correlation ID. Respect `details.retryAfterSeconds` on `429 QUEUE_FULL` and `Retry-After` when present. A `RESULT_NOT_READY` response means to reread job status.
2. Map the result's fields, source citations, and warnings into CTS. Use `GET /dps/v1/versions/{versionId}/pages/{page}/image` for PDF evidence. Serve the preview through CTS after checking that the user can access that borrower and version.
3. On `NEEDS_REVIEW`, use `GET /dps/v1/jobs/{jobId}/review`. Send the returned **review revision** in `If-Match` on `POST .../review`. This revision is a number. The result endpoint's cache ETag has a separate purpose. For example, send `If-Match: 1` and `{"decisions":[{"fieldId":"<field-id-from-review>","action":"CONFIRM"}]}`. If the revision changed, reload it and show the latest state before resubmitting. When the reviewer deliberately finishes with unused fields, call `POST .../close` with `{"reason":"Reviewed in CTS"}` and the current revision.
4. Apply approved business changes in CTS under its existing permissions. An HTTP success or DPS `DONE` alone does not authorize a CTS business update. In `SHADOW`, use results for comparison only. In an approved assisted UAT run, verify statement publishing explicitly. Closing a statement can publish confirmed or corrected lines if the period and checks permit. Inspect `published` and `notPublishedReason` when returned.
5. Add event polling once the single-job path works. `GET /dps/v1/events?after=<savedCursor>&limit=100` returns `events` and `nextCursor`. Persist the cursor after handling events durably and handle repeated events safely. Event push needs a CTS receiver and can be enabled separately.

For the onboarding screen, use DPS `fields[].name` for the label and `source.page` and `source.quote` for evidence. Keep `fields[].id` for sending review decisions. Use `libraryMatch.covenantMasterId` only after CTS confirms the match. Show unmatched items as proposals for review. Validate the extracted `value.expression` in CTS before using it as a condition override. The current CTS wizard calls `/document-ai-recommendations`. Keep that route as the Spring-facing UI endpoint if useful and use the DPS job mapping behind it.

[Result schema](../../../contracts/schemas/result.json) · [Review implementation](../../../dps/src/dps/api/review.py#L278) · [Statement publication on close](../../../dps/src/dps/api/review.py#L424) · [Evidence implementation](../../../dps/src/dps/api/evidence.py#L49)

## 4. Connect the other CTS flows

| CTS flow | Connection and verification |
| --- | --- |
| **Amendment and waiver** | Submit the accepted document using `EXTRACT_AMENDMENT` or `EXTRACT_WAIVER`. Pass approved `context.baseCovenants`. An amendment can instead provide `amendsVersionId`, while a waiver requires the approved base covenants. Test against the signed-off CTS baseline. |
| **Financial statement** | Submit `EXTRACT_STATEMENT` with the known cycle and period context. Complete review and verify approved lines through `POST /api/data/values`. CTS's existing values client uses this default path. Configure the DPS URL and credential for the intended CTS REST data source. Preserve the connection settings for other sources. Check period, units, currency, and correction handling using the same CP as the extraction. |
| **Compliance certificate** | After the monitoring upload, make the accepted bytes available and submit `EXTRACT_CERTIFICATE` for its mapped version ID. Review the figures. Then pass the **same version ID** in `certificateFileIds` to `POST /api/v1/ai/covenant-status`. Point `covenant.clients.ai-suggestion.base-url` at DPS in UAT and configure the existing CTS recommender to send the DPS credential. A failed call currently falls back to no suggestions in CTS, so check the response and logs during UAT. The current CTS caller sends all certificate attachments for the proposal. Select the versions relevant to the review cycle. Treat a missing mapping as an integration error so it does not accidentally send an empty list and trigger DPS's latest-certificate fallback. DPS can return `certificateDiagnostics` when requested. |
| **Other document workflows** | Use `EXTRACT_AGREEMENT` or `EXTRACT_SANCTION` when CTS knows the document type, or `AUTO` when DPS should classify it. `ANALYZE` can read an accepted file or a separately configured staging reference. `DRAFT_COMMENTARY` and `RECONCILE_SET` take JSON manifest files and need separate CTS wiring. |
| **Support and bulk import** | Add retry, cancel, reclassification, batch errors, and reruns once the first document flow is stable. Respect the configured batch and queue limits and keep the user-facing status in CTS. |

Send `cpId`, `to`, and `keys` to the values endpoint. Include `from` for a date range. Send `cpId`, `certificateFileIds`, and `covenants` to the certificate endpoint. The current DPS handler derives suggestions from reviewed certificate figures. Do not assume ordinary covenant attachments provide suggestions through this route.

[Certificate contract and diagnostics](README.md) · [Statement request and response](../../../contracts/schemas/compat-data-values.json) · [Certificate request and response](../../../contracts/schemas/compat-covenant-status.json)

## 5. Endpoint check

Compared **all 37 operations on 35 paths** in the published DPS OpenAPI file with the Python route declarations and a running in-process FastAPI app. Every HTTP method and normalized path matched. This checks that the endpoints exist. The UAT checklist below covers live behavior between applications.

| Who calls DPS | Methods and paths |
| --- | --- |
| CTS Spring, job flow | `POST /dps/v1/jobs`, `GET /dps/v1/jobs/{jobId}`, `GET /dps/v1/jobs/{jobId}/result`, `GET/POST /dps/v1/jobs/{jobId}/review`, `POST /dps/v1/jobs/{jobId}/close` |
| CTS Spring, job controls | `POST /dps/v1/jobs/{jobId}/cancel`, `POST /dps/v1/jobs/{jobId}/retry`, `POST /dps/v1/jobs/{jobId}/reclassify` |
| CTS Spring, evidence | `GET /dps/v1/versions/{versionId}/pages/{page}/image`, `GET /dps/v1/versions/{versionId}/runs`. `POST /dps/v1/versions/{versionId}/ask` exists and is deferred until its artifact lookup is fixed. |
| CTS Spring, synchronization and discovery | `GET /dps/v1/events`, `GET /dps/v1/capabilities`, `GET /dps/v1/schemas`, `GET /dps/v1/schemas/{name}` |
| CTS Spring, existing compatibility clients | `POST /api/data/values`, `POST /api/v1/ai/covenant-status` |
| Import and support | `GET /dps/v1/jobs`, `POST /dps/v1/batches`, `GET /dps/v1/batches/{batchId}`, `GET /dps/v1/batches/{batchId}/errors.csv`, `POST /dps/v1/batches/{batchId}/rerun` |
| Infrastructure | `GET /health`, `GET /health/live`, `GET /health/ready` |
| Operations | Under `/dps/v1/ops`, GET `/queues`, `/usage`, `/ai-routes`, `/affected-results`, `/scaling`, `/audit-seals`, `/quality`, `/go-live`, `/golden-candidates`. PUT `/ai-routes/{route}`. POST `/golden-candidates/{candidateId}/{decision}`. |

Use the CTS service identity for the job flow. The global job list and retry endpoint require DPS `SUPPORT` or `ADMIN`. The AI route update requires DPS `ADMIN`. Keep these operations behind the appropriate support or admin workflow.

[DPS OpenAPI contract](../../../contracts/dps-jobs-api.yaml)

### Calls from DPS back to CTS

| Call | Status in the reviewed CTS checkout |
| --- | --- |
| `GET /api/v1/credit-proposals/{cpId}` | Present. Reads proposal data. Verify how the deployed CTS gateway and service enforce borrower access. A successful lookup alone is insufficient evidence of a permission check. |
| `GET /actuator/health` | Default configured DPS probe path. Confirm the actual CTS probe URL and status from inside the DPS deployment. |
| `POST /api/v1/dps-tools/formulas/validate` | Batch route not found. CTS has the single-item `POST /api/v1/formulas/validate`. DPS falls back to it when the batch route returns 404. Test the permitted call and a real formula against the CTS catalog. |
| `POST /api/v1/dps-tools/lookups`, `GET /api/v1/dps-tools/covenant-library` | Matching handlers were not found in the local CTS source. Add an adapter or confirm deployed equivalents before depending on automatic identifier matching or library enrichment. |
| `GET /api/v1/credit-proposals/{cpId}/currency`, `GET /api/v1/credit-proposals/{cpId}/profile`, `GET /api/v1/settings/countries`, `GET /api/v1/fx/convert` | Exact handlers were not found. Confirm where the deployed CTS services provide this data. Wire and test the routes needed for statement and currency workflows. |
| `POST /api/v1/dps-tools/events` | No receiver found in the reviewed checkout. DPS uses `DPS_EVENTS_PUSH_PATH` when configured. Start with outbound CTS polling. |

[Spring tool contract](../../../contracts/spring-tools.yaml) · [DPS Spring tools client](../../../dps/src/dps/integrations/spring_tools.py) · [DPS Spring client](../../../dps/src/dps/integrations/spring.py)

## 6. UAT sign-off

| Owner | What to demonstrate |
| --- | --- |
| DBA and Ops | DPS schema verification passes against test MySQL. API and workers see the same accepted bytes and derived folder. DPS can write a page model and serve it after a restart. |
| CTS and DPS | From the CTS UI, upload a real test PDF. Record its version ID, object key, SHA-256, job ID, and correlation ID. Show real extraction fields and open a cited page. Repeat the same submit and confirm the job ID is unchanged. |
| CTS | An authorized reviewer can open the job and act on it. A user without borrower access is denied by CTS. Another tenant's resources are denied. Stale review revisions cause a reload. |
| CTS and DPS | Run one financial statement and one certificate in an approved UAT rollout mode. Check reviewed values, corrected certificate version mapping, and a deliberately unreviewed item. Confirm shadow runs do not publish. |
| CTS and Ops | Simulate DPS being unavailable, then recover and process the pending request once. Confirm CTS can disable the DPS route and continue its existing workflow without losing uploaded source documents. |
| DPS and Ops | Resolve file/index recovery before production reliance. If Q&A is in scope, resolve tenant/version lookup before enabling it. Confirm storage ownership, retention, and monitoring with CTS. |

Use the [DPS pilot and go-live runbook](../go-live.md) before changing the rollout mode.

### Where to look in the CTS checkout

- `RecommendationController`, `DocumentUploadService`, and `DocumentAiMockService` implement the current onboarding upload, receipt, and mock-result route. The wizard calls it through `frontend/src/api/onboarding.ts`.
- `AttachmentService` and `AiSuggestionService` show which file ID monitoring passes to the certificate client.
- `WebClientDataApiClient`, `WebClientAiStatusRecommender`, and `WebClientsConfig` are the existing outbound integration points.
- `JwtProvider` and the API gateway's `JwtValidationFilter` show the current CTS token behavior. `FormulaController` implements the existing single-item validation route.

### Verification scope

The guide request was exercised against the in-process API with a temporary database and a fixture file. It returned 202, replay returned the original job ID, and status polling returned QUEUED. This verifies submission and polling. The fixture was not processed by a worker in this check. The full local DPS suite passed **1,143 tests**, with **7 skipped**. The focused integration suite passed **166 tests**, with four opt-in Azure tests skipped. The focused checks include a stand-in Spring service for onboarding. The CTS covenant service compiled and **24 targeted Maven tests passed** for REST-source configuration, the data values client, the certificate recommendation client, and the file upload/download client. These tests use fixtures or stand-in HTTP services. The CTS checkout has pre-existing, uncommitted work. Office MySQL, deployed CTS identities, live SharePoint, Azure, and the full Spring-to-DPS workflow require environment verification. No office data, applications, or migration state were changed during this review.

## 7. Implementation handoff for the next engineer or AI

Everything in sections 7–12 is proposed implementation work. Start by comparing both checkouts with the revisions at the top of this guide and inspect the local CTS changes before touching a file. Carry those changes forward. In the implementation branch, record the actual file, change, reason, and test result for each step using the change log at the end of this guide.

### 7.1 Where the integration belongs

| CTS component | Current role | Integration decision |
| --- | --- | --- |
| `frontend` | React onboarding wizard uploads documents and shows recommendations. | Keep the existing wizard, upload receipt, manual options, and final apply action. The browser calls only CTS routes. |
| `api-gateway` and `auth-service` | The auth service issues HS256 tokens. The gateway verifies the token. | Keep the login and use the verified access-token identity for authorization. Forward that identity through a trusted internal path. |
| `covenant-service` | Stores the upload receipt, runs borrower checks, retrieves recommendations, and applies covenants. | Own the DPS client, durable submission, document-to-job mapping, status endpoint, review and evidence proxy, and CTS business approvals. |
| `covenant-platform` and `shared-library` | Existing CTS build and shared dependencies. | Follow the actual module build order provided by CTS. Add integration logic to the owning service first. |
| DPS API and workers | Accept jobs, read bytes, process asynchronously, and store versions of findings. | Reuse existing APIs. DPS owns its MySQL tables, generated files and processing lifecycle. |

Treat the CTS database as the authority for uploaded documents, proposals, users, and applied covenants. A DPS field ID identifies an extraction finding. A CTS covenant master ID identifies a rule in CTS. Keep both when a reviewer confirms a match.

### 7.2 Milestone A. Trusted user identity and borrower access

1. **CTS gateway and auth.** Compare `JwtValidationFilter.java`, `JwtProvider.java`, and the deployed gateway configuration. The reviewed filter verifies the token and forwards the request. The inspected checkout does not show it setting `X-User` from verified claims. CTS `CurrentUserContext.java` reads that header, and the frontend only supplies its impersonation header in dev. Confirm the real production identity path. If absent, add it where the token is verified. At the trusted boundary, discard client-supplied identity headers and supply an identity from a verified access token. The checked refresh token uses the same signature algorithm and issuer and carries a refresh marker. Require a valid access token on document routes and check its purpose before forwarding the verified identity. Restrict direct access to the downstream service.
2. **CTS borrower guard.** Use the existing RM/WRM ownership query in `CreditProposalCacheRepository.findCpIdsOwnedBy`, also used by `MonitoringService`. The token has both a username and a `userId` claim. Test which verified identity maps to the owner IDs in the deployed proposal data. Apply the agreed role exceptions explicitly. Deny when identity or ownership cannot be established. A proposal lookup returning 200 does not establish ownership.
3. **CTS document guard.** For every request, resolve the upload from CTS using its proposal and document version. For evidence, also resolve the associated DPS job and document version from the server-side mapping. Use the same guard for upload, status, result, preview, review, and the existing apply route when used by this workflow. A browser-supplied job ID or page URL carries no permission.
4. **Spring-to-DPS.** Add a dedicated WebClient patterned after `WebClientsConfig.java`. Use a fixed internal base URL, HTTPS in the target environment, bounded timeouts, and the approved backend credential in a bearer header. Use DPS `SERVICE_TOKEN` mode in the initial design. Derive audit identity from the verified CTS user. Keep the CTS signing secret with CTS. DPS configuration stores the service token hash and the approved network range. Verify the effective caller address behind the reverse proxy. Keep a service-credential endpoint private.
5. **DPS-to-CTS.** Treat callback authentication separately. A credential accepted by DPS does not automatically work on the CTS gateway. Verify approved auth for every enabled Spring tool, and document the actual base URL and route. A bearer value configured on the DPS client must be accepted by that deployment.

**Done when** the authorized manager can see their extracted result and preview, a different manager cannot, and a user-provided identity header cannot change the decision. Exercise upload, status, preview, review, and apply permissions independently. Test with real CTS test users and known proposal ownership. Confirm `X-User` attribution in the DPS review audit.

### 7.3 Milestone B. Submit only a saved file and keep the job link

**Change in CTS.** Reuse `DocumentUploadService.java`, `DocumentUpload.java`, `DocumentUploadRepository.java`, `DocumentUploadResponse.java` and the upload method in `RecommendationController.java`. Keep the current multipart upload. The current service calculates SHA-256, builds a version ID and relative object key, moves the file into `accepted`, and saves the receipt.

1. **Storage contract.** Verify the approved storage path from inside the DPS API and worker containers. Mount CTS's committed `accepted` root read-only as `/dms/blobs`. Pass the receipt's relative `objectKey` and actual file size, supported media type, and lowercase SHA-256. Keep the object immutable for a given version. With production SharePoint, first confirm whether the upload service already exposes the bytes through an approved mount. If it does not, use an approved export of the exact immutable version or plan a storage adapter. Keep source storage and DPS's writable derived folder separate.
2. **Track one onboarding submission per version.** The supplied office dump already has `cov_document_upload` with unique `document_version_id` and `object_key`. For the first flow, add a CTS-owned migration for a nullable `dps_onboard_job_id` plus the minimal submission state needed for durable retries. Example state is `PENDING_SUBMIT`, `SUBMITTED`, or `SUBMIT_FAILED`. Keep retry timing and claim ownership only if needed by CTS's chosen scheduler. Older rows remain without a submission unless CTS deliberately selects them for backfill. Use the approved next migration version after checking the target history. The checkout's two V13 files require reconciliation by the CTS DBA. Avoid cross-service foreign keys. If several jobs per version become part of the UI, replace the single job pointer with a CTS-owned relationship keyed by proposal, version, and purpose or generation.
3. **Commit, then dispatch.** Record that submission is pending in the same CTS transaction as the accepted receipt. Call DPS only after the transaction commits. Use a durable dispatcher or a persisted pending-record scan. An in-memory after-commit event alone can be lost if CTS exits. If several CTS instances can scan pending work, claim each item using the existing supported scheduling mechanism or a transactional claim, and recover abandoned claims. The dispatcher sends `ONBOARD` using the sample in section 2. Derive or persist a stable request key for that logical submission. Store immutable request inputs needed for replay. Never regenerate the key just because the network timed out. Do not hold a CTS database transaction open during the DPS HTTP request.
4. **Handle uncertain outcomes.** Accept 202 or 200 with the same referenced job, then store the DPS job ID. If DPS accepts the job and CTS stops before saving the ID, the dispatcher repeats the same request and key. For temporary failures, schedule bounded retries. For an invalid fingerprint, unsupported type, or 409 key conflict, surface a supportable error and stop automatic retries until the input is fixed. If the user uploads a corrected PDF, CTS creates a new version and submits a new job. Do not reuse the old version ID or idempotency key.
5. **Route response.** Keep the existing upload response fields and stay compatible with callers. Return the upload receipt as soon as CTS saves it. The UI can fetch DPS processing state separately. For the first UI flow, restrict document extraction to PDF until each other format currently accepted by the CTS chooser has a passing test. CTS may still support those formats for its existing upload workflow.

**Change in DPS.** Use the existing `POST /dps/v1/jobs` contract. Keep input validation and the existing tenant guard. For a custom tenant, ensure the same configured value reaches the API and workers in the actual Compose override and is used by CTS. Use mounted file access in the first pilot. DPS stores the file reference and fingerprint and reads the bytes from the mount. It does not need a new upload endpoint for this path.

**Done when** a normal upload creates one DPS job, upload survives downtime, and the retry finds the same DPS job after a simulated failure between DPS acceptance and CTS receipt update.

### 7.4 Milestone C. Show real findings in the existing wizard

**Current code.** `frontend/src/pages/onboarding/CpOnboardingWizard.tsx` calls `onboardingApi.documentAiRecommendations(cpId)` immediately after upload. It replaces the grid with the response and treats `FINANCIAL_DOCUMENT` as an AI source when deciding which rows to preselect. A separate effect sets grid rows when the existing onboarding recommendations reload. These are the two places where late replies can overwrite manual edits or the selected document's findings.

| File or area | Proposed implementation |
| --- | --- |
| `RecommendationController.java` and a new CTS DPS integration service | Add version-scoped processing, evidence and review routes. Use proposal and version from the route to resolve the stored job ID. Keep the current mock route behavior confined to the explicitly selected demo path. For the integrated path, return real processing state and findings. Preserve current callers during rollout. |
| `frontend/src/api/onboarding.ts` and `frontend/src/types/onboarding.ts` | Add a typed processing response and separate evidence/review requests. Key requests by proposal and document version. Use CTS routes only. |
| `CpOnboardingWizard.tsx` | Keep **Financial document upload**, **Document uploaded**, **Add from library**, **Add custom covenant**, and **Confirm and apply**. Show **Processing document** while pending. Restore status after reload and stop polling when the state is terminal or the user leaves the screen. Ignore late responses for older versions. Keep manual grid changes when polling or master recommendations refresh. Show real findings as a separate set of unselected proposals, with evidence and warnings, until the person chooses what to apply. |
| `OnboardingRecommendationRow.java`, `ThresholdSource.java`, and `AppliedCovenantService.java` | The existing grid/apply contract expects a real CTS covenant master ID. Treat DPS's library match as a proposal and validate the active master in CTS before applying. Display unmatched findings for manual mapping or custom-covenant creation. Keep DPS provenance on the finding/evidence response. The current persisted threshold enum is `MASTER`, `USER`, or `ML`. Agree to a separate provenance change across backend, UI, and reporting if the business needs a new DPS threshold source. Avoid inventing a master ID or classifying a DPS finding as an ML threshold. |

**Example CTS-facing contract.** The names below are proposals for the CTS implementation. Align them with the team's route conventions before coding.

| Proposed CTS route | What it returns or performs |
| --- | --- |
| `GET /api/v1/credit-proposals/{cpId}/document-uploads/{versionId}/extraction` | The current submission/processing state, version ID, and safe error information. When ready, return document findings, warning flags, and the current review revision if needed. Report `PENDING_SUBMIT`, queued, running, and retry-wait as **Processing**. Show failed, dead, or cancelled states explicitly. |
| `GET .../document-uploads/{versionId}/extraction/pages/{page}/image` | Spring checks access, loads the corresponding DPS PDF page, and streams it to the browser. Use the saved version and a valid page number. Keep evidence URLs internal to CTS. |
| `POST .../document-uploads/{versionId}/extraction/review` | Spring checks review permission and forwards the reviewed field IDs, actions and current revision. It returns the updated state and revision. A stale revision is shown as a reload-and-review message. |
| `POST .../document-uploads/{versionId}/extraction/close` | Spring closes only after the reviewer deliberately finishes. It sends the reason and current revision. Display any warnings or publication outcome relevant to the job. |

For each finding, carry the DPS field ID, group, name, value, status, issues, and source page and quote as relevant to the screen. Retain the master match and expression as suggested values until validated. Render quotes as text. Use previous accepted CTS formula validation for any condition applied. `NEEDS_REVIEW` is a usable result with findings to inspect. It is not an extraction failure. DPS result ETag and review revision serve different purposes. Use `If-Match` with the review revision, not the result ETag.

**Review and apply are separate operations.** Save the DPS review decision, then apply the user-selected and validated business changes through CTS. CTS apply can fail after DPS saved the review, so show both outcomes clearly and allow retry of the CTS apply step without issuing a second DPS review. On uncertain review outcome, reread the revision and decisions before resubmitting. Test against the CTS uniqueness rule for a proposal and covenant master ID. The existing bulk-apply service also marks unselected suggested rows as rejected. Keep the full selection semantics of the existing wizard and test that adding DPS proposals does not reject unrelated CTS recommendations. Skip live business apply in shadow mode. Preserve the original manual and master/ML flow during the pilot.

**Done when** the existing wizard shows pending status and real evidence for the right PDF after refresh, keeps manual edits, and respects an explicit human decision before apply. If PDF A finishes after PDF B was selected, A never replaces B on the screen.

## 8. DPS changes and AI validation

| Work item | Where to change it | What to verify |
| --- | --- | --- |
| **Check the tenant before job replay** | `dps/src/dps/api/jobs.py`, `submit_job` and `_check_tenant` | Apply the same tenant-check ordering as batch submission. Check immediately after resolving settings and before any idempotency lookup. Verify that a tenant mismatch returns no job ID or duplicate status and that valid retries still work. [Single-job submission](../../../dps/src/dps/api/jobs.py#L348) |
| **Recover interrupted batch submission** | `dps/src/dps/api/jobs.py`, `submit_batch` and `_submit_item`, and `dps/src/dps/jobs.py`, `_sync_batch` | Persist enough information to resume the same batch. Use stable item identities and preserve per-item errors. Before completion, verify that the recorded count equals the expected count and the items meet the existing completion rules. Test concurrent retries and one completion event. [Batch submission](../../../dps/src/dps/api/jobs.py#L507) · [Completion check](../../../dps/src/dps/jobs.py#L382) |
| **Make artifact saves recoverable** | `dps/src/dps/ingest/store.py`, `dps/src/dps/ingest/pipeline.py`, `dps/src/dps/api/evidence.py` | Write complete content to a temporary file on the same storage, then publish atomically using a method safe on the actual filesystem with concurrent writers. Verify hash before accepting an existing artifact. Make retry repair an orphaned file or database reference using the unique artifact path. Handle conflicting writers without returning a partial file. Test missing file, missing DB row, wrong hash, and concurrent preview requests. |
| **Fix Q&A artifact selection before enabling it** | `dps/src/dps/api/evidence.py` and the reuse lookup in `ingest/pipeline.py` | Reuse a tenant- and version-aware artifact lookup with deterministic selection. Check the chosen source hash, format, and pipeline version associated with that job. Preserve current worker reuse for compatible files. Verify two tenants, two pipeline versions, and a missing artifact. Keep Q&A off the integration surface until tested. |
| **Validate extraction behavior** | DPS skill registry, rule/AI flow, staged release configuration, and existing golden/eval tooling | Hold prompts and model routes stable during the initial integration so failures can be traced to wiring or extraction. Record pipeline version and the approved model deployments. Compare representative approved documents against expected findings, values, page citations, and review warnings. Include a scanned PDF that exercises the Heavy pool. |
| **Keep diagnostics and audit available** | CTS/DPS correlation IDs, worker logs, DPS ops APIs, shared derived storage | Carry a correlation ID across CTS and DPS. Check job ID, document version, attempt, and status are enough for support to trace a failure. Avoid logging tokens or whole document contents. Keep existing AI usage budgets, review audit and daily seal checks working. Monitor oldest queued job, Heavy-worker health, storage space, and repeated artifact failures. |

**Rollout gates.** First deploy with the current `SHADOW` setup for engineering comparison. In that mode, the test team can inspect generated results through authorized test access. The DPS result endpoint can return a completed shadow result. Enforce comparison-only use in CTS Spring using the recorded rollout mode, including when DPS reports DONE. Move to an approved `ASSISTED` UAT only once source-file access, borrower checks and evidence are proven. A later phase may switch reviewed job types to broader use after the existing go-live checks. Changing rollout can require an intentional new logical submission. Do not rely on replaying an old idempotency key to create a new run.

## 9. Database and packaging decisions

- **Current deployable DPS release.** Use the current **24-table** DPS MySQL handoff for the selected release. The creation script is stamped at `f46b72aa08c1`. Confirm its target revision and installed schema using the DBA's read-only preflight and verification scripts. The two supplied CTS dumps contain 39 monitoring tables and 10 user tables. `cov_document_upload` is already present in the monitoring dump. Confirm the live UAT schema before selecting its next migration. The repository and DB Team copies of the DPS creation script have identical hashes and define 24 tables and four audit triggers.
- **Reuse CTS tables for references.** A job pointer and durable submit state belong on the existing CTS upload receipt for one onboarding job per version. Add the DPS version reference to the existing certificate attachment only if one row means one immutable certificate version. Store the full DPS result, review history, page index and business evaluation history with their respective owners. CTS's existing data-source ingestion can stage approved statement values through the DPS REST endpoint.
- **Transfer one coherent version.** Pin the DPS images and matching SQL revision. Ship this guide, API schemas, deployment configuration and DB instructions. Send an approved test fixture with expected output. Use the approved secret store for credentials and keep bank documents and data dumps out of the release package. Confirm CTS's supported module build/start procedure because the root scripts reference a missing directory in the reviewed checkout.

## 10. Follow-up workflows after the PDF pilot

1. **Statements.** Use a scoped CTS REST source with DPS credentials and `/api/data/values`. Send the document version and verified statement period and scope. After review, compare the numbers returned by DPS with those staged by CTS. Test the same period corrected twice, different reporting units, and a statement with unresolved warning(s). Verify requested and returned keys against the CTS measure catalog. Test currency/profile and FX calls that the selected flow uses. Keep a shadow run from publishing.
2. **Certificates.** Leave the existing attachment store in place. Resolve the exact uploaded bytes and create or map the immutable DPS document version. Send only relevant IDs for the assessment period to `/api/v1/ai/covenant-status`. Keep unmapped files visible as an integration issue. Send diagnostics in UAT to distinguish unavailable, awaiting review, and reviewed figures. Test both original and corrected versions and a normal attachment that should not drive a certificate result.
3. **Spring callbacks.** Check actual route and body against the Spring tools contract. Plan an adapter only for tooling required by the agreed workflow. A missing batch formula endpoint can fall back to the existing single-item route on a 404. Test that result and permission errors are distinguishable. Poll DPS events first. If push becomes necessary, add a receiver, acknowledge only after a durable write, and de-duplicate repeated events by event ID.
4. **Other job types.** Add amendments, waivers, reconciliation, and batch processing separately. Send their required accepted inputs and base references and verify each output against its own result contract. Preserve explicit reviewer control for amendment changes and waiver decisions.

## 11. Test matrix and rollout ownership

| Scenario | Expected result | Owner |
| --- | --- | --- |
| Gateway identity and owner check | Assigned manager can access their proposal. Unassigned manager is denied. A missing identity or browser-supplied identity cannot become an authorized user. | CTS |
| Exact file and key | Both DPS processes read the same accepted version. A changed fingerprint, wrong key, or unsupported type produces a visible submission failure. | CTS, DPS, Ops |
| One normal PDF | One CTS upload creates one DPS job and one initial result. CTS shows progress, then real findings. The user opens the cited page. | CTS, DPS |
| Retry after acceptance | Spring repeats the same request and finds the original DPS job. CTS restart or timeout preserves the pending upload. | CTS, DPS |
| Replace a file and reload | New version gets a distinct job. Old status or results stay attached to the old version. Refresh recovers the selected version, and a late result cannot overwrite recent manual edits. | CTS |
| Concurrent review and apply | Stale revision requires reload. A partial failure after DPS review can retry the CTS apply step. No duplicate applied covenant is created. | CTS, DPS |
| Unavailable storage or worker | CTS shows that processing is pending or failed with a correlation ID. A recovered worker can process the job. A failed artifact save is repairable. | DPS, Ops |
| Statement and certificate | The approved figures and selected certificate versions match the CTS review context. Shadow mode never updates business data. | CTS, DPS |
| Clean database setup and restart | Preflight and verification match the pinned release, API and workers use their expected accounts, and generated evidence survives restart. | DBA, Ops |
| Rollback | The CTS integration can be disabled using a proposed server-side release flag. Existing upload receipts, manual workflows, and review evidence remain available according to retention. Jobs already accepted are reconciled or allowed to drain. | CTS, DPS, Ops |

Run CTS gateway/service tests for the auth paths, frontend tests for the wizard and version changes, then DPS contract, retry, artifact and review tests. Use the actual target MySQL and shared storage for the end-to-end test. Keep the initial production enablement separate from this handoff. Document any check that could only be run with fixtures.

## 12. Implementation change log

Use this section as the shared record during implementation. When a phase is complete, add one line per changed file or deployment setting with the real revision and the evidence. This keeps proposed tasks separate from work already done.

| Status | Area | Actual file or setting | What changed | Verification and revision |
| --- | --- | --- | --- | --- |
| Done | Analysis and documentation | This integration guide | Traced the CTS UI, gateway, upload/ownership, and apply paths against DPS. Wrote the implementation sequence, target contracts, and test plan. | Static source review and the previously recorded local API and test results in section 6. |
| Done | Follow-up integration review | This integration guide | Added rollout gates and verification cases for trusted identity, version-safe UI and apply, shadow mode, artifact recovery, tenant-safe replay, and interrupted batches. | Seven focused DPS tests passed, with 43 deselected. The new regression cases and office UAT remain to be run. |
| Planned | CTS identity and borrower access | Fill in during implementation | Add or verify trusted caller identity and reuse the CTS ownership rule in DPS-facing routes. | Record positive and negative access tests. |
| Planned | CTS submission and wizard | Fill in during implementation | Persist and submit the selected upload and replace the mock recommendation flow with a real result. | Record retry, reload and evidence tests. |
| Planned | DPS artifact and Q&A | Fill in during implementation | Repair file/index recovery and scope Q&A to the correct tenant and parser version. | Record recovery, concurrency and scope tests. |
| Planned | Environment and DBA | Fill in during implementation | Provision the pinned schema, credentials, mounts and deployment. | Record UAT results, release revision and the approved rollback. |

**Current change scope.** This pass updated this Markdown handoff. CTS and DPS application code, database schema migrations, configuration files, and office data were not changed.

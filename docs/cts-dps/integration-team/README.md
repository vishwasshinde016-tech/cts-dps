# CTS integration handoff

- [DPS API reference, 23 November 2026](DPS-API-reference-2026-11-23.pdf) includes current integration notes before the original reference. All 32 DPS paths and eight Spring paths in the current [DPS OpenAPI contract](../../../contracts/dps-jobs-api.yaml) and [Spring tools contract](../../../contracts/spring-tools.yaml) appear in the reference.
- [N5 certificate version ID question](CTS-DPS%20Group%201%20N5%20-%20Certificate%20Version%20ID%20Confirmation.pdf) explains the ID flow, what CTS might need to change, and what to verify in UAT.

## Current certificate contract

`POST /api/v1/ai/covenant-status` expects each entry in `certificateFileIds` to equal the DMS version ID submitted as `document.versionId` for that certificate's extraction job. The monitoring path in the reviewed CTS checkout takes the external upload service's opaque `fileId` from the certificate attachment record. Its separate onboarding uploader creates a `documentVersionId` and object key, and onboarding recommendations still use a mock. Confirm the deployed monitoring upload-to-DPS job wiring, including the ID mapping for an original and a corrected certificate.

DPS evaluates only explicitly selected versions when given a nonempty list. An empty or omitted list uses the newest eligible certificate for the requested credit proposal. Only figures a reviewer confirmed or corrected can drive a recommendation.

The dated PDF predates the optional request flag `includeCertificateDiagnostics`. When set to `true`, DPS returns `certificateDiagnostics` alongside `recommendations`, with one entry per distinct explicitly requested ID. An empty or omitted selection returns an empty diagnostics list. Existing CTS requests retain their response shape.

| Status | Meaning |
| --- | --- |
| `REVIEWED_FIGURES_AVAILABLE` | The version has reviewed figures that can be considered. A particular covenant may still have no recommendation. |
| `AWAITING_REVIEW` | DPS identified the certificate and its extraction awaits review. |
| `NO_REVIEWED_FIGURES` | DPS found a completed certificate result without usable reviewed figures. |
| `UNAVAILABLE` | DPS has no eligible certificate result for that ID under the requested credit proposal and tenant. |

See the [request and response schema](../../../contracts/schemas/compat-covenant-status.json) for the current wire contract. The CTS statement values client already supports `POST /api/data/values` as its default values path.

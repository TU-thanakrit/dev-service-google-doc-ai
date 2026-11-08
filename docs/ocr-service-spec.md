# Shared asynchronous OCR service — v1 specification

Status: product scope and ticket breakdown confirmed by the user. The approved tickets are published as GitHub issues #1–#12 in TU-thanakrit/dev-service-google-doc-ai by TU-thanakrit. Technical design and testing proposals below require review; this is not approval to implement, provision, deploy, upload documents, or incur cloud costs.

## Problem Statement

Consumer teams need a reusable OCR service callable from n8n, with a stable interface suitable for future web applications and LLM agents. The current synchronous expense-document pipeline classifies five templates and extracts business fields, whereas the desired initial capability is recognized text without classification or structured extraction. Documents vary in page count, callers can wait asynchronously, and document contents must remain within organization-controlled infrastructure.

## Solution

Provide an authenticated asynchronous OCR service. A consumer team submits one file through the API, receives a job identifier, polls progress, and retrieves combined recognized text plus numbered per-page results and page count. A separate Cloud Run Jobs worker performs processing. GCS initially stores inputs, results, job state, ownership, and idempotency records. Managed dispatch and scheduling services are allowed, but their concrete arrangement is not selected.

Processing capacity is shared, while each team's jobs and documents remain isolated. OCR engine replacement is operator-controlled and does not change the public result contract. CPU-only evaluation precedes engine selection and performance-limit decisions.

## User Stories

1. As an n8n workflow author, I want to upload a document through HTTP, so that I can initiate OCR without a custom connector.
2. As a caller, I want a job identifier without waiting for recognition, so that long documents do not hold my submission connection open.
3. As a caller, I want to poll progress and retrieve results, so that I can continue my workflow after processing completes.
4. As a consumer team, I want to authenticate using GCP identity, so that anonymous callers cannot consume the service.
5. As a consumer team, I want my own caller identity mapped to an internal team identity, so that ownership does not depend on another team's credential.
6. As a consumer team, I want other teams denied access to my jobs and documents, even if they know a job identifier.
7. As a caller, I want to submit PDF, JPEG, PNG, or TIFF, so that common document formats are supported.
8. As a caller, I want unsupported, encrypted, malformed, empty, or over-limit documents handled explicitly, so that invalid input does not become an unexplained processing failure.
9. As a document reader, I want printed Thai mixed with English recognized, so that the service matches the initial document population.
10. As a caller, I want combined plain text, so that downstream automation can consume the whole document.
11. As a caller, I want numbered per-page text and page count, so that I can associate results with their source pages.
12. As a caller, I want successful pages preserved when another page fails, so that a local failure does not discard usable output.
13. As a caller, I want partial completion and per-page error codes made explicit, so that I cannot mistake incomplete output for a complete document.
14. As a caller, I want quality warnings distinct from execution failures, so that I do not mistake successful execution for verified transcription accuracy.
15. As an operator, I want usable embedded PDF text reused and its source recorded per page, so that OCR is not unnecessarily repeated.
16. As a workflow author, I want a repeated submission with the same team-scoped idempotency key and content to return the existing job during its lifetime, so that network retries do not create duplicate work.
17. As a workflow author, I want reuse of that key with different content rejected, so that the wrong document is not associated with a previous request.
18. As a caller, I want bounded recovery from transient failures, so that temporary infrastructure problems do not immediately discard my job.
19. As an operator, I want completed pages checkpointed where possible, so that recovery can avoid unnecessary repeated recognition.
20. As an operator, I want a maximum job lifetime, so that queued or stalled work cannot persist indefinitely.
21. As a consumer team, I want successful inputs deleted promptly and retained data expired on a defined schedule, so that document exposure is limited.
22. As an operator, I want expired data inaccessible immediately and cleanup monitored, so that a delayed cleanup task does not extend API access.
23. As an operator, I want content-free operational diagnostics, so that I can investigate reliability without logging documents or OCR text.
24. As an operator, I want to replace the engine without changing caller workflows, so that engine selection is not a permanent public contract.
25. As an operator, I want engine/model identity and worker revision recorded and frozen per job, so that retries do not mix incompatible page results.
26. As an operator, I want engine replacements evaluated on the same Thai/English corpus and result contract, so that changes have evidence rather than assumed equivalence.
27. As a future maintainer, I want Microsoft Entra and Cloud SQL explicitly deferred, so that v1 remains bounded without hiding those future directions.

## Implementation Decisions

### Confirmed architecture and scope

- Replace expense-processing behavior with OCR-only behavior. Classification, Gemini calls, and structured business-field extraction are not part of the new processing path.
- Use a Cloud Run HTTP API and separate Cloud Run Jobs OCR workers. Do not perform durable OCR work in an untracked background task inside the API process.
- The user restored development/testing to project `thaiunion-ai-platform`, account `thanakrit.boonquarmdee@thaiunion.com`, region `asia-southeast1`. New resource names use literal prefix `dev-ocr-service`. Set account, project, and region explicitly; never rely on ambient CLI defaults. Read-only permission checks show Cloud Run service/job create and update access, but not a complete build, image-upload, bucket-create, service-account attachment, or private-IAM path. Verify exact resource-level permissions and obtain any missing administrator actions before provisioning.
- Use GCS for application data and job metadata initially; no database is selected for v1. Cloud SQL is a possible future metadata backend.
- Permit managed dispatch/scheduling services. Dispatch acceptance is not evidence of worker start or OCR success. Exact services and recovery protocol remain open.
- Keep inputs within organization-controlled processing/storage. Use GCP identity and Cloud Run IAM initially; Microsoft Entra integration is deferred.
- Map verified caller identities to stable internal team identities. Derive ownership from authentication, not caller-supplied team identifiers. Enforce ownership on every document/job operation.
- Shared capacity does not grant shared document access. No per-team quotas are required initially; global concurrency/backlog limits still need a design.

### Processing and results

- One uploaded file per submission, sent through the API and stored in GCS. Direct-to-GCS upload, existing-object submission, and URL downloads are not required for v1.
- Initial formats: PDF, JPEG, PNG, TIFF. Exclude Office files, archives, and password-protected PDFs. File-content validation and resource bounds must be designed before accepting untrusted documents.
- Initial recognition scope: printed Thai and English, not handwriting.
- Return combined plain text and numbered per-page results, page count, explicit completeness/status, and per-page text source. Preserve failed pages in the page-result accounting rather than silently dropping them.
- Reuse embedded text only when defined usability checks pass; otherwise apply OCR. Neither an embedded text layer nor OCR output is automatically authoritative.
- Preserve available successful pages when processing is partial. Separate execution errors from warnings such as no text or suspected poor quality; do not invent confidence scores.
- Use one approved engine at a time behind an application-owned recognition interface. No caller engine selection or automatic cross-engine fallback.
- Freeze engine/model identity, worker revision, and processing configuration per job and its retries. Changes apply to new jobs only. How old pinned configurations remain executable across deployments is a technical design prerequisite.

### Reliability and lifecycle

- Team-scoped idempotency applies while a job is pending/running and until its final-state retention expires. Same key and content returns the existing job; different content conflicts. Different keys allow intentional separate jobs. After record expiry, deduplication is no longer guaranteed.
- Retry only defined transient failures, with bounded attempts; do not endlessly retry invalid/unreadable input or silently rerun successful jobs. Retry counts, backoff, error categories, leases, and recovery ownership remain open.
- Maximum total lifetime: configurable initial ceiling of 24 hours from acceptance, including queueing and retries. At the deadline, stop additional processing and finalize with a deadline reason: partial success if usable page results exist, otherwise failure. Prevent stale workers from overwriting that final result.
- Delete inputs immediately after full success. Expire remaining inputs, results, job metadata, and idempotency records one day after the final state. This period starts after completion, not at upload.
- Deny API access at expiry independently of cleanup. Target cleanup within one hour after expiry and alert on overdue cleanup. Physical-erasure behavior depends on deliberate storage recovery/versioning configuration and verification; no exact-second erasure guarantee is claimed.
- Do not log document contents or OCR text. Operational metadata must be allowlisted and errors sanitized. Separate operational-log retention and diagnostic naming policy require specification.

### Technical proposals requiring review

- Keep persistence behind a small interface to contain GCS concurrency/recovery logic and permit a future backend replacement.
- Specify the GCS conditional-write protocol for claims, idempotency, state transitions, checkpoint publication, and cleanup. Do not assume a collection of object writes is one transaction.
- Define the public submission/status/result contract and exact routes, status codes, error vocabulary, page ordering, whitespace behavior, and versioning before implementation. This document freezes behavior, not an unreviewed JSON schema.
- Update existing project instructions during the approved implementation so they no longer require the superseded synchronous/no-workflow design or five-template response contract.

## Testing Decisions

These are proposed testing interfaces, pending user confirmation, not tests already implemented.

- Prefer the public submission/status/result lifecycle as the primary test interface. Exercise team isolation, idempotency, progress, partial success, retry exhaustion, and expiration through observable outcomes rather than internal call counts.
- Reuse the existing injected-dependency and FastAPI test-client approach. Use a fake recognition engine, controllable dispatch, fake clock, and deterministic persistence behavior for offline tests; no live cloud calls in unit tests.
- Add focused persistence contract tests for simultaneous idempotent submissions, claim races, stale-worker fencing, crashes between writes, duplicate dispatch, restart recovery, and cleanup races. A fake-only test does not establish real GCS concurrency behavior; add separately approved cloud integration tests.
- Test exact result structure and types, one accounted-for result per source page, combined-text ordering, failed-page visibility, and absence of structured expense fields.
- Test input rejection, team access denial, safe errors/logging, deadline transitions, access cutoff, cleanup failures, and retention clocks using injected time rather than wall-clock sleeps.
- Benchmark candidate engines on representative clean/poor scans, native PDFs, mixed Thai/English, and longer documents. Label expected text on selected pages and report accuracy separately from schema/integration success. Select metrics, normalization, acceptance thresholds, and corpus before comparing candidates.
- Begin with CPU-only evaluation. Record engine/model configuration, source identities, memory, duration, and page outcomes. Select engine, upload/page bounds, worker resources, and processing limits from evidence.
- Separately verify private deployment IAM, the actual n8n identity-token path, submission/poll/retrieval behavior, duplicate delivery, recovery, expiry enforcement, and monitored deletion within an explicitly approved cloud test budget.
- Existing passing tests describe the old implementation. They are baseline evidence, not evidence that this asynchronous design works.

## Out of Scope

- Expense classification and structured business-field extraction.
- Handwriting, table reconstruction, bounding-box output, and Markdown layout reconstruction.
- Callbacks, batch submission, URL fetches, and direct-to-GCS uploads.
- Caller-selected engines, automatic multi-engine fallback, and GPU-first deployment.
- Per-team capacity quotas or fairness guarantees.
- A web UI, custom n8n node, MCP server, or LLM-agent framework integration.
- Microsoft Entra integration, Cloud SQL migration, and indefinite document/audit retention.
- Deployment, paid processing, issue publication, Git commits, or pushes without their separate authorization.

## Further Notes

### Release-blocking technical work

1. Choose the initial engine from an approved corpus and CPU benchmark; establish accuracy acceptance criteria.
2. Specify dispatch, bounded global capacity, GCS state/claim/idempotency protocols, worker revision pinning, and recovery/cancellation behavior.
3. Verify the n8n runtime identity/token path and define verified caller-to-team mapping.
4. Freeze exact result/error contracts, embedded-text checks, retry policy, file/page limits, and overload behavior.
5. Use project `thaiunion-ai-platform`, account `thanakrit.boonquarmdee@thaiunion.com`, region `asia-southeast1`, and new-resource prefix `dev-ocr-service`. Agree resource/spend boundaries and verify resource-level build, image, service-account, IAM, bucket, and runtime permissions before provisioning.
6. Define cleanup alerting, soft-delete/versioning/recovery policy, operational-log retention, and acceptance evidence.

The confirmed scope is not a claim that the architecture is fully resolved. Technical proposals and test interfaces require review before implementation. The user selected GitHub repository TU-thanakrit/dev-service-google-doc-ai and account TU-thanakrit for ticket publication. The 12 approved tickets were published with the ready-for-agent label, acceptance criteria, explicit blocking references, and 16 native blocking relationships; their authors, bodies, labels, open states, and dependency sets were read back and verified. Issues #1 and #2 have no blockers. The standalone specification itself has not been committed or pushed.

Ticket list: https://github.com/TU-thanakrit/dev-service-google-doc-ai/issues?q=is%3Aissue%20label%3Aready-for-agent

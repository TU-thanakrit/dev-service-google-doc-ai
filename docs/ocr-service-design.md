# OCR service design interview

Historical planning record: the decisions and deployment references below reflect the original interview, not the current implementation or deployment. For current requirements and deployment state, see `ocr-service-spec.md`, `ocr-v1-contracts.md`, and `../deploy/ocr-v1/sandbox.toml`.

Status: v1 product scope and ticket breakdown confirmed by the user. Consolidated specification: `ocr-service-spec.md`. The approved tickets were published as issues #1–#12 in TU-thanakrit/dev-service-google-doc-ai by TU-thanakrit. Technical design/test proposals still require review; implementation, cloud changes, and paid tests are not authorized. Application code remains unchanged.

## Confirmed direction

- Replace the expense-processing behavior with an OCR-only service. Structured business-field extraction is deferred.
- Serve multiple teams within the same organization through a shared service.
- Integrate with n8n first; web applications and LLM agents are intended future consumers.
- Return combined recognized text, per-page recognized text, and page count.
- Use GCP identity authentication with Cloud Run IAM initially; each consumer team has a separate caller identity. Map verified caller identities to internal team IDs. Exact n8n token acquisition and identity-mapping mechanisms remain to be verified and designed. Team API keys are not selected for v1.
- Microsoft Entra corporate authentication is the intended future direction, not part of v1. Its integration mechanism is undecided; no IAM or public-access changes are authorized by this design discussion.
- Deploy processing on Cloud Run and store input files in GCS. The user switched development/testing to personal project `proud-quasar-351116`, account `pb.danny@gmail.com`; region `asia-southeast1`; new-resource naming prefix `dev-ocr-service`. Only public/synthetic test documents are in scope in the personal project absent further approval; do not assume it satisfies corporate data-control requirements. The original corporate project `thaiunion-ai-platform` is a separate future target. Exact names, permissions, and workload-test budget remain to be verified/agreed.
- Do not log document contents or OCR text. Operational metadata logging is accepted. Temporary storage is permitted: delete inputs immediately after full success; expire remaining inputs, results, job metadata, and idempotency records one day after the job enters a final state (succeeded, partially succeeded, or failed). GCS soft-delete/versioning configuration and deletion enforcement remain to be specified; this is not yet a verified physical-erasure guarantee.
- Initial language/content scope: printed Thai mixed with English. Handwriting is not part of the initial scope.
- Page counts vary, and users can wait for processing. No maximum page count, file size, acceptable latency, or concurrency target is agreed yet.
- OCR engine is undecided. User raised an open-source candidate referred to as "peddle-ocr"; PaddleOCR was the assistant's interpretation, not an explicitly confirmed engine choice.
- Use asynchronous processing rather than keeping the upload request open until OCR finishes. Return a job ID; callers poll job status and retrieve the result. Callbacks are deferred. Run OCR workers in Cloud Run Jobs, separate from the HTTP API; job metadata is stored in GCS initially. Dispatch and recovery mechanisms remain undecided.
- Allow operator-controlled replacement of the OCR engine behind an application-owned interface without changing the public job/result contract. Deploy one approved engine at a time; callers cannot select engines per request in v1. No specific engine is selected.
- Normalize engine outputs to the application-owned page/result contract. Record engine/model identity and worker revision with each job, and freeze that configuration for its retries/checkpoints. Engine replacements affect new jobs only and must pass the same Thai/English evaluation and contract tests. Do not mix incompatible page checkpoints or introduce automatic engine fallback in v1.
- Representative-document evaluation is approved in principle. No cloud execution, document upload, or spend budget has been approved.
- Isolate jobs and documents by consumer team. Derive team identity from authenticated credentials and enforce ownership on submission, status, retrieval, and deletion. A job ID alone grants no access.
- Initial uploads go through the API, which writes them to GCS. Direct-to-GCS uploads are deferred; submission of existing GCS objects has not been explicitly agreed.
- Accept PDF, JPEG, PNG, and TIFF initially. Exclude Office documents, archives, password-protected PDFs, and URL downloads.
- Return readable plain text, with page-numbered text and combined text in page order. Table reconstruction, bounding boxes, and Markdown formatting are outside initial scope; exact reading order/whitespace behavior needs evaluation.
- Preserve successful pages when another page fails and mark the job partially succeeded. Include explicit per-page status and error codes; never silently omit failed pages or label partial output complete.
- For native PDFs, use embedded text when basic usability checks pass; otherwise run OCR. Record the text source per page. Usability criteria remain to be designed and tested.
- Evaluate CPU-only processing first. Consider GPU processing only if benchmark evidence justifies it. Set explicit page/file limits and processing deadlines using measured results.
- Support team-scoped idempotency keys while the job is pending/running and until one day after its final state: repeated submission of the same content with the same key returns the existing job; different content with that key is a conflict. Different keys permit intentional separate jobs. After expiry the records are removed, so the old key no longer guarantees deduplication.
- Automatically retry transient infrastructure failures within bounded limits, not invalid/unreadable documents. Preserve completed pages where possible; exhausted attempts become a final failed state. Do not silently reprocess successful jobs. Retry count and retryable error categories remain to be specified.
- Processing success means execution completed, not verified text accuracy. Return page warnings for no text or suspected poor quality separately from execution errors. Do not invent confidence scores.
- Retain partially successful inputs and results for up to 24 hours after the final state, like failed inputs/results, then delete document contents.
- Retain non-content job metadata for one day after the final state, not the proposed seven days.
- Enforce a configurable initial maximum total job lifetime of 24 hours from acceptance, including queueing and retries. At the deadline, stop further processing and finalize as partially succeeded when usable page results exist, otherwise failed, with a deadline reason. The one-day final-state retention clock then begins. This is a ceiling, not an expected processing duration. Worker cancellation/fencing and deadline recovery require implementation verification.
- Share processing capacity across teams initially; do not introduce per-team processing allocations or backlog quotas yet. Team-level data isolation and separate credentials still apply. Global backlog/concurrency limits and overload handling remain to be specified.
- Keep job state, ownership, and idempotency metadata in GCS initially, alongside separate input/result objects. Do not add a database for v1. Cloud SQL is a possible future metadata backend, not part of current scope.
- Enforce API access expiry immediately at the agreed expiry time, independently of physical cleanup. Target physical cleanup within one hour after expiry and alert on overdue cleanup. This operational target is accepted but not yet exercised or verified; GCS soft-delete/versioning and recovery settings must be consistent with it. No exact-second physical-erasure guarantee is agreed.
- Additional managed GCP dispatch/scheduling services are permitted for work delivery, recovery, and cleanup. Application data and job metadata remain in GCS; this does not select a particular orchestration service or authorize provisioning.

## Open decisions

- Storage deletion implementation, soft-delete/versioning/recovery settings, and verified cleanup/deadline enforcement.
- Request limits, latency target, and overload behavior.
- Cloud Run Jobs dispatch/recovery design, compute budget, exact prefixed resource names/IAM, and n8n identity-token acquisition/verified caller mapping.
- Initial engine selection and representative-document quality evaluation.
- Embedded-text usability criteria, warning detection, retry counts/categories, and expired-result HTTP behavior.
- Request/response contract, versioning, operational metadata policy, and acceptance tests.

## Implementation candidates, not approved decisions

- Strengthen request-size enforcement, file-content validation, and provider/engine failure handling.
- Isolate metadata persistence behind a small interface so a future Cloud SQL implementation does not change the public API. For GCS, design conditional writes and crash recovery explicitly; multi-object updates must not be assumed transactional. This concurrency design is not yet approved or exercised.
- Separate verified caller authentication from internal team authorization so future Entra integration can change identity verification/mapping without rewriting job ownership checks or changing stored team IDs. Do not build the Entra integration in v1.
- Cloud Run Jobs is selected for OCR execution; dispatch acknowledgment must not be treated as OCR completion. Duplicate execution, global capacity, and reconciliation still need an explicit design. Task timeout reference for implementation verification: https://docs.cloud.google.com/run/docs/configuring/task-timeout .

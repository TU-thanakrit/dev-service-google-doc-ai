# dev-ocr-service deployment evidence

Authoritative manifest: `deploy/ocr-v1/sandbox.toml`

Recorded: `2026-11-09T20:45:50+07:00`

## Scope

- Project: `thaiunion-shared-sandbox`
- Region: `asia-southeast1`
- Source branch: `integration/ocr-service-v1`
- Source commit: `0b4a8f8ac536bb51e7e608ade733b9d895ca1b14`
- Staged build-context SHA-256: `722e6d7619954f4231ce0baa8472313051611a6305abe535e8e0eff37203f50c`
- Runtime/Scheduler identity: `1022222817897-compute@developer.gserviceaccount.com`
- n8n caller identity: `tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com`
- Durable bucket: `gs://dev-ocr-service-data-asse1-1022222817897`

## Local readiness

- At deployed source commit `0b4a8f8`: 423 tests total — 415 passed, 0 failed/errors, 8 environment-gated skipped.
- Ruff: clean.
- Lockfile: current, 59 packages.
- Local Podman health: HTTP 200.
- Local SIGTERM: clean exit 0 in about 0.5 seconds.

## Build

- Artifact Registry: `asia-southeast1-docker.pkg.dev/thaiunion-shared-sandbox/dev-ocr-service`
- Cloud Build ID: `c58a9cdd-d909-4a94-bd47-c1ff85df31a7`
- Build status: `SUCCESS`
- Immutable image: `asia-southeast1-docker.pkg.dev/thaiunion-shared-sandbox/dev-ocr-service/ocr-service@sha256:fbe7c282d72e040199cdde770a1dd219be289b1b01e1e39d8dcf55059a03917e`

## Cloud Run

- Private API: `dev-ocr-service-api`
- Client URL/audience: `https://dev-ocr-service-api-1022222817897.asia-southeast1.run.app`
- Ready revision: `dev-ocr-service-api-00003-ghw`
- Traffic: 100% latest revision.
- Scaling: min 0, max 1; concurrency 10; CPU 1; memory 512 MiB; timeout 300 seconds; startup CPU boost disabled.
- Runtime SA: `1022222817897-compute@developer.gserviceaccount.com`.
- Unauthenticated `GET /health`: HTTP 403.
- Authenticated human `GET /health`: HTTP 200, `{"status":"ok"}`.
- Invokers: the n8n runner SA and `thanakrit.boonquarmdee@thaiunion.com`; no `allUsers` binding.

Jobs, all Ready with the immutable image and zero task retries:

- `dev-ocr-service-worker-fbe7c282d72e`: command `python -m app.ocr_worker`, timeout 1800 seconds, memory 1 GiB. Live v2 template passed the application's `WorkerPin` comparison.
- `dev-ocr-service-reconcile`: command `python -m app.ocr_reconcile`, timeout 1800 seconds, memory 512 MiB.
- `dev-ocr-service-cleanup`: command `python -m app.ocr_cleanup`, timeout 1800 seconds, memory 512 MiB.

## Scheduler

- `dev-ocr-service-reconcile-schedule`: enabled, every 5 minutes, Asia/Bangkok.
- `dev-ocr-service-cleanup-schedule`: enabled, every 15 minutes, Asia/Bangkok.
- Scheduler OAuth identity: `1022222817897-compute@developer.gserviceaccount.com`.
- First reconcile execution: `dev-ocr-service-reconcile-tgtft`, `EXECUTION_SUCCEEDED`.
- First cleanup execution: `dev-ocr-service-cleanup-tq989`, `EXECUTION_SUCCEEDED`.

## Remaining acceptance gaps

- No document/OCR request was sent; paid/live OCR budget was not authorized.
- The n8n workflow was not imported or executed, and its metadata-server ID-token acquisition/refresh path is not verified.
- The worker's real Tesseract/Poppler/ImageMagick accuracy tests remain environment-gated.
- Real duplicate delivery, deadline, partial-result, expiry and cleanup behavior with document content has not been acceptance-tested.
- Artifact Registry vulnerability scanning is disabled because the Container Scanning API is not enabled.
- The shared default Compute Engine service account has broad `roles/editor` and `roles/storage.admin`; this is a development deployment, not production least privilege.

# service_google_doc_ai

Asynchronous OCR-only service (OCR v1). A caller uploads one document over HTTP,
receives a job id, polls progress, and retrieves combined text plus numbered
per-page results. There is no classification and no structured business-field
extraction: the recognition engine is replaceable behind the service's own result
contract (`docs/ocr-v1-contracts.md`).

Roles: a private Cloud Run **service** (`app/ocr_api.py`) and separate Cloud Run
**Jobs** for the worker, reconciliation, and cleanup (`app/ocr_worker.py`,
`app/ocr_reconcile.py`, `app/ocr_cleanup.py`). Durable state, inputs, results, and
job metadata live in GCS. Callers authenticate with Google identity; anonymous
access is denied.

**Status: not deployed, and not claimed to be.** Nothing here is verified cloud
behaviour — the service, jobs, bucket IAM, and the n8n token path remain
unverified evidence (`docs/ocr-v1-contracts.md` B1–B4, `docs/ocr-v1-cutover.md`).
Offline tests and the fake-mode demos below are all that has been executed.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/ocr/jobs` | Submit one document; `202` with a status object (`200` on an idempotent retry) |
| `GET` | `/v1/ocr/jobs/{job_id}` | Status: state, timestamps, dispatch, progress |
| `GET` | `/v1/ocr/jobs/{job_id}/result` | `200` final result, `202` while not final, `410` at expiry |
| `GET` | `/health` | Liveness; no cloud client, no identity check |

Submission is `multipart/form-data` with one `file` part (PDF, JPEG, PNG, or TIFF)
and a required `Idempotency-Key` header. Errors use a closed code vocabulary with
fixed, allowlisted messages; no path, bucket, job, or token appears in a client
error. The full contract — statuses, result shape, page rules, error codes,
ownership and idempotency — is `docs/ocr-v1-contracts.md` §2.

```bash
TOKEN=$(gcloud auth print-identity-token \
  --impersonate-service-account="$RUNTIME_SA" \
  --audiences="$SERVICE_URL")

# submit
curl -sS -X POST "$SERVICE_URL/v1/ocr/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'file=@document.pdf;type=application/pdf'

# poll, then retrieve the result
curl -sS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/v1/ocr/jobs/$JOB_ID"
curl -sS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/v1/ocr/jobs/$JOB_ID/result"
```

The token must be a Google-signed ID token whose `aud` is exactly
`OCR_API_AUDIENCE` (the service URL) and whose identity holds
`roles/run.invoker`; the application re-verifies it in-process, and an unmapped
identity is `403 team_not_authorized`. `X-Serverless-Authorization` is not read:
Cloud Run strips its signature, so it cannot be verified. See
`docs/ocr-v1-n8n-workflow.md` for the same flow from n8n.

## Local development

```bash
uv sync --python 3.12
uv run pytest
uv run ruff check .
```

Fake-mode demos run the whole lifecycle in one process with injected fakes, spend
no quota, and fail if a cloud client module is imported:

```bash
uv run python -m scripts.ocr_tracer_demo   # submit -> dispatch -> poll -> result, recovery, expiry, cleanup
uv run python -m scripts.ocr_worker_demo   # dispatch -> worker -> reconcile recovery paths
```

`.env.example` holds the `OCR_*` settings every role reads (`app/ocr_config.py`,
contract §8). Copy it to `.env` for local work; `.env` is git-ignored.

## GCP target

| Fact | Value | Status |
|---|---|---|
| Account (development) | `thanakrit.boonquarmdee@thaiunion.com` | — |
| Service project | `thaiunion-ai-platform` | target |
| Region | `asia-southeast1` | target |
| New-resource prefix | `dev-ocr-service` | target |
| Durable bucket | `gs://dev-ocr-service-data-asse1-1022222817897` | exists; settings read back (soft-delete retention `0`, uniform bucket-level access) |
| Bucket project | `thaiunion-shared-sandbox` | exists |
| Selected runtime service account | `tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com` | exists; **no bucket object access granted** |

The bucket is in its own project and the service is not: `OCR_BUCKET_PROJECT`
carries the bucket's project into every role's store, while dispatch still
addresses Cloud Run jobs in `OCR_PROJECT`.

## Deploy

The container runs `uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}` (the
`CMD` in `Dockerfile`), so the OCR API is what a deployed service starts; the
worker, reconcile, and cleanup jobs override that command (`python -m
app.ocr_worker`, `python -m app.ocr_reconcile`, `python -m app.ocr_cleanup`).
`docs/ocr-v1-dispatch.md` lists the roles, their Cloud Run kind, and their
environment.

```bash
gcloud run deploy dev-ocr-service-api \
  --project thaiunion-ai-platform \
  --region asia-southeast1 \
  --source . \
  --command uvicorn \
  --args app.ocr_api:app,--host,0.0.0.0,--port,8080 \
  --service-account tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --max-instances 4 --memory 512Mi --cpu 1 \
  --set-env-vars '^@^OCR_PROJECT=thaiunion-ai-platform@OCR_REGION=asia-southeast1@OCR_BUCKET=dev-ocr-service-data-asse1-1022222817897@OCR_BUCKET_PROJECT=thaiunion-shared-sandbox@OCR_API_AUDIENCE=https://dev-ocr-service-api-REPLACE.a.run.app@OCR_TEAMS={"tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com":"team-dev"}@OCR_WORKER_REVISION_DIGEST=sha256:REPLACE@OCR_TASK_TIMEOUT_S=1800@OCR_TASK_MAX_RETRIES=0'
```

`^@^` is the environment-variable delimiter, chosen because `OCR_TEAMS` and the
pin values contain commas. Instance sizing (`--max-instances`, memory, CPU,
timeouts) carries the provisional shape of contract §10 and is not a benchmark
result; the upload cap, page limit, timeout pair, and capacity ceiling stay gated
on the CPU benchmark (`docs/ocr-v1-benchmark-report.md`).

The API service alone does not process anything: one versioned Cloud Run Job per
worker revision (`dev-ocr-service-worker-<slug>`), the reconcile and cleanup jobs,
and their Cloud Scheduler triggers are described in `docs/ocr-v1-dispatch.md` and
`docs/ocr-v1-lifecycle.md`.

## Deployment prerequisites — administrator actions

Read-only permission checks on `thanakrit.boonquarmdee@thaiunion.com` in
`thaiunion-ai-platform` did **not** return a complete build, image-upload,
bucket-create, private-IAM, or service-account-attachment path. The commands
below are the missing administrator actions; **none of them has been run, no
grant is claimed, and the deployment above cannot succeed until they are**
(`docs/ocr-v1-dispatch.md`, `docs/ocr-v1-cutover.md`).

```bash
# 1. Build + image upload (currently denied: artifactregistry.repositories.uploadArtifacts).
#    A repository that can hold the worker image by immutable digest, with cleanup
#    policies that never delete a pinned digest.
gcloud artifacts repositories create dev-ocr-service \
  --project thaiunion-ai-platform --location asia-southeast1 \
  --repository-format docker \
  --description 'OCR v1 API and worker images'
gcloud artifacts repositories add-iam-policy-binding dev-ocr-service \
  --project thaiunion-ai-platform --location asia-southeast1 \
  --member user:thanakrit.boonquarmdee@thaiunion.com \
  --role roles/artifactregistry.writer
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member user:thanakrit.boonquarmdee@thaiunion.com \
  --role roles/cloudbuild.builds.editor

# 2. Bucket IAM for the selected runtime identity (currently absent, and the
#    attempt to add it was denied for want of storage.buckets.setIamPolicy).
gcloud storage buckets add-iam-policy-binding \
  gs://dev-ocr-service-data-asse1-1022222817897 \
  --member serviceAccount:tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com \
  --role roles/storage.objectAdmin

# 3. Cloud Run Job permissions the API needs to read the pinned template and
#    launch it (contract §6.1: run.jobs.get, run.jobs.runWithOverrides).
gcloud iam roles create dev_ocr_service_dispatch \
  --project thaiunion-ai-platform --title 'OCR v1 dispatch' \
  --permissions run.jobs.get,run.jobs.run,run.jobs.runWithOverrides,run.operations.get
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member serviceAccount:tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com \
  --role projects/thaiunion-ai-platform/roles/dev_ocr_service_dispatch

# 4. Deployer attachment and the token path used by the verification command.
gcloud iam service-accounts add-iam-policy-binding \
  tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com \
  --member user:thanakrit.boonquarmdee@thaiunion.com \
  --role roles/iam.serviceAccountUser
gcloud iam service-accounts add-iam-policy-binding \
  tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com \
  --member user:thanakrit.boonquarmdee@thaiunion.com \
  --role roles/iam.serviceAccountTokenCreator

# 5. The caller identity (n8n, or a person for a manual call) may invoke the
#    private service, and no one else may.
gcloud run services add-iam-policy-binding dev-ocr-service-api \
  --project thaiunion-ai-platform --region asia-southeast1 \
  --member serviceAccount:<n8n-caller-sa>@<project>.iam.gserviceaccount.com \
  --role roles/run.invoker
```

Reusing `tu-ai-n8n-dev-runner-sa` for the API, worker, reconcile, and cleanup
roles is an explicit development choice with less role separation than the
original dedicated-identity design; a production deployment should revisit it.

Bucket settings to read back before any run (contract D17: versioning **disabled**,
soft-delete retention `0`, no locks):

```bash
gcloud storage buckets describe gs://dev-ocr-service-data-asse1-1022222817897 \
  --format='yaml(softDeletePolicy,versioning,iamConfiguration)'
```

## Verification

`scripts/verify_ocr_service.py` runs the asynchronous lifecycle end to end against
a deployed service — submit, poll, retrieve the result — and fails unless every
input returns a result body whose result and page keys, types, page numbering,
completeness, and text join match the contract exactly.

```bash
SERVICE_URL=$(gcloud run services describe dev-ocr-service-api \
  --project thaiunion-ai-platform --region asia-southeast1 \
  --format='value(status.url)')

uv run python -m scripts.verify_ocr_service \
  --base-url "$SERVICE_URL" \
  --file document.pdf \
  --gcloud-auth \
  --impersonate tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com
```

`--gcloud-auth` mints an ID token with `aud` = `--base-url` through the
impersonation path above, so the token matches `OCR_API_AUDIENCE`. Every run
consumes real OCR work and quota, so run it only under an approved document and
spend budget. The report contains metadata and key names only; `--include-responses`
additionally stores full OCR text and must be used only for an explicitly approved
local evidence run, because that text may be sensitive.

Local/fake acceptance is not cloud acceptance: fake-engine success in `uv run
pytest` says nothing about the deployed service, its IAM, or the real n8n token
path.

## Historical code (pre-OCR v1)

The synchronous five-template expense prototype is retained in this repository as
historical code, not as a service: `app/main.py`, `app/factory.py`,
`app/integrations.py`, `app/service.py`, `app/templates.py`, `app/verification.py`,
`app/config.py`, `scripts/serve_with_gcloud.py`, and the `examples/manifest.json`
corpus. None of it is an entrypoint, none of it is reachable from
`app/ocr_api:app`, and no command documented here starts it, so no deployed role
reaches Gemini, Document AI, or the `POST /v1/documents:process` route. Its tests
remain as baseline evidence for the old behaviour (`docs/ocr-v1-cutover.md`).

## n8n

`examples/n8n/ocr-upload-poll-result.workflow.json` is an inactive, credential-free
workflow definition for upload → poll → result, and `docs/ocr-v1-n8n-workflow.md`
explains it and the Google identity-token path. It has not been imported into any
n8n instance and must not be activated without explicit approval.

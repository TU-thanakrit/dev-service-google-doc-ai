# service_google_doc_ai

Asynchronous OCR-only service (OCR v1). A caller uploads one document over HTTP,
receives a job id, polls progress, and retrieves combined text plus numbered
per-page results. There is no classification and no structured business-field
extraction: the recognition engine is replaceable behind the service's own result
contract (`docs/ocr-v1-contracts.md`).

Roles: a private Cloud Run **service** (`app/ocr_api.py`) and separate Cloud Run
**Jobs** for the worker, reconciliation, and cleanup (`app/ocr_worker.py`,
`app/ocr_reconcile.py`, `app/ocr_cleanup.py`). Durable state, inputs, results, and
job metadata live in GCS. Direct callers of the private API authenticate with
Google identity; anonymous access to that backend is denied.

A separate public, **development-only** facade (`app/ocr_key_proxy_entrypoint.py`)
accepts `X-Demo-Key` and calls the private API using its own Google identity. All
facade callers share one backend team; there is no per-user isolation or edge
brute-force protection. Its development key is a plaintext Cloud Run environment
variable. This is **not production-ready**. See `docs/ocr-key-proxy.md`,
`deploy/ocr-key-api/sandbox.toml`, `ARCHITECTURE.md`, and `TODO.md`.

**Status:** the OCR backend is recorded as deployed privately in GCP. The authoritative mutable resource, identity, image, URL, revision, and acceptance facts are in `deploy/ocr-v1/sandbox.toml`; its redacted read-back is under `verification/deployments/`. Facade deployment facts are separate in `deploy/ocr-key-api/sandbox.toml`. Local/fake checks and live acceptance remain separate.

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

For a direct private-backend client, see `docs/ocr-v1-n8n-workflow.md` and
`scripts/verify_ocr_service.py`. The latter performs live submissions and an
idempotent replay, so it requires explicit approval for its complete request
budget. The Postman user guide was previously placed in `GUIDE.md`, but that file
was absent during the wrap-up inspection. Restore it from the earlier session or
ask the owner before publishing a link.

The token must be a Google-signed ID token whose `aud` is exactly
`OCR_API_AUDIENCE` (the service URL) and whose identity holds
`roles/run.invoker`; the application re-verifies it in-process, and an unmapped
identity is `403 team_not_authorized`. `X-Serverless-Authorization` is not read:
Cloud Run strips its signature, so it cannot be verified. See
`docs/ocr-v1-n8n-workflow.md` for the same flow from n8n.

An allowlist entry in `OCR_TEAMS` is keyed by the **pair** `<email>|<subject>`
(the ID token's verified `email` and `sub`), so an allowlisted address presented
with a different subject is unauthorized — a reused or forged address cannot
borrow a team. A key without both claims fails at startup.

## Local development

```bash
uv sync --python 3.12
uv run python scripts/check.py
```

That command is the canonical local gate: it checks the lockfile, runs Ruff, executes pytest, and prints authoritative JUnit totals. GitHub Actions runs the same gate and then verifies the production container's liveness and graceful shutdown.

Fake-mode demos run the whole lifecycle in one process with injected fakes, spend
no quota, and fail if a cloud client module is imported:

```bash
uv run python -m scripts.ocr_tracer_demo   # submit -> dispatch -> poll -> result, recovery, expiry, cleanup
uv run python -m scripts.ocr_worker_demo   # dispatch -> worker -> reconcile recovery paths
```

`.env.example` describes core settings and facade settings. The facade reads
process environment variables; it does not automatically read `.env`. Do not
blindly source the file: the example `OCR_TEAMS` value is JSON and needs correct
quoting for your chosen environment-file loader. Replace placeholders before
using a core role. `.env` is git-ignored.

### Run the facade locally without live OCR

From this repository's root, start a local server with a **throwaway test key**:

```bash
uv sync --python 3.12
export OCR_KEY_PROXY_BACKEND_URL='https://dev-ocr-service-api-1022222817897.asia-southeast1.run.app'
export OCR_KEY_PROXY_BACKEND_AUDIENCE="$OCR_KEY_PROXY_BACKEND_URL"
export OCR_KEY_PROXY_KEY='local-test-key-only-0123456789abcdef'
uv run uvicorn app.ocr_key_proxy_entrypoint:app --host 127.0.0.1 --port 8080
```

In another terminal on the same machine, test rejection without a key:

```bash
curl -i http://127.0.0.1:8080/health
```

Expected: HTTP `401`. Stop the server with Ctrl+C. For remote access to the
laptop, run `ssh -L 8080:127.0.0.1:8080 danny@<laptop-tailscale-address>` on the
client, then use `http://127.0.0.1:8080/health` on that client.

**Boundary:** This entrypoint has no fake backend or local Google-token fallback.
Any supported request with the valid local key attempts to contact the Cloud Run
metadata server and potentially the configured real backend; document submissions
can incur charges. Do not send a document without explicit approval. The fake-only
facade tests inject their own token provider and local backend:

```bash
uv run python -m pytest -o addopts='' tests/test_ocr_key_proxy.py --junitxml="${TMPDIR:-/tmp}/ocr-key-proxy-junit.xml"
```

Use a private temporary directory instead of `/tmp` if required by your environment.
The canonical full gate remains `uv run python scripts/check.py`. See
`docs/ocr-key-proxy.md` for configuration and the response/timeout contract.

## Deployment

The authoritative development deployment record is `deploy/ocr-v1/sandbox.toml`.
Read that manifest instead of copying mutable project, identity, image, revision,
URL, IAM, or schedule values from prose. The matching read-back evidence is
`verification/deployments/2026-11-09-thaiunion-shared-sandbox.md`.

Current high-level posture:

- private Cloud Run API plus version-pinned worker, reconciliation, and cleanup Jobs;
- GCS-only durable state in the same development project;
- n8n caller identity distinct from the broad development runtime identity;
- reconciliation every five minutes and cleanup every fifteen minutes;
- one approved synthetic one-page test through the facade succeeded; this does
  not establish n8n end-to-end acceptance or broad accuracy. See the facade
  manifest and its redacted verification report for scope and evidence.

The immutable image serves `app.ocr_api:app` by default; worker, reconciliation, and cleanup Jobs override that command. The manifest's `source.commit` identifies the code used to build the deployed image. Documentation- or CI-only commits made later do not change that image. Update the manifest only after reading the exact external resources back.

For an approved live document test, use the client URL and caller identity from
the manifest:

```bash
SERVICE_URL="<client_url from deploy/ocr-v1/sandbox.toml>"
CALLER_SA="<n8n_caller_email from deploy/ocr-v1/sandbox.toml>"

uv run python -m scripts.verify_ocr_service \
  --base-url "$SERVICE_URL" \
  --file /absolute/path/to/approved-document.pdf \
  --gcloud-auth \
  --impersonate "$CALLER_SA"
```

Every document submission can consume real OCR work. Use zero automatic retries
and an explicitly approved document/spend budget. Default reports are redacted;
full OCR responses require separate approval and access-controlled storage.

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

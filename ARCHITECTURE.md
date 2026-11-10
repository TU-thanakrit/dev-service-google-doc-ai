# Architecture

This document describes the repository and the `feature/ocr-key-api` worktree as inspected on 10 November 2026. The facade files are untracked on base commit `496e16d78a1c3cca891119965fe5e97e0e1f466c`; that commit alone does not reproduce the facade. The authoritative deployment records are `deploy/ocr-v1/sandbox.toml` and `deploy/ocr-key-api/sandbox.toml`. Read external resources again before acting on recorded state.

## System boundaries

```text
Client with X-Demo-Key
    -> public development-only key facade (Cloud Run service)
    -> metadata-server ID token for fixed backend audience
    -> private OCR API (Cloud Run service, Google identity + team mapping)
    -> GCS admission/idempotency/job records + pinned Cloud Run worker Job
    -> embedded PDF text OR rasterize and OCR each page
    -> GCS page checkpoints + final result
    -> client polls status and retrieves JSON through facade

Cloud Scheduler -> reconciliation Job / cleanup Job -> GCS
```

Direct clients of the private backend use a Google ID token with the configured audience and a verified `email|sub` mapped to a team. The facade removes client authentication headers and forwards a Google ID token minted for its runtime service account. All facade clients share that service account and team. It has no per-user isolation, edge brute-force control, or production security guarantee; the recorded dev deployment stores the shared key in a plaintext environment variable. The browser demo is a separate service and is not part of this worktree's facade.

The facade's allowed routes are `GET /health`, `POST /v1/ocr/jobs`, `GET /v1/ocr/jobs/{job_id}` and `GET /v1/ocr/jobs/{job_id}/result`. It authenticates before reading a body, limits requests to 20 MiB and upstream responses to 16 MiB, forwards a small safe header set, caps the metadata response, applies bounded timeouts, disables redirects/environment proxy inheritance, and never automatically retries POST. A failed or timed-out submit may already have created a billable job. See `docs/ocr-key-proxy.md` for details and `docs/ocr-v1-contracts.md` for the backend's normative contract.

## Stack and components

- Python (project range in `pyproject.toml`), FastAPI, Uvicorn, httpx, and Pydantic.
- GCS for durable objects; conditional generation writes coordinate concurrent operations. There is no SQL database.
- Cloud Run services for HTTP and Cloud Run Jobs for workers, reconciliation, and cleanup; Cloud Scheduler triggers maintenance jobs.
- Poppler for PDF inspection/text extraction and rasterization; Tesseract for OCR; ImageMagick handles TIFF frames. The engine is provisional and opt-in for development.
- `uv`, pytest, Ruff, and JUnit reporting for development and validation; container builds use `Dockerfile` or `Dockerfile.key-proxy`.

Key paths:

- `app/ocr_key_proxy.py`, `app/ocr_key_proxy_entrypoint.py`: isolated development facade and strict configuration.
- `app/ocr_api.py`: private backend composition. `app/ocr_tracer.py`: API-facing job lifecycle.
- `app/ocr_auth.py`, `app/ocr_admission.py`: token/team authorization and input/capacity admission.
- `app/ocr_store.py`, `app/gcs_objects.py`: object layout and conditional GCS adapter.
- `app/ocr_dispatch.py`, `app/ocr_pinning.py`: Cloud Run dispatch and pinned worker template checks.
- `app/ocr_worker.py`, `app/ocr_engine.py`: per-job orchestration and per-page text extraction.
- `app/ocr_reconcile.py`, `app/ocr_cleanup.py`: recovery, expiry, and deletion.
- `app/ocr_tracer_fakes.py`, `scripts/ocr_tracer_demo.py`, `scripts/ocr_worker_demo.py`: offline fakes and demos, not live cloud evidence.
- `tests/`: tests for contracts, facade, worker, storage seams, and opt-in real integrations.
- `docs/`: contracts, implementation details, and `ocr-key-proxy.md`. `deploy/`: environment manifests. `verification/deployments/`: redacted deployment and acceptance evidence.
- `examples/n8n/`: inactive workflow example, not proof of a deployed n8n integration.
- `app/main.py`, `app/service.py`, `app/integrations.py`, and `app/templates.py`: historical synchronous expense prototype, not the OCR v1 service entrypoint.

## Job lifecycle and invariants

1. Validate the caller, document, and `Idempotency-Key`. Admit capacity and persist the idempotency claim, job metadata, and input. Create a dispatch intent and attempt to launch the pinned worker. A `202` only means the job was accepted; dispatch acknowledgement is not completion.
2. A worker claims a lease with a fencing token and processes each page. A PDF page with usable embedded text can use `text_source: embedded`; otherwise it is rasterized and sent to OCR. Images use OCR. Write successful page checkpoints and a fence-specific result.
3. A create-if-absent `final.json` chooses the authoritative final result. Reconciliation repairs recoverable dispatch/lease failures and finalizes expired work. Cleanup removes data and records under generation preconditions.
4. Clients poll `accepted` or `running` until `succeeded`, `partially_succeeded`, or `failed`; then retrieve the result. A final result can still describe failed pages. `accuracy: not_verified` is not a quality guarantee.

For a live `(team_id, Idempotency-Key)`, the same bytes resolve to the existing logical job; different bytes conflict. Deduplication is not permanent after expiry/cleanup. Execution is at-least-once, not exactly-once. Do not use a new key to blindly retry an uncertain POST. Lifetime and retention are configured separately; the current design defaults are 24 hours from acceptance and 24 hours from finalization. The expiry gate can return `410` before cleanup, then `404` after records are removed. Physical deletion is not established merely by an expiry response.

## GCS object schema

Paths below are application object keys, not a relational schema. See `app/ocr_store.py` and `docs/ocr-v1-contracts.md` for exact fields, conditional writes, fencing, repair, and concurrency rules.

```text
idem/{team_id}/{sha256(team_id + "\n" + idempotency_key)}
inputs/{team_id}/{job_id}
jobs/{team_id}/{job_id}/job.json
jobs/{team_id}/{job_id}/claim.json
jobs/{team_id}/{job_id}/dispatch.json
jobs/{team_id}/{job_id}/pages/{page_number:05d}.json
jobs/{team_id}/{job_id}/result.{fence}.json
jobs/{team_id}/{job_id}/final.json
capacity/inflight.json
```

- The idempotency object associates a team-scoped key with the content identity and job.
- `job.json` contains job identity, input identity, acceptance/deadline and the worker/engine pin; `claim.json` holds the lease/fence; `dispatch.json` holds dispatch diagnostics.
- `pages/` holds successful page checkpoints; the fence-specific result contains the result body. `final.json` is the finalization and expiry authority.
- `capacity/inflight.json` is the conditional-write admission set. Do not replace it with a naive independently updated counter.

## Verification and change safety

Run `uv run python scripts/check.py` and the fake demos in README. The facade tests inject token providers and local upstreams; they do not establish real GCP behavior. Integration tests are opt-in and may require real resources. The recorded one-page live acceptance returned 202 on submit, a final succeeded state, and 200 on result with a conforming body and an expected reference. That evidence does not establish OCR accuracy, full-document class coverage, production security, or n8n acceptance; the recorded test did not capture `text_source`.

The deployment snapshot and image are documented in the facade manifest, including the allowlisted build-context hash. A repository-root container build copies more files than that staged context; do not assume it reproduces the deployed image. Do not deploy, change IAM, run paid tests, commit, or push without the appropriate separate approval.

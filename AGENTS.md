# AGENTS.md

This repository's production entrypoint is the asynchronous OCR-only service. A caller uploads one PDF/JPEG/PNG/TIFF document, receives a job id, polls status, and retrieves combined plus per-page Thai/English recognized text. The container serves `app.ocr_api:app`; worker, reconcile, and cleanup run as separate Cloud Run Jobs. The synchronous five-template expense/Gemini code remains only as historical, non-entrypoint code and must not be reintroduced into the deployed route surface.

Follow `docs/ocr-service-spec.md` for scope and `docs/ocr-v1-contracts.md` for the contract. Use simple injected interfaces and test at the public API lifecycle, storage coordination, and OCR engine seams. Never weaken team isolation, content protections, conditional writes, deadline/expiry gates, or engine/configuration pinning. Fake tests are not cloud evidence.

Development target: project `thaiunion-ai-platform`, account `thanakrit.boonquarmdee@thaiunion.com`, region `asia-southeast1`, resource prefix `dev-ocr-service`. Durable state is in `gs://dev-ocr-service-data-asse1-1022222817897` in project `thaiunion-shared-sandbox`. The selected development runtime identity is `tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com`; bucket access and required Cloud Run Job permissions still require administrator verification/grants. Specify account/project/region explicitly and do not change global defaults. Cloud uploads, paid workload tests, GitHub pushes, and IAM changes require explicit scoped approval and budget.

## Repository map

- `app/ocr_api.py`: production FastAPI composition; authenticated submission/status/result routes.
- `app/ocr_tracer.py`: application lifecycle and public OCR contract.
- `app/ocr_store.py`, `app/gcs_objects.py`: durable GCS job state and conditional-write adapter.
- `app/ocr_auth.py`: Google ID-token verification and caller-to-team mapping.
- `app/ocr_dispatch.py`: Cloud Run Jobs dispatch and template-pin checks.
- `app/ocr_worker.py`: one-job worker entrypoint.
- `app/ocr_reconcile.py`, `app/ocr_cleanup.py`: repair, deadline finalization, expiry, and deletion jobs.
- `app/ocr_engine.py`: embedded-PDF text plus Tesseract Thai/English adapter.
- `app/ocr_admission.py`: upload structure/size/page validation and capacity controls.
- `app/ocr_pinning.py`: immutable engine/worker configuration identity.
- `app/ocr_config.py`: `OCR_*` environment-backed settings.
- `app/ocr_verification.py`, `scripts/verify_ocr_service.py`: redacted async endpoint verification.
- `examples/n8n/ocr-upload-poll-result.workflow.json`: inactive n8n workflow artifact.
- `app/main.py`, `app/service.py`, `app/integrations.py`, `app/templates.py`: historical expense POC only; not a deployment entrypoint.
- `tests/`: offline tests plus explicitly environment-gated real engine/GCS checks.

## Environment

- Use Python 3.12: `uv sync --python 3.12`.
- Confirm dependencies: `uv lock --check`; do not hand-edit `requirements.txt`.
- Copy `.env.example` to ignored `.env` only for local work. Runtime variables use the `OCR_*` namespace documented there.
- Production roles require Application Default Credentials from their Cloud Run service account. Never store tokens, keys, OCR text, or document payloads in source control.
- OCR binaries required by the container: Poppler, Tesseract with `tha+eng`, and ImageMagick. Local binary execution is not container/deployment evidence.

## Commands

```bash
uv sync --python 3.12
uv lock --check
uv run python -m pytest -o addopts='' -q
uv run ruff check .
uv run uvicorn app.ocr_api:app --reload
```

Fake-mode lifecycle checks, which must not import cloud clients:

```bash
uv run python -m scripts.ocr_tracer_demo
uv run python -m scripts.ocr_worker_demo
```

Verify an approved deployed private service using an ID token acquired inside the verifier:

```bash
uv run python -m scripts.verify_ocr_service \
  --base-url "$SERVICE_URL" \
  --file /absolute/path/to/public-or-approved-document.pdf \
  --gcloud-auth
```

After dependency changes:

```bash
uv export --frozen --no-dev --format requirements-txt --no-hashes --output-file requirements.txt
```

## Contracts and conventions

- Submission is asynchronous: submit -> job id -> status polling -> final result. Launch acknowledgment is not OCR completion.
- `Idempotency-Key` is team-scoped. Same key/content reuses one logical job; different content conflicts. Execution is at least once, with at most one authoritative published result.
- Caller identity comes only from a verified Google ID token and server-owned team mapping. Ignore arbitrary identity/team headers. Foreign job ids return 404.
- Preserve one ordered entry per source page. Partial results retain successful pages and explicit failures; execution success never claims text accuracy.
- Use embedded PDF text only when validated usable; otherwise OCR. Keep source metadata and warnings separate from errors.
- Enforce upload byte/page/pixel limits and shared capacity before paid processing; fail closed on corrupt/unrecordable admission state.
- Stop new page work at the deadline, deny result access at expiry, and keep cleanup conditional/idempotent. Never claim exact-second physical erasure.
- A job's engine reference, worker image digest, versioned job name, timeout, and retries are immutable. Callers cannot choose an engine; automatic fallback is out of scope.
- Client errors and logs must use fixed safe categories. Never log raw exceptions, document content, OCR text, credentials, bucket object payloads, or tokens.

## Verification and sensitive data

- Offline pytest/fake-client success is separate from real GCS, Cloud Run, IAM, n8n, and OCR-accuracy acceptance.
- Real GCS tests require the explicit `OCR_GCS_INTEGRATION_*` environment and must use only the approved `dev-ocr-service-*` bucket; verify cleanup afterward.
- Real engine tests are environment-gated and may use only tracked public or explicitly approved fixtures. Do not commit raw OCR output.
- Default reports contain status, counts, safe metadata, and hashes only. Full OCR responses require explicit approval and access-controlled storage.
- Failed/timed-out live calls may still incur cost. Do not automatically replay paid requests.

## Pitfalls

- The bucket is cross-project: `OCR_PROJECT=thaiunion-ai-platform`, while `OCR_BUCKET_PROJECT=thaiunion-shared-sandbox`.
- The selected development service account does not yet have verified sandbox-bucket object access. Do not claim deployment ready until IAM read-back and a runtime test pass.
- The DEV engine is Tesseract 4.1.1 from a synthetic benchmark; the Debian container may install Tesseract 5.x. Production accuracy, version skew, and licensing evidence remain open.
- Upload/page/capacity defaults are conservative development bounds, not production throughput claims.
- `app/main.py` and `scripts/serve_with_gcloud.py` are historical. Do not use them in new runbooks or deployment commands.
- No GitHub Actions workflow exists; local lock, pytest, Ruff, demos, approved cloud gates, and n8n acceptance are distinct checks.
- This checkout tracks `TU-thanakrit/dev-service-google-doc-ai`; the corporate copy under `tu-ai-platform/assets/ocr` is separate. Do not synchronize or push between them without explicit instruction.

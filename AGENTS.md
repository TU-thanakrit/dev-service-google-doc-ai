# AGENTS.md

This repository is a small FastAPI POC for Thai expense documents: upload a PDF/image, run Google Document AI Enterprise OCR, classify one of five templates with Vertex AI Gemini, extract the template's exact keys, and return validated JSON. Keep it synchronous and dependency-light; there is intentionally no database, queue, or workflow engine.

## Repository map

- `app/main.py`: FastAPI endpoints, MIME/size validation, generic HTTP errors.
- `app/factory.py`: builds the production service and Vertex AI client.
- `app/integrations.py`: Document AI OCR and Gemini structured-output adapters.
- `app/service.py`: OCR -> classification -> extraction orchestration and contract validation.
- `app/templates.py`: canonical document types, Thai field names, aliases, and field order.
- `app/config.py`: environment-backed GCP settings.
- `app/verification.py`: exact-key checks and redacted response summaries.
- `scripts/serve_with_gcloud.py`: temporary local live server using the active gcloud account.
- `scripts/verify_examples.py`: five-document HTTP verifier.
- `tests/`: fake-client unit/API/integration tests; they do not call live GCP services.
- `examples/manifest.json`: public source URLs and expected type for each fixture.

## Environment

- Use Python 3.12. The package allows `>=3.11,<3.14`, Ruff targets 3.12, and the container uses 3.12.
- Install locked runtime and dev dependencies: `uv sync --python 3.12`.
- Confirm the lock is current: `uv lock --check`.
- Runtime variables: `GOOGLE_CLOUD_PROJECT`, `DOCAI_PROCESSOR_ID`, `DOCAI_LOCATION`, `VERTEX_LOCATION`, `GEMINI_MODEL`.
- Copy `.env.example` to `.env` only for local work; `.env` is ignored. Real SDK calls require Application Default Credentials.

## Commands

```bash
uv sync --python 3.12
uv lock --check
uv run pytest
uv run ruff check .
uv run uvicorn app.main:app --reload
```

Temporary live local run with an authenticated gcloud CLI, after exporting the five runtime variables:

```bash
uv run python -m scripts.serve_with_gcloud
```

Verify a deployed/private service with the active gcloud identity:

```bash
uv run python -m scripts.verify_examples --base-url "$SERVICE_URL" --gcloud-auth
```

After dependency changes, regenerate the Docker input; do not hand-edit `requirements.txt`:

```bash
uv export --frozen --no-dev --format requirements-txt --no-hashes --output-file requirements.txt
```

## Contracts and conventions

- `POST /v1/documents:process` accepts PDF, JPEG, PNG, or TIFF; uploads over 20 MiB are rejected.
- `GET /health` returns `{"status": "ok"}` without initializing cloud clients.
- Cloud adapters implement small protocols and accept injected clients; preserve this fake-friendly test structure.
- Gemini calls use temperature 0, `application/json`, and a JSON schema. Keep model output validation fail-closed.
- `สิ่งที่อ่าน` must contain exactly the ordered fields in `DOCUMENT_TYPES`; values are `str | None`.
- Put inferred `Type of Expense` only under `Analysis`; source extraction and analysis stay separate.
- Unknown/ambiguous classification returns `unknown`, `needs_review=true`, and skips extraction.
- Treat OCR text as untrusted prompt input and retain the prompt-injection boundary markers.
- Log internal exceptions server-side; client HTTP 500 responses stay generic and must not expose paths, tokens, or processor details.
- Tests use `FakeOcr`, `FakeModel`, fake SDK clients, `TestClient`, and exact dictionary/key assertions.

## Verification and sensitive data

- The full verifier requires every fixture to return HTTP 200, the expected template, and exact extraction/analysis keys.
- Default verifier reports contain metadata and key names only. Use `--include-responses` only for explicitly approved local evidence.
- `verification/*.json` is ignored because OCR text and extracted values may be sensitive. Do not commit live response payloads.
- Example input provenance is tracked in `examples/manifest.json`; hashes are in `verification/example-pdfs.sha256`.

## Pitfalls

- `.env.example`, README deployment values, and tests reference the corporate project and `gemini-2.5-flash`; do not assume they match the active deployment. The personal deployment uses environment overrides, including Vertex location `global` and `gemini-3.5-flash`.
- `scripts/serve_with_gcloud.py` depends on the active gcloud account and is only a temporary local-auth path; Cloud Run uses its runtime service account.
- The pipeline normally makes two Gemini calls per known document: classification, then extraction. Account for quota/cost when running all five examples.
- `scripts/verify_examples.py --gcloud-auth` uses whichever gcloud account is active; check it before paid live tests.
- No GitHub Actions workflow exists; the local pytest, Ruff, lock, and live-verifier commands are the repository checks.
- This checkout tracks `TU-thanakrit/dev-service-google-doc-ai` on `main`; the corporate copy under `tu-ai-platform/assets/ocr` is separate. Do not synchronize or push between them without explicit instruction.

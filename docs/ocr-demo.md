# OCR demo web service

`app/ocr_demo_entrypoint:app` is the deployable FastAPI app for a browser demo workflow. It does **not** change the existing OCR API (`app/ocr_api:app`) and must not be treated as the production OCR entrypoint.

**`production_ready=false`**

Current mutable deployment facts and redacted read-back evidence are authoritative in `deploy/ocr-demo/sandbox.toml` and `verification/deployments/2026-11-10-thaiunion-shared-sandbox-ocr-demo.md`.

The demo is intentionally narrow: it accepts one PDF in a browser, proxies it to the private OCR API, polls until a final job state, and returns the backend result JSON unchanged. It has no persistence, no submission retry, no raw OCR/document logging, and no caller identity beyond the app-level passcode.

`app/ocr_demo.py` remains the factory module for tests (`create_demo_app`, injected fakes). It does not define a silent fallback app and importing it never requires demo environment variables. The explicit entrypoint (`app.ocr_demo_entrypoint:app`) fails process startup when required/invalid configuration is present, so misconfiguration is visible at deploy/startup rather than at first request.

## Browser workflow

1. Select/upload one PDF.
2. The page previews the selected PDF locally (`URL.createObjectURL`) and sends the file with the passcode.
3. The demo submits the PDF to the private OCR API, polls status until a final state (`succeeded`, `partially_succeeded`, or `failed`), fetches `/result`, and displays the **exact** result JSON formatted in the page.
4. **Download JSON** exports that same response as a `.json` file (`Blob` download). No credentials are embedded in the page.

## HTTP surface

| Route | Auth | Purpose |
|---|---|---|
| `GET /` | public | Single-page HTML/CSS/vanilla-JS demo UI. |
| `GET /health` | public | Liveness (`{"status":"ok"}`). |
| `POST /api/ocr` | `X-Demo-Key` required | Multipart field `file` (PDF only). Returns backend OCR result JSON unchanged on success, or a fixed sanitized JSON error. |

An ASGI guard protects `POST /api/ocr` before multipart parsing: it checks `X-Demo-Key` and rejects missing/wrong keys with a fixed JSON 401 without reading the body. It bounds request bodies before parsing: declared over-limit requests (Content-Length beyond the configured PDF limit plus a small fixed multipart overhead allowance) are rejected without reading; undeclared/chunked bodies are bounded and replayed. The route keeps a defense-in-depth key check and returns fixed sanitized validation errors (missing file, invalid request shape) without leaking FastAPI details.

The processing endpoint is synchronous and reads the uploaded file synchronously so FastAPI uses its thread pool. Deploy with **Cloud Run concurrency=1** for this demo.

## Environment contract

| Variable | Required | Default | Meaning |
|---|---|---:|---|
| `OCR_DEMO_BACKEND_URL` | yes | — | HTTPS base URL of the private existing OCR API and Google ID-token audience. Validated at startup: `https`, hostname ends with `.run.app`, no credentials/query/fragment, root path only; trailing slash normalized. |
| `OCR_DEMO_KEY` | yes | — | App-level passcode required in `X-Demo-Key` for processing routes. |
| `OCR_DEMO_MAX_UPLOAD_BYTES` | no | `10485760` (10 MiB) | Maximum accepted PDF upload size, hard-capped at 10 MiB. |
| `OCR_DEMO_POLL_INTERVAL_S` | no | `2` | Seconds between status polls; must be positive and bounded. Sleep is capped to remaining deadline time. |
| `OCR_DEMO_POLL_TIMEOUT_S` | no | `300` | Absolute application deadline for token acquisition onward (as far as practical). Bounded. |

## Deployment

Build and deploy the demo with the dedicated image definition `Dockerfile.demo` (do not use the OCR API/worker `Dockerfile` for this service):

```bash
docker build -f Dockerfile.demo -t <demo-image> .
```

The image runs PID 1 as:

```bash
exec uvicorn app.ocr_demo_entrypoint:app --host 0.0.0.0 --port ${PORT}
```

Recommended Cloud Run settings for this demo:
- `--concurrency=1` (processing is synchronous and polls).
- Set the Cloud Run request timeout with margin **above** `OCR_DEMO_POLL_TIMEOUT_S` so the application can enforce its own absolute deadline and return a fixed timeout response where possible.
- Deploy only on Cloud Run (the demo obtains backend ID tokens from the Cloud Run metadata server).

Tradeoffs:
- The service is not a streaming/proxy-optimized design; it buffers bounded request data and polls the backend.
- A single absolute deadline covers token acquisition onward as far as practical. Per-request backend calls receive remaining-time timeouts, and the app checks the deadline before each call and before accepting a final state.
- Cloud Run instance/request timeout is an outer bound; it should be configured above the app timeout, but infrastructure can still terminate an overlong request.

## Backend call flow

For an authorized PDF upload, the demo:

1. Validates multipart `file` presence/type/size and PDF magic (`%PDF-`) before contacting the backend.
2. Starts one absolute application deadline and obtains a Google ID token for audience `OCR_DEMO_BACKEND_URL` (never logged/returned).
3. Generates a UUID idempotency key and submits `POST ${OCR_DEMO_BACKEND_URL}/v1/ocr/jobs` with multipart field `file` and `Idempotency-Key` (no submission retries). Upstream 400/413/415/422 submit rejections map to fixed client input errors without exposing bodies.
4. Polls `GET ${OCR_DEMO_BACKEND_URL}/v1/ocr/jobs/{job_id}` until a final state, with deadline checks and remaining-time request timeouts.
5. Fetches `GET ${OCR_DEMO_BACKEND_URL}/v1/ocr/jobs/{job_id}/result` before the deadline.
6. Returns that result JSON unchanged.

Every upstream JSON body must be an object; `job_id` is validated as a UUID before URL placement; `state` must be a string. Upstream failures and timeouts are translated to fixed JSON error messages. Backend error details/codes are never exposed to clients and are logged only through a fixed allowlisted category.

UI/JSON responses set `Cache-Control: no-store` and basic browser security headers (nosniff, frame policy, CSP compatible with the current inline page).

## Public service / app-key tradeoff

The service can be publicly reachable, but that is **not** equivalent to production access control. `X-Demo-Key` is a shared app-level passcode carried by the browser request:

- It is suitable only for constrained demos where exposure and key distribution are explicitly accepted.
- A shared passcode does not provide per-user identity, revocation/audit at user granularity, CSRF protection guarantees, or strong protection against key leakage from a browser/client environment.
- The private OCR API remains protected separately by Google identity; the demo must still run with a runtime identity allowed to call that backend.

For production use, replace this pattern with an approved authentication/authorization design and re-evaluate data handling, quotas, abuse controls, and observability before deployment.

## Local fake/test instructions

The public HTTP seams are testable without GCP or a real OCR API by injecting fake collaborators:

```bash
uv run pytest tests/test_ocr_demo.py -q
```

Tests cover the home page/security headers, health, missing/wrong demo key (including wrong key with declared oversize), declared oversize rejection before body receive, missing file sanitization, invalid PDF magic, file/type/size validation, backend URL validation, hard max cap, poll-interval bounds, successful submit→poll→result with exact JSON passthrough and UUID job IDs, deadline-before-final-result behavior, upstream non-object/invalid job ID handling, mapped submit client rejections, sanitized upstream failures/timeouts, fixed-category logging boundaries, synchronous processing endpoint, no submission retries, and entrypoint startup behavior (fails without env; serves UI/health with valid env). They do not call Google APIs and do not start a browser.

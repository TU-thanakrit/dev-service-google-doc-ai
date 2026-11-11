# Dev-only OCR API-key facade (`ocr-key-proxy`)

This is a separate, minimal **development-only** Cloud Run facade for the existing private OCR v1 API. It accepts a shared `X-Demo-Key` from callers and forwards a fixed allowlist of OCR routes to the configured private backend using a Google ID token minted from the Cloud Run metadata server for that backend's exact audience.

It does **not** change the OCR core, its contract, or its storage/auth model. **`production_ready=false`.** It is not a production authentication mechanism, provides **no brute-force edge rate limiting, no key rotation, no tenant isolation, and no strong production control**, and makes **no production-readiness or per-user isolation claim**. A shared key is a dev-only control: **all `X-Demo-Key` callers map to the facade runtime identity/team**, not to individual caller identities. The key is **not protection from bandwidth abuse**.

## Endpoints (forwarded allowlist)

Only these routes may be proxied; unknown paths or methods return `404`/`405` and are never forwarded:

- `GET /health`
- `POST /v1/ocr/jobs`
- `GET /v1/ocr/jobs/{job_id}`
- `GET /v1/ocr/jobs/{job_id}/result`

`{job_id}` must be a UUID. Query parameters are rejected (not forwarded). No browser workflow is provided or required; the facade transparently forwards request/response bodies for the supported document types (PDF/JPEG/PNG/TIFF) as submitted by clients.

## Authentication, identity, and upstream trust

- `X-Demo-Key` is required (constant-time comparison) for **all** forwarded routes, including `/health`. The deployed entrypoint requires a strong key: at least 32 non-whitespace ASCII characters (not surrounded by whitespace).
- A missing or incorrect key returns a fixed JSON `401` **before** any request body is read or parsed. The facade never proxies without a valid demo key.
- On a valid key, the facade fetches a Google ID token (with an email claim) from the fixed metadata host `metadata.google.internal` (`Metadata-Flavor: Google`, `format=full`) for the exact configured backend audience, using real async HTTP (`httpx.AsyncClient`) with bounded connect/read timeouts, redirects disabled, and proxy environment inheritance disabled (`trust_env=False`) for **both** metadata and backend requests.
- The token is attached as `Authorization: Bearer <token>` **only** to the exact configured backend origin. Client `Authorization`, `X-Demo-Key`, and arbitrary client headers are not forwarded upstream. Only `Content-Type`, `Idempotency-Key`, and optional `X-Request-Id` are forwarded when present and safe (header-injection values are rejected). The multipart request body and `Content-Type` boundary are preserved exactly.
- All upstream redirects are disabled. No arbitrary proxy URLs, no arbitrary outbound requests, and `POST` is never retried on transport uncertainty.
- One strict `asyncio.timeout` absolute deadline spans the metadata token request and backend submission/response read. Cancellation and timeout exceptions are handled safely; no internal exceptions or credentials are leaked.

## Limits and behavior

- Total request body is bounded before parsing/forwarding (default 20 MiB, inclusive, same ceiling as the OCR API). Declared-length and chunked bodies are enforced.
- On auth/unknown/declared-overlimit rejects the facade responds without reading the request body in application logic and returns `Connection: close` on HTTP/1.1. **Ingress/platform may still receive bytes; Cloud Run outer HTTP request size limits apply. The application does not claim an ingress-draining guarantee, and the key is not protection from bandwidth abuse.** On chunked oversize the facade stops reading app-side and closes where HTTP/1.1.
- Upstream connect/read/total deadlines are bounded. Upstream response is streamed and capped at **16 MiB** to avoid unbounded buffering while preserving normal JSON responses.
- Upstream status, response bytes, and `Content-Type` are passed through, along with safe correlation headers (`Retry-After`, `X-Request-Id`). No other backend response headers are exposed. The facade returns `Cache-Control: no-store` responses.
- Upstream transport, timeout, or metadata failures return fixed safe `502`/`504` JSON errors. An uncertain submission is not misclassified as safe to resend (no automatic POST retry).
- Logs must not include the backend token, demo key, document content, or OCR results.

## Configuration (entrypoint)

`app.ocr_key_proxy_entrypoint:app` requires:

- `OCR_KEY_PROXY_BACKEND_URL` (required): exact production backend origin `https://dev-ocr-service-api-1022222817897.asia-southeast1.run.app` (no variants/path/query/userinfo/port).
- `OCR_KEY_PROXY_KEY` (required): strong shared demo key (>=32 non-whitespace ASCII characters).
- `OCR_KEY_PROXY_BACKEND_AUDIENCE` (required): must equal that exact backend origin.

A local HTTP backend seam is available only in tests via `allow_http_backend`; it is unavailable in the production/deployed entrypoint.

## Cloud Run deployment constraints (dev-only)

Deployment MUST set: `min instances = 0`, `max instances = 1`, `concurrency = 1`, `memory >= 512MiB`, and `production_ready=false`. Public dev access is explicitly approved for this facade, but there is no brute-force edge rate limiting, no key rotation, no tenant isolation, and no strong production control. These are set externally.

## Backend permissions and acceptance prerequisites

- The facade's Cloud Run runtime service account must be allowed to obtain an ID token for the configured audience and must have Cloud Run invoker permission on the private OCR API so the minted identity is accepted by the backend/IAM.
- **Live unpaid acceptance** (no document processing cost): deploy the facade with valid env/config, call `/health` and a GET for a known/unknown job id with a valid/invalid `X-Demo-Key`, and confirm 401/404/200 pass-through and that no unauthenticated or unkeyed request reaches the backend.
- **Live paid acceptance** (incurs OCR processing cost, explicit approval/budget required): submit a real document via `POST /v1/ocr/jobs` with `X-Demo-Key` and `Idempotency-Key`, then poll status/result through the facade and confirm pass-through of async states and final JSON; do not replay paid requests automatically on failure.

## Container

`Dockerfile.key-proxy` builds a dedicated, nonroot image that starts the facade with `exec uvicorn ...` (PID 1, clean SIGTERM handling) and no fallback startup path.

# OCR demo deployment evidence — thaiunion-shared-sandbox

Recorded: 2026-11-10 (Asia/Bangkok)

Authoritative mutable facts: `deploy/ocr-demo/sandbox.toml`.

## Deployed service

- Project: `thaiunion-shared-sandbox`
- Region: `asia-southeast1`
- Service: `dev-ocr-demo-web`
- Browser URL: `https://dev-ocr-demo-web-1022222817897.asia-southeast1.run.app`
- Ready revision: `dev-ocr-demo-web-00001-8tm` (100% traffic)
- Runtime identity: `tu-ddai-ocr-sa@thaiunion-shared-sandbox.iam.gserviceaccount.com`
- Image: `asia-southeast1-docker.pkg.dev/thaiunion-shared-sandbox/dev-ocr-service/ocr-demo@sha256:2f4ad6a483098301c1ac6f1cf4477d699b723156d40184d9595a842b01aef8bc`
- Cloud Build: `f6ec6ef5-167c-4998-a768-db2599c47d3e`
- Build context SHA-256: `9eb9117a182cbe2de2c817d4e1e3180a733d38ddbc4140fbe14cd5d645d6c8e3`

At deployment time, the five-file allowlisted context contained `Dockerfile` (copied from `Dockerfile.demo`), `requirements.txt`, `app/__init__.py`, `app/ocr_demo.py`, and `app/ocr_demo_entrypoint.py`. The deployed source was an uncommitted snapshot on `feature/ocr-demo-ui`; no commit or push had been performed at that point. A later source commit does not change the running image.

On 2026-11-11, the tar snapshot was rebuilt locally using the original file-copy and tar procedure. Its SHA-256 matched the recorded `9eb9117a182cbe2de2c817d4e1e3180a733d38ddbc4140fbe14cd5d645d6c8e3`. No image was rebuilt or redeployed as part of this check.

## Access path

The browser service is publicly reachable, but `POST /api/ocr` requires a shared `X-Demo-Key`. The key is not recorded here. A local mode-0600 copy is at `~/.config/ocr-demo/passcode`.

The demo runtime identity has `roles/run.invoker` on private service `dev-ocr-service-api`. The OCR API maps the verified identity pair:

```text
tu-ddai-ocr-sa@thaiunion-shared-sandbox.iam.gserviceaccount.com|100484182732107656230 -> team-demo
```

Updating that mapping created backend revision `dev-ocr-service-api-00004-hk5`, serving 100% of backend traffic.

## Verification performed

- Canonical local gate: 484 tests total; 476 passed; 8 environment-gated skips; zero failures/errors.
- Demo suite: 56 passed.
- Ruff and lock checks: clean.
- Local image from the exact staged context:
  - `/health` returned 200.
  - UI contained upload and Download JSON controls.
  - container ran as UID 10001.
  - PID 1 was Uvicorn.
  - SIGTERM exited 0.
- Cloud read-back:
  - immutable image digest matched;
  - runtime identity, max instances 1, concurrency 1, CPU 1, memory 512 MiB and timeout 360 seconds matched;
  - revision ready with 100% traffic;
  - `allUsers` has Invoker on the demo service only.
- Live no-cost probes:
  - anonymous `GET /health` returned 200;
  - anonymous `GET /` returned the upload UI;
  - wrong demo key returned 401 fixed JSON;
  - correct key plus an intentionally malformed `%PDF-` file returned 400 fixed JSON;
  - the private backend recorded the corresponding `POST /v1/ocr/jobs` as HTTP 422 on revision `dev-ocr-service-api-00004-hk5`, establishing Cloud Run invocation plus in-application team authorization without dispatching OCR.

## Unverified and blocked

- No valid document was submitted. Paid/live OCR requests remain **0**.
- Real extraction accuracy, JSON content and browser JSON download remain unverified against a valid PDF.
- The user must explicitly approve one non-sensitive test PDF and a one-request OCR budget before live processing.
- Secret Manager was not used because the deployer lacks `secretmanager.secrets.create` and `secretmanager.secrets.setIamPolicy`. The shared key is therefore a plaintext Cloud Run environment variable. This, public transport, lack of per-user identity and lack of rate limiting keep `production_ready=false`.

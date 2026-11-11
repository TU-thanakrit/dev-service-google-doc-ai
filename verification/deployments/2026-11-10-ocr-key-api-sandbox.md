# OCR shared-key API — sandbox deployment, 2026-11-10

Development-only. Not production ready. The service is publicly reachable and authenticates application requests with one shared `X-Demo-Key`; it has no per-caller isolation or edge brute-force/rate limiting. The approved development key is a plaintext Cloud Run environment variable, not Secret Manager. Do not put the key or documents in logs or deployment evidence.

## Deployed target

- Project: `thaiunion-shared-sandbox`; region: `asia-southeast1`.
- Service: `dev-ocr-key-api`; URL: `https://dev-ocr-key-api-1022222817897.asia-southeast1.run.app`.
- Ready revision: `dev-ocr-key-api-00001-7fv`, 100% traffic.
- Cloud Build `92d7ee98-335a-4718-97bf-dcacc8a007b0`: SUCCESS.
- Immutable image: `asia-southeast1-docker.pkg.dev/thaiunion-shared-sandbox/dev-ocr-service/ocr-key-api@sha256:8ca6ce49032dfb7d0a97b31520ce794281b853aa76bc4aaa03b7b9995e61cf8c`.
- Runtime: `tu-ddai-ocr-sa@thaiunion-shared-sandbox.iam.gserviceaccount.com`; 1 CPU, 512 MiB, timeout 90 s, max instances 1, concurrency 1, min instances 0.
- Public transport via `allUsers` Cloud Run Invoker binding. The private backend IAM readback contains only authorized principals and no `allUsers` binding.
- Runtime service identity invokes the private backend and maps to `team-demo`; callers are not individually isolated.
- Separate dev key resides locally at `~/.config/ocr-key-api/passcode` (mode 0600); it is not reproduced here.

## Source and verification

- Working tree `feature/ocr-key-api`, base commit `496e16d78a1c3cca891119965fe5e97e0e1f466c`; source changes not committed/pushed.
- Build context snapshot SHA-256: `60d06464abce8888aa7cb8058a6e2b475544bb623bfd26f101759760f7cc017e`. Allowlist: `Dockerfile` (copied from `Dockerfile.key-proxy`), `requirements.txt`, `app/__init__.py`, `app/ocr_key_proxy.py`, `app/ocr_key_proxy_entrypoint.py`.
- OpenCode targeted JUnit: 54 passed. Full checks: 474 passed, 8 skipped, 0 failures/errors; Ruff and lock checks passed.
- Local image: starts as UID 10001; missing and wrong keys returned 401; graceful stop exit code 0.
- Before public binding, anonymous health returned 403, authenticated health with no/wrong key returned 401, correct key health returned 200, unknown job returned 404 `not_found`, malformed PDF returned 422 `malformed_document`.
- After public binding, without any Google token: missing/wrong keys returned 401 for health, submit, status and result; correct key health returned 200; synthetic unknown status/result returned 404 `not_found`; synthetic malformed PDF returned 422 `malformed_document`.
- A malformed test fixture was submitted for validation. Subsequently, the user approved one synthetic one-page PDF; exactly one document submission was attempted and no POST was retried. Submit returned 202; status polling observed accepted then succeeded; result returned 200 with a conforming `ocr.v1` result, one succeeded page, and the expected synthetic reference present. The API reports accuracy `not_verified`.
- Redacted live acceptance journal: `verification/deployments/2026-11-10-ocr-key-api-one-page-acceptance.json`. This validates one synthetic fixture, not a comprehensive accuracy, load, tenant, or production security test.

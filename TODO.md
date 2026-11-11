# TODO and known limitations

Inspection snapshot: 10 November 2026, `feature/ocr-key-api` on base `496e16d`. This is a bounded review, not proof that all bugs or missing error handlers have been found. No item below authorizes deployment, IAM changes, paid OCR, commits, or pushes.

## Confirmed code and documentation defects

- [ ] **Key validation does not match its stated rule.** `app/ocr_key_proxy.py:validate_demo_key` checks minimum length, ASCII, and leading/trailing whitespace but accepts internal whitespace. The entrypoint and error message state a non-whitespace rule. Add failing tests for internal whitespace and control characters, decide the exact allowed character set, fix validation, and review compatibility with any deployed key before rollout. This is a configuration-validation gap, not a demonstrated authentication bypass.
- [ ] **Test-only HTTP backend URL seam accepts password-only userinfo.** The `allow_http_backend=True` branch in `app/ocr_key_proxy.py` checks username but not password. Add a negative test and reject both. The production entrypoint uses the stricter HTTPS/exact-origin validator; no production exposure is claimed from this test seam.
- [ ] **Correct stale module documentation.** `app/ocr_config.py` says deployment is blocked and `app/ocr_auth.py` says no deployed composition exists. Those statements conflict with the current recorded deployment and `app/ocr_api.py`. `docs/ocr-v1-contracts.md` points to nonexistent `app/ocr_contract.py`; the current code defines vocabulary in `app/ocr_tracer.py`. Update carefully without changing normative behavior.
- [ ] **Reconcile facade docs with implementation.** `docs/ocr-key-proxy.md` omits forwarding `Content-Encoding` and does not clearly say the total timeout includes inbound body receipt. Align it with `app/ocr_key_proxy.py` and regression tests.
- [ ] **Make environment setup unambiguous.** The existing `OCR_TEAMS` example contains JSON that is not safely loaded by all dotenv/shell parsers. The facade itself reads process environment only. Provide tested role-specific configuration and loader instructions without exposing credentials.

## Security and operations (development only)

- [ ] Replace the public shared key with an approved authentication/authorization design before production. Current callers share one runtime identity/team and lack per-user identity/isolation and edge brute-force rate limiting.
- [ ] Replace the plaintext Cloud Run key environment value with an approved secret storage and rotation strategy; scope runtime access and verify rollback. Do not rotate the live development key without approval and a consumer migration plan.
- [ ] Review ingress abuse, request size, quota/spend controls, monitoring, key leakage, retention, and incident response. `production_ready=false` remains until an explicit production review.
- [ ] Review the broad development runtime service account and existing role grants; move toward least privilege with explicit approval.
- [ ] Keep the backend private. Reverify both IAM policies and the exact deployed revision after any authorized deployment or access change.

## Testing and acceptance gaps

- [ ] Add a local facade fake-runner or dedicated container smoke test. `scripts/container_smoke.py` exercises the core image, not the facade. Avoid introducing a fake fallback in the deployed entrypoint.
- [ ] Add regression tests for key whitespace/control characters and test-only URL credentials. Test real timeout, response-size limits, no redirects, no proxy inheritance, and no POST retries when changing the facade.
- [ ] One approved synthetic one-page document succeeded through the facade; that does not prove OCR accuracy or representative document coverage. The record does not capture whether the text came from the PDF's embedded layer or OCR. Separately approve paid tests and their exact request budgets before running them.
- [ ] Verify live GCS concurrency/recovery, worker execution under failures, real-engine quality for representative Thai/English scans, expiry/cleanup behavior, and n8n end-to-end access separately. Local fakes and a single live fixture do not prove these properties.
- [ ] Verify effective live page and document limits rather than equating source defaults with deployed settings.
- [ ] Cover timeout/connection-uncertain client guidance: preserve the original file and idempotency key, do not automatically retry a paid POST, and do not assume permanent deduplication.

## Build, configuration, and mock inventory

- [ ] The facade was deployed from an uncommitted snapshot. Its source, tests, and evidence were later committed in `cb17233`; base commit `496e16d` alone does not reproduce that snapshot. Verify source and build-context hashes before any rebuild or redeployment.
- [ ] `Dockerfile.key-proxy` copies `app/` and installs root `requirements.txt`. The recorded deployment used a small allowlisted build context. A repository-root build is not equivalent and may include unnecessary code/dependencies. Add a reproducible build-context script and consider a minimal dependency set.
- [ ] The facade is fixed to the single development backend URL (`PRODUCTION_BACKEND_URL` in `app/ocr_key_proxy.py`; despite its name, it is a development service). Make any change to the fixed-origin boundary deliberate and test exact audience, URL validation, and token handling.
- [ ] `app/ocr_tracer_fakes.py`, fake engines/stores, local token providers, tests, and synthetic PDFs are intentional test fixtures, not live implementations. Keep them separate from production entrypoints. Sample keys in `.env.example`, README, and tests are placeholders and must never be deployed as secrets.


## Other error handling and lifecycle considerations

- [ ] Investigate unexpected `502`/`504` with sanitized diagnostics that never log keys, tokens, document content, or OCR output. A submit timeout can hide an accepted job.
- [ ] Keep `final.json` as the final-state/expiry authority and preserve object generation preconditions, lease fencing, page checkpoints, and admission-set invariants when fixing recovery or cleanup.
- [ ] Treat `404` after cleanup and `410` at the expiry gate as access outcomes, not proof of physical data erasure. Review retention and lifecycle policies separately.

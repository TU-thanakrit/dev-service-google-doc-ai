# OCR v1 — local cutover record (issue #12, code and docs only)

Status: this is the historical local cutover record. The development service was subsequently deployed; current mutable project, identity, image, URL, revision, IAM, Scheduler, and acceptance facts live only in `deploy/ocr-v1/sandbox.toml`, with read-back evidence under `verification/deployments/`. Treat the pre-deployment blockers below as audit history, not current state.

Scope of this record, per the ticket's execution boundaries: source, configuration,
documentation, an inactive workflow artifact, and administrator command blocks.
No cloud write, no IAM change, no image build or upload, no n8n import or
activation, no commit to the corporate clone, and no paid workload.

## What changed

| File | Change |
|---|---|
| `Dockerfile` | The default `CMD` now serves `uvicorn app.ocr_api:app`; a comment names the four roles and states that the expense modules are historical, non-entrypoint code |
| `README.md` | Rewritten for the asynchronous OCR service: routes, GCP identity, submit → poll → result examples, local fake-mode demos, the GCP target and selected runtime identity, the documented deploy command (which starts `app.ocr_api:app`), the administrator prerequisites, and the new verification tool |
| `.env.example` | Now the `OCR_*` settings every role composes, with target values; the `GOOGLE_CLOUD_PROJECT`/`GEMINI_MODEL` block is gone |
| `app/ocr_verification.py` | New: checks a returned result body against contract §2.5/§2.6 and yields a text-free summary |
| `scripts/verify_ocr_service.py` | New: the async verifier — submit, poll with `Retry-After`, retrieve the result, re-submit the same idempotency key, and verify the body |
| `scripts/verify_examples.py` | Deleted: it posted to the retired `/v1/documents:process` and asserted the five expense templates |
| `examples/n8n/ocr-upload-poll-result.workflow.json` | New: inactive, credential-free n8n definition for upload → poll → result |
| `docs/ocr-v1-n8n-workflow.md` | New: the workflow guide and the identity-token path |
| `docs/ocr-v1-cutover.md` | New: this record |
| `tests/test_ocr_api_routes.py` | New: the production-route contract and entrypoint/build-context checks |
| `tests/test_ocr_verification.py`, `tests/test_verify_ocr_service.py`, `tests/test_n8n_workflow_artifact.py` | New: offline coverage for the new verification code and the artifact |

## The served surface

- The container's default command is
  `uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}`; the worker, reconcile,
  and cleanup jobs override it with `python -m app.ocr_worker`,
  `python -m app.ocr_reconcile`, and `python -m app.ocr_cleanup`.
- `tests/test_ocr_api_routes.py` imports the module the container runs, composed
  with injected dependencies only (a stub store factory, a stub credential factory,
  and a stub session — no GCS client, no credential lookup, no network), and asserts
  that its `/v1` surface is exactly `POST /v1/ocr/jobs`,
  `GET /v1/ocr/jobs/{job_id}`, `GET /v1/ocr/jobs/{job_id}/result`, plus `/health`.
  A second test asserts that no route matches `/v1/documents:process` and that both
  `GET` and `POST` on it answer `404`, so the retired expense path is unreachable
  from the served app.
- Two further tests keep the entrypoint from drifting back: the documented deploy
  command and the Dockerfile must name `app.ocr_api:app` and must not name
  `app.main`, and every `COPY` source of the build context must exist and not be
  excluded by `.dockerignore`.

## Retired path and retained code

Deleting `app/main.py`, `app/service.py`, `app/integrations.py`, `app/templates.py`,
`app/verification.py`, `app/config.py`, and `app/factory.py` would have churned
`tests/test_api.py`, `tests/test_service.py`, `tests/test_integrations.py`,
`tests/test_templates.py`, `tests/test_verification.py`, and `tests/test_config.py`
at once — a large diff in the same slice that has to be provably safe. They are
therefore retained as historical code with three properties, each of which is
asserted or documented rather than assumed:

1. none of them is an entrypoint: the Dockerfile's `CMD` and every documented
   command start the OCR roles only;
2. none is reachable from `app.ocr_api:app`, proven by the route-table test;
3. no documented procedure sends them anywhere: the README documents the OCR
   lifecycle, and the old local script (`scripts/serve_with_gcloud.py`, which
   builds Document AI and Gemini clients) is no longer referenced by any
   documented command.

The five-template corpus (`examples/manifest.json`, `verification/`) and the
verifier that consumed it are historical evidence of the old behaviour; the old
tests remain as baseline evidence for the old synchronous app, not as evidence that
the asynchronous design works.

`AGENTS.md` was aligned after the user explicitly approved the protected edit. It
now names the OCR production entrypoint, `OCR_*` environment, current module map,
async verifier, cross-project bucket, selected development runtime identity, and
the separation between offline and cloud acceptance. The historical expense module
is explicitly non-entrypoint code.

The per-slice records (`docs/ocr-v1-dispatch.md`, `-lifecycle.md`, `-pinning.md`,
`-recovery.md`, `-engine-adapter.md`, `-admission.md`) each say that the
synchronous app is untouched, because that was true for their slice. The dispatch
record, which carries the container commands, is updated for this cutover; the
others are historical records of their own slice and should be read as such.

## Offline evidence (this worktree, this branch)

| Check | Command | Result |
|---|---|---|
| Tests | `uv run pytest` | 324 passed, 8 skipped |
| Lint | `uv run ruff check .` | clean |
| Lock | `uv lock --check` | current (59 packages) |
| Demo (lifecycle) | `uv run python -m scripts.ocr_tracer_demo` | exit 0, "no cloud clients imported" |
| Demo (dispatch/worker/reconcile) | `uv run python -m scripts.ocr_worker_demo` | exit 0, no cloud clients |
| Dockerfile syntax / build context | offline test in `tests/test_ocr_api_routes.py` | pass |
| Dockerfile build check | `docker buildx build --check .` | **blocked**: the sandbox has no registry access (`lookup registry-1.docker.io ... connection refused`), so the base-image metadata cannot be resolved; the offline context check stands in for it |
| Image build and push | not attempted | needs the Artifact Registry grants below |

The 27 tests added by this slice are green; the 8 skips are pre-existing and
environment-gated (7 engine-binary integration tests, 1 live GCS integration test).

## Historical acceptance status at cutover implementation

The table and blocker list below capture the state before deployment. Current status is in `deploy/ocr-v1/sandbox.toml` and its evidence report; unresolved items there remain n8n end-to-end verification and paid/live OCR acceptance.

| Ticket acceptance criterion | Status at that time |
|---|---|
| Use the approved project/region, snapshot, identities, limits, storage config, spend budget; read back runtime state before testing | **Blocked.** Target and resource facts are documented (`README.md`, `docs/ocr-v1-dispatch.md`); no runtime state was read back and no budget was approved |
| Verify n8n obtains/refreshes the intended Google identity tokens and maps to the correct team | **Blocked.** The metadata/impersonation paths and the mapping are documented (`docs/ocr-v1-n8n-workflow.md`); nothing was executed and the configuring human's identity is not assumed to be the runtime caller |
| Run a real n8n workflow from upload through polling to final retrieval | **Blocked.** An inactive artifact and a runbook exist; no instance was contacted |
| Verify unauthenticated and cross-team denial, idempotent resubmission, duplicate delivery/recovery, global bounds, expiry denial, and cleanup | **Partially met offline, blocked in cloud.** Cross-team `404`s, idempotent resubmission, duplicate dispatch, expiry denial, and cleanup are covered by fake-store tests at the approved seams; nothing about real GCS, IAM, or Cloud Run is verified |
| Retire the old expense behaviour, update instructions and examples for OCR-only use, and confirm no unintended Gemini/expense path remains reachable | **Met locally.** Container command, documented commands, and the served route table are asserted; the Gemini/expense modules stay as unreachable historical code |
| Safe example caller instructions, and an inactive reusable workflow configuration if exported; no activation without approval; no credentials in exported evidence | **Met locally.** README caller examples, `.env.example`, an inactive credential-free workflow artifact, and a test that the artifact carries no secret |
| Redacted evidence ledger with deployed image/revision references, checks, attempt counts, failures, limitations | **Partially met.** This record is the offline ledger; it can carry no deployed image or revision reference because no deployment exists |
| Complete offline tests, lint, lock checks, build verification; mark release blocked if real acceptance is unperformed | **Met offline, release marked blocked.** See the table above — required cloud and n8n acceptance is unperformed |

## Historical planned closure steps

1. **Administrator actions** — the Artifact Registry repository plus build/upload
   permission, the bucket `roles/storage.objectAdmin` binding for
   `tu-ai-platform-sa@thaiunion-ai-platform.iam.gserviceaccount.com`, the Cloud
   Run Job permissions the API needs (`run.jobs.get`, `run.jobs.runWithOverrides`),
   the deployer's `iam.serviceAccountUser`, the caller's `roles/run.invoker`, and the
   token-creator grant used by the verification command. Exact commands are in
   `README.md`; none has been run and no grant is claimed.
2. **A scoped deployment and spend approval**, then the deploy in `README.md`, the
   worker/reconcile/cleanup jobs, and the Cloud Scheduler triggers
   (`docs/ocr-v1-dispatch.md`, `docs/ocr-v1-lifecycle.md`).
3. **An approved document/spend budget** for the first real lifecycle run
   (`scripts/verify_ocr_service.py`), followed by the n8n run in
   `docs/ocr-v1-n8n-workflow.md`. Both must record a redacted evidence ledger with
   the deployed image digest and revision, attempt counts, and observed failures.

Until those happen, treat every number in this repository as a provisional
configuration value, not as measured behaviour, and treat "the service works" as
unproven.

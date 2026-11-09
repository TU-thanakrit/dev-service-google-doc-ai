# OCR v1 — dispatch and private worker execution (issue #6)

Status: implemented. This document preserves the dispatch protocol and pre-deployment audit history; current mutable deployment facts are authoritative only in `deploy/ocr-v1/sandbox.toml`, with read-back evidence under `verification/deployments/`. Do not use the older target/IAM blocker text below as live configuration. It implements
`docs/ocr-v1-contracts.md` §5.3, §5.4, §5.5, and §6.1–§6.2 in the parts named
here and nothing more.

## What changed

| Module | Role |
|---|---|
| `app/ocr_dispatch.py` | `launch()` (the shared dispatch step) and `CloudRunJobsDispatcher`, the Cloud Run Admin `jobs:run` adapter |
| `app/ocr_worker.py` | Cloud Run Jobs worker entrypoint: claim the lease, recognize, publish |
| `app/ocr_reconcile.py` | Cloud Run Jobs reconciliation entrypoint: repair undispatched, lost-ack, and failed-launch jobs |
| `app/ocr_config.py` | `OCR_*` environment settings for every role |
| `app/ocr_api.py` | the Cloud Run API composition (a separate app; `app/main.py` is historical code) |
| `app/ocr_store.py` | added the `claim.json` lease and the dispatch-attempt bookkeeping |

The synchronous expense app (`app/main.py`, `POST /v1/documents:process`) is
retained as historical code that no command starts; the service's default
container command is now this composition
(`uvicorn app.ocr_api:app`, `Dockerfile`), and the retired route is not served by
it. Issue #12's cutover record is `docs/ocr-v1-cutover.md`.

## Launch acknowledgment is not OCR completion

Four states are distinct and each is observable:

1. **Accepted** — `job.json` + input + a launch intent exist; `POST /v1/ocr/jobs`
   returns `202`.
2. **Launch acknowledged** — the `jobs:run` call returned. This is recorded in
   `dispatch.json` as `acknowledged` + `execution_ref`, is diagnostic only [D31],
   and no decision reads it.
3. **Processing** — a worker holds the `claim.json` lease and sets `running`.
4. **Completed** — a worker publishes `result.{fence}.json` and the single
   `final.json` arbiter. The status/result routes report the state from
   `final.json`.

A successful launch therefore never finalizes a job: `GET .../result` stays `202`
until a worker publishes.

## Lease and dispatch bounds

- `claim.json` is create-once with `fence: 1`. A second execution arriving while
  the lease is live writes nothing and exits non-zero. An expired lease is
  taken over by a conditional replace with `fence + 1`, so a dead holder recovers
  after at most one TTL.
- Lease TTL is 5 minutes and dispatch spacing is 5 minutes, with at most 2
  dispatch attempts [D15, D14]; both are benchmark-gated ([B3]) and **not**
  approved numbers.
- **Renewal is the worker's heartbeat.** The worker renews its lease with a CAS
  replace before every page and before publication, keeping `owner` and `fence`.
  A renewal that gets `412` means another execution has taken the lease over, so
  the attempt stops and writes nothing: no page, no result, no `final.json`.
  Renewal generations are therefore not the lease object's creation generation,
  and release is a conditional delete at a fresh read plus a holder check.
- **Renewal and takeover are the same conditional replace, and neither is a lock.**
  Both are read-then-act on `claim.json` alone, so they *detect* a takeover rather
  than exclude one: inside the read-to-replace window two executions can both
  believe they hold the lease, which is why §5.3 claims no mutual exclusion. What
  the fence does guarantee is at most one **authoritative result** — each execution
  writes its own `result.{fence}.json` and only `final.json` picks the winner —
  never at-most-once execution.
- **Publication is fenced.** Each execution publishes `result.{lease.fence}.json`
  and only `final.json` selects the authoritative one, so a stale execution can
  never rewrite another execution's result object; the loser's object stays
  unpublished until cleanup enumerates `result.*`.

## Reconciliation

`reconcile()` scans every non-final job with conditional single-object writes and
applies the first matching row:

| Condition | Action |
|---|---|
| `now >= deadline_at` | finalize `deadline_exceeded` (never `succeeded`, D42) |
| last launch error non-retryable | finalize `engine_unavailable` + alert (no second launch, §6.1 step 5) |
| input absent past `materialization_grace`, no live lease | finalize `internal_error` + conditional `idem` delete (§6.2) |
| no `dispatch.json` | create the intent and launch (crash before launch, lost `202`) |
| attempts exhausted, other | leave it: the deadline ends it |
| live lease held | leave it |
| else, spacing elapsed and attempts remain | claim one more attempt and launch |

`final.json` is the single arbiter, so two reconcilers (or a reconciler and a
worker) can never produce two authoritative completions. Not implemented and
named rather than missing: reconciliation does not resume page work. The capacity
admission set (§6.4) is now added by issue #10 (`docs/ocr-v1-admission.md`).
Cleanup (§6.3) is now `app/ocr_cleanup.py` (issue #9,
`docs/ocr-v1-lifecycle.md`), which is the physical deleter; reconciliation no
longer leaves it implicit.

## Container command and args

One image, four commands (`app.ocr_api`, `python -m app.ocr_worker`,
`python -m app.ocr_reconcile`, `python -m app.ocr_cleanup`):

| Role | Cloud Run kind | Command / args | Notes |
|---|---|---|---|
| API | Service | `uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}` | private; needs an ID token |
| Worker | Job `dev-ocr-service-worker-<slug>` | `python -m app.ocr_worker`, no args | the job name is the version pin; identity arrives as env overrides |
| Reconcile | Job `dev-ocr-service-reconcile` | `python -m app.ocr_reconcile`, no args | run by Cloud Scheduler |
| Cleanup | Job `dev-ocr-service-cleanup` | `python -m app.ocr_cleanup`, no args | physical deletion only; added by issue #9 (`docs/ocr-v1-lifecycle.md`), run by Cloud Scheduler |

Environment (`OCR_*`): `OCR_PROJECT`, `OCR_BUCKET`, `OCR_BUCKET_PROJECT` (the
bucket's own project; defaults to `OCR_PROJECT`), `OCR_REGION`,
`OCR_WORKER_JOB_NAME`, `OCR_WORKER_CONTAINER_NAME`, `OCR_WORKER_REVISION_DIGEST`,
`OCR_WORKER_ARGS`, `OCR_WORKER_ENV`, `OCR_ENGINE_REF`, `OCR_TASK_TIMEOUT_S`,
`OCR_TASK_MAX_RETRIES`, `OCR_API_AUDIENCE`, `OCR_TEAMS` (a JSON
`<email>|<subject> -> team_id` map, so a binding carries both verified claims),
and for the worker `OCR_JOB_TEAM_ID` / `OCR_JOB_ID`, which the launch
injects into the override env from the *accepted job* on every launch (a pinned
env value for either name is dropped, so a stale pin can never point an execution
at another job). The dispatch override payload carries only container args, env,
and the timeout: there is no image field, so the versioned job name *is* part of the
pin (§8). Those settings now form the operator's pin for **new** jobs, and every
launch is built from the pin recorded in the job (issue #11,
`docs/ocr-v1-pinning.md`); the pre-launch template read-and-compare of that pin
(D43) is implemented there too. Resolving the real execution name is **not**
implemented and remains deployment evidence ([B2]).

## Cross-project bucket

The durable bucket may live in a different project than the service. `OCR_BUCKET_PROJECT`
names the bucket's own project and is passed to `create_gcs_ocr_store` in every
composition (`app.ocr_api`, `app.ocr_worker`, `app.ocr_reconcile`, `app.ocr_cleanup`);
it defaults to `OCR_PROJECT`, so a single-project deployment is unchanged. Only the store is
cross-project: dispatch and reconcile still address Cloud Run jobs in `OCR_PROJECT`
(§8), which is the service's project.

Provisioned by the project owner and read back on 2026-11-08 with account
`thanakrit.boonquarmdee@thaiunion.com`:

| Fact | Value |
|---|---|
| bucket | `gs://dev-ocr-service-data-asse1-1022222817897` |
| bucket project | `thaiunion-shared-sandbox` |
| service project | `thaiunion-ai-platform` |
| location | `ASIA-SOUTHEAST1` (region) |
| uniform bucket-level access | `true` |
| soft-delete retention | `0s` |
| public access prevention | `inherited` - left unchanged; bucket IAM and PAP are administrator actions here |

Evidence: `tests/test_ocr_gcs_integration.py` ran against that bucket with the
caller's ADC identity (`thanakrit.boonquarmdee@thaiunion.com`, quota project
`thaiunion-shared-sandbox`). It created one generated `idem/` metadata record,
exercised create-if-absent, generation-matched replace, and conditional delete,
then deleted it and read its unique prefix back as empty. No document content was
written and no bucket setting was changed. This is real bucket behaviour, not a
fake-client result; it says nothing about the Cloud Run runtime.

**Selected runtime identity, caller identity, and remaining blocker.** The
selected deployment runtime service account for the API, worker, reconcile, and
cleanup roles is `tu-ai-platform-sa@thaiunion-ai-platform.iam.gserviceaccount.com`.
The n8n caller identity is distinct and must never be conflated with the runtime
service account: n8n authenticates as
`tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com` (verified
`sub` `110367093326452748988`), mapped to its internal team in `OCR_TEAMS` as
`tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com|110367093326452748988`.
The bucket's IAM policy carries only `thaiunion-shared-sandbox` project-level
legacy bindings (`legacyBucketOwner` / `Reader`, `legacyObjectOwner` / `Reader`),
so the selected runtime service account has **no** object access to this bucket.
The attempted `roles/storage.objectAdmin` bucket grant was denied because the
development account lacks `storage.buckets.setIamPolicy`; read-back confirmed no
binding was added. An administrator must bind
`tu-ai-platform-sa@thaiunion-ai-platform.iam.gserviceaccount.com` before a
deployed execution can read or write durable state. Sharing one runtime service
account across the four OCR roles is an explicit development choice with less role
separation than a per-role design.

## Verification (offline only)

- `tests/test_ocr_store.py` — lease acquisition, live-holder refusal, expired
  takeover with `fence + 1`, conditional release, attempt counting/capping/spacing,
  dispatch-error recording, and job enumeration.
- `tests/test_ocr_dispatch.py` — the `jobs:run` request shape and its
  retryable/non-retryable/unacknowledged mapping, and that an acknowledgment does
  not finalize a job.
- `tests/test_ocr_worker.py` — the worker publishes a result the API serves; a
  duplicate launch writes nothing; a restart finishes the job; reconciliation
  repairs an undispatched job, a lost acknowledgment, and a non-retryable launch
  failure exactly once; a stale lease is taken over.
- `scripts/ocr_worker_demo.py` — the same lifecycle end to end in fake mode, and
  it fails if a cloud client module is imported.

These are fake-client tests. They are **not** evidence of real GCS concurrency,
the real Cloud Run Jobs runtime, or `jobs:run` IAM ([B1], [B2]).

## Historical pre-deployment blockers

**Historical state at implementation time.** The development account
`thanakrit.boonquarmdee@thaiunion.com` in project `thaiunion-ai-platform`
(region `asia-southeast1`, prefix `dev-ocr-service`) cannot perform the steps a
real private execution needs, and no unrelated existing service account, bucket,
or Artifact Registry repository may be reused. Observed limits:

- image build/upload to Artifact Registry is denied
  (`artifactregistry.repositories.uploadArtifacts`);
- no bucket can be created (needs `storage.buckets.create`);
- a new service account / project-level IAM bindings cannot be created
  (`iam.serviceAccounts.create`, `resourcemanager.projects.setIamPolicy`).

An administrator must provide, at minimum:

1. An Artifact Registry repository (or push permission) able to hold the worker
   image **by immutable digest**, with cleanup policies that do not delete a
   pinned digest.
2. A GCS bucket named `dev-ocr-service-*` with Object Versioning **disabled** and
   soft-delete retention **0** (contract §5.1, D17), plus the read-back evidence
   ([B4]). **Partly provided**: `gs://dev-ocr-service-data-asse1-1022222817897`
   exists in `thaiunion-shared-sandbox` with the required settings (see
   *Cross-project bucket*); what is still missing is object access for the runtime
   identities below.
3. For development, use the user-selected existing service account
   `tu-ai-platform-sa@thaiunion-ai-platform.iam.gserviceaccount.com` for the
   API/worker/reconcile/cleanup roles. An administrator must grant it bucket
   `roles/storage.objectAdmin` and the minimum Cloud Run Job permissions needed by
   the API (`run.jobs.get`, `run.jobs.runWithOverrides`), and verify deployer
   attachment (`roles/iam.serviceAccountUser`). Keep the n8n caller identity
   (`tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com|110367093326452748988`)
   separate as the `roles/run.invoker` caller mapped in `OCR_TEAMS`. A production
   deployment should revisit separate least-privilege runtime identities.
4. The Cloud Run service (private, no `allUsers`, IAM invoker only), one versioned
   Cloud Run Job per worker revision, and a Cloud Scheduler trigger for the
   reconcile and (later) cleanup jobs.
5. No work has been done against any cloud resource by this slice: no upload, no
   bucket, no IAM change, and no live `jobs:run` or n8n call.

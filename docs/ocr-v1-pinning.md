# OCR v1 — per-job engine and worker pinning, and the pre-launch template comparison (issue #11)

Status: implemented. This file records the pinning slice and its original offline evidence; current mutable image, revision, Job, IAM, and acceptance facts are authoritative only in `deploy/ocr-v1/sandbox.toml` and `verification/deployments/`. It implements `docs/ocr-v1-contracts.md` §8, §6.1 step 2,
§5.5's engine/checkpoint rules, §5.7's `job.json` pin fields, and D16/D18/D19/D43.
The deadline-scoped page timeout and the lease-heartbeat renewal that also carry
the #11 label are now implemented (`app/ocr_tracer.run_job`, `app/ocr_store`), with
the same single-object read-then-act limit as the pin check
(`docs/ocr-v1-dispatch.md`), and so is issue #5's ID-token verification
(`app/ocr_auth.py`).

## What changed

| Module | Role |
|---|---|
| `app/ocr_pinning.py` | the pin as a value object: image-digest normalization, the versioned job name, and the fail-closed rules |
| `app/ocr_config.py` | `settings.pin()`, `resolved_engine_ref()`, and the pinned args/env and `task_max_retries` |
| `app/ocr_store.py` | `job.json` now carries the whole §8 pin [D43] |
| `app/ocr_engine.py` | `engine_ref` as the adapter's declared identity, and `EngineRegistry` |
| `app/ocr_worker.py` | resolves its engine from the **job's** recorded ref through the injected registry |
| `app/ocr_dispatch.py` | launches the **recorded** pin, and `CloudRunJobTemplateGuard` reads and compares the template first |
| `app/ocr_cleanup.py` | the cleanup guard now takes the later of the deployment's attempt bound and the job's pinned `task_timeout_s` [D43] |
| `app/ocr_api.py`, `app/ocr_reconcile.py` | build the pin and the guard from the settings and refuse to start without a usable pin |

The synchronous expense app (`app.main`) is untouched.

## The pin (§8, D43)

Recorded once in `job.json` at acceptance and frozen for the job and all its
retries; afterwards the only CAS update is `state: accepted -> running` (§5.7).

| Field | Meaning | Enforced by |
|---|---|---|
| `engine_ref` | engine, models, and the parameters that produce output bytes; operator-only [D19] | the worker's registry lookup, and the engine's own declared `engine_ref` (§5.5/D16) |
| `worker_revision_digest` | the worker image by **immutable digest** | `normalize_image_digest`, then the template comparison |
| `worker_job_name` | the one Cloud Run Job for that revision | must be version-qualified by the digest's slug (§8, D18) |
| `worker_container_name`, `worker_args`, `worker_env`, `task_timeout_s`, `task_max_retries` | what the launch sends and compares | the template comparison, and the override body |

**Normalization, not trust.** `sha256:<64 hex>` is the only accepted image form,
bare or after an `@` in a full `repo@sha256:...` reference. A tag - even a
plausible `worker:v1` - fails closed, because a tag is mutable and the pin's whole
job is to name exact bytes. `engine_ref` is empty-or-exact: padding is refused.

**One job per revision.** An empty `OCR_WORKER_JOB_NAME` becomes
`dev-ocr-service-worker-<first 12 hex of the digest>`; an operator-supplied name
may keep any prefix but must still end in that slug. A single shared job name -
the shape that lets one job be re-pointed under an accepted job - is refused when
the pin is built, so a drifted deployment fails at startup instead of accepting
jobs it cannot pin.

**`engine_ref` when unset** is derived from the settings that change output bytes
(`tesseract:<binary>:<lang>:<raster dpi>dpi`). An operator who changes the language
or the resolution cannot silently keep writing checkpoints that claim the old
configuration; setting `OCR_ENGINE_REF` names a specific model or version instead.
The worker image digest pins in-image models only: when `OCR_ENGINE_TESSDATA_PREFIX`
points Tesseract at an external model directory, an explicit non-empty
`OCR_ENGINE_REF` is required at startup so two external model directories cannot
silently share the derived in-image identity. `OCR_TASK_MAX_RETRIES` defaults to
`0` (`0` means Cloud Run tasks run once per launched execution) and is pinned and
compared like every other template field, so retry policy is owned by the
application's bounded dispatch attempts rather than uncontrolled platform retries.

## The launch is built from the record, never from the current setting

`CloudRunJobsDispatcher` holds the operator's pin for **new** jobs, and uses it
for exactly one thing: the tracer copies it into `job.json`. Every launch -
including a reconciliation re-dispatch of a pending or retried job - builds its
request from the pin in that record:

- the URL is the recorded versioned job name;
- the override body carries the recorded args, env, and timeout;
- the override body carries **no image**: there is no `image` field in
  `overrides`, so the versioned job name *is* the image pin (§6.1).

So an operator who moves the deployment to a new engine or revision reaches only
newly accepted jobs, and a pending or retried job keeps running what it was
accepted under. That is what `tests/test_ocr_dispatch.py`
(`..._posts_the_recorded_pins_override_body`, with a different pin in the
dispatcher) and `tests/test_ocr_worker.py`
(`the_worker_runs_the_engine_the_accepted_job_was_pinned_to`,
`a_job_accepted_after_the_operator_change_runs_the_new_engine`) demonstrate with
two distinguishable fake revisions.

**No caller selection, and no fallback.** Submission takes a document and an
idempotency key; `engine_ref` and the revision are not readable from, and not
settable through, the API (D19). Nothing anywhere falls back to the deployment's
current engine: an unregistered `engine_ref`, an engine that declares a different
ref, and a recorded pin that does not normalize are all refusals.

## The worker resolves the engine it was pinned to (§5.5, D16)

`run_worker` takes an injected `EngineRegistry` (dev/test code passes a fake
engine under its own declared ref) and resolves `engines.build(job.engine_ref)`
**after** reading the job and **before** taking a lease. A resolution that fails -
unknown ref, or a builder answering with a differently declared engine - writes
nothing at all, not even `claim.json`, and exits non-zero. Page checkpoints are
validated against the job's recorded `engine_ref` (D41); a checkpoint written by
another engine is treated as absent and its page is recomputed.

A worker image is per-revision, so a deployment usually registers one ref. To
still serve a job accepted under a superseded revision, that revision's ref must
stay registered (see *Preserving* below): the registry is the operator's list of
the revisions this deployment can still honour, not a lookup of "whatever is
current".

## The pre-launch template comparison, and the window it cannot close (D43)

`CloudRunJobTemplateGuard` is injected into the dispatcher and runs immediately
before the launch call: it reads
`GET /v2/projects/{p}/locations/{r}/jobs/{name}` for the **recorded** job name and
compares the template with the pin - `containers[0].image` (normalized), the
pinned container name, `timeoutSeconds`, `maxRetries`, and the pinned args/env
(equal **or absent**, since they travel as overrides).

A mismatch, a missing job, a denied read, an unreadable body, a shape that does
not carry the compared fields, or a transport failure is
`LaunchNotRetryable` -> the job records `engine_unavailable` and reconciliation
finalizes it `failed`/`engine_unavailable` with an alert (§6.1 steps 2 and 5).
Nothing is launched from an unverified template, and no read happens at all when
the recorded pin itself is unusable.

**Read-then-act, stated plainly.** The comparison is a read followed by a
separate call, so a job re-pointed *between* them still runs the wrong template:
the wrong executable, or a wider platform-retry bound. Cloud Run exposes no
conditional launch here - the `jobs:run` request accepts an `etag`, but its
run-time failure semantics are undocumented and are not assumed [B2]. The residual
is bounded: the attempt timeout travels as an override from the pin, the cleanup
guard takes the later of the deployment's bound and the pin's `task_timeout_s`
(so a job accepted under a longer bound is not guarded by a shorter one), and the
published result, access, and erasure are unaffected. Detecting drift is what this
slice delivers; preventing it is a deployment/IAM artifact [B2].

**Unverified cloud assumptions in the guard** (no call was made): the job
resource's `template.template.{containers,timeout,maxRetries}` field names and
their string encodings; that the API service account holds `run.jobs.get` as well
as `run.jobs.runWithOverrides`; and that the versioned job is protected from
in-place update or deletion. The guard is therefore unit-tested against the
documented JSON shape with a fake session, and is **not** cloud evidence.

## Preserving a superseded revision, and rollback

A job's pin is frozen, so retiring a revision is not instantaneous - it has to
outlive every job still pinned to it. For **at least** the lifetime of the
longest-lived job on that revision (`deadline_at` + retention, i.e. up to 24 h +
per-attempt bound) a deployment must keep:

1. the versioned Cloud Run **Job** `dev-ocr-service-worker-<slug>`, not deleted
   and not re-pointed (its template is what the guard compares);
2. its **image digest** and its layer blobs in Artifact Registry (no cleanup
   policy may delete a pinned digest);
3. the **engine** that ref names: the adapter binary, its models, and the settings
   it was built from, inside that image;
4. the **registry entry** for that `engine_ref` in the worker's composition, or
   the revision's workers cannot resolve it.

None of this is automated, and no operator tooling for it was added here: it is a
deployment runbook item, consistent with §8's statement that re-executability is
an operational invariant rather than a platform guarantee.

**Rollback** therefore has two distinct forms, and only one of them is immediate:

- *Rollback for new work* is a settings change: point `OCR_WORKER_REVISION_DIGEST`,
  `OCR_ENGINE_REF`, and the engine settings at the previous, still-registered
  revision. New jobs are pinned to it at once; already-accepted jobs are
  unaffected, which is the property that makes the rollback safe.
- *A job already accepted on a revoked revision cannot be re-pinned* - that is the
  point of the pin. If its revision is deleted or re-pointed, dispatch fails
  closed and reconciliation finalizes that job `failed`/`engine_unavailable`; if
  its engine is simply unregistered, its worker writes nothing and the job ends at
  its deadline as `deadline_exceeded`. Nothing is silently recognized by the newer
  engine, and no partial output is published under the wrong pin.

## Verification

Offline only (fakes, no binary executed, no cloud client constructed):

- `tests/test_ocr_pinning.py` — 32 tests: digest normalization and every rejected
  image form, the derived versioned name, a non-version-qualified name refused,
  half-configured pins refused, the pin read back from a record, the registry's
  ref-keyed resolution, an unknown ref refused with no fallback, and a builder
  whose engine declares another ref refused.
- `tests/test_ocr_dispatch.py` — 22 tests: the read-then-launch request sequence
  and body from the recorded pin, six drift cases refused with no launch, absent
  pinned args/env accepted, unreadable/unparsable/denied/missing templates failing
  closed, an unusable recorded pin refused before any read, and the
  non-retryable outcome recorded for reconciliation.
- `tests/test_ocr_worker.py` — the accepted job running the engine of its own ref
  while the deployment has moved on, a new job running the new engine, an
  unresolvable pin refused with no write, a mis-declared engine refused, and a
  foreign-engine checkpoint recomputed.
- `tests/test_ocr_tracer.py` — the whole pin recorded at acceptance and exposed to
  no caller, an operator change reaching new jobs only, and a submission carrying
  pin fields changing nothing.
- `tests/test_ocr_bucket_project.py` — a composition refusing to start without a
  usable pin.
- `tests/test_ocr_lifecycle.py` — a job accepted under a longer pinned attempt
  bound guarded by that bound rather than by the current setting.

At this slice's commit: `uv run pytest` → **297 passed, 8 skipped**;
`uv run ruff check .` → clean; `uv lock --check` → current;
`scripts/ocr_tracer_demo.py` and `scripts/ocr_worker_demo.py` → exit 0 with no
cloud module imported.

The pin, registry, and guard tests were checked against deliberate mutations
(always-match template comparison, registry fallback to any engine, the
version-qualification check removed): each mutation fails the tests written for it.

## Explicit non-claims

- No cloud call, upload, deployment, paid run, IAM change, or bucket write was
  made by this slice, and no Cloud Run job was read.
- The real Tesseract adapter is still the **DEV** engine and no second production
  engine was added: the "two engines" in these tests are fakes with different refs.
- The Cloud Run resource shape, the `run.jobs.get` permission, and the
  read-then-act window are unverified cloud assumptions [B2]; fake-session success
  is not cloud evidence.
- The deployment numbers stay benchmark-gated ([B3]): `page_timeout` as a named
  numeric limit, and any backoff, are unapproved. Issue #2's B1/B2/B4 evidence
  blockers stay open.

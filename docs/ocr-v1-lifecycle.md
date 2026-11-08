# OCR v1 — deadlines, expiry, and monitored cleanup (issue #9)

Status: implemented and covered by offline tests at the approved seams; **no cloud
behaviour is verified**. This slice implements `docs/ocr-v1-contracts.md` §3.3,
§5.5, §6.2, §6.3, §10 and D17, D26, D32, D37, D39, D42, D44 in the parts named
here, with the injected clock, storage fake, and fake engine only. Real GCS
soft-delete/versioning behaviour, the deployed cleanup job, its Cloud Scheduler
trigger, and its alert wiring are **deployment evidence, not code** ([B1], [B4]),
so nothing here is a claim about a deployed service, and no cloud call is made.

## What changed

| Module | Role |
|---|---|
| `app/ocr_cleanup.py` | the cleanup pass, the derived guard, the D32 target invariant, and the standalone `python -m app.ocr_cleanup` entrypoint |
| `app/ocr_reconcile.py` | deadline finalization now credits durable page checkpoints; also completes a full-success input deletion the worker left behind |
| `app/ocr_tracer.py` | the expiry gate reads records only, the result is loaded after the gate, and a full success deletes its input at once |
| `app/ocr_store.py` | late-write fences on `job.json`/page checkpoints, `get_job_meta`/`read_result`, `read_page_checkpoints`, `list_job_ids`, `purge_job` |
| `app/ocr_config.py` | `OCR_CLEANUP_SCHEDULE_PERIOD_S`, for the D32 startup invariant |
| `scripts/ocr_tracer_demo.py` | the same lifecycle end to end in fake mode |

The synchronous expense app (`app.main`) is untouched.

## Lifetime and the deadline (unchanged rules, now completed)

`accepted_at` starts a 24 h lifetime (`deadline_at = accepted_at + job_lifetime`)
and the deadline behaviour of issue #8 is unchanged: an attempt reaching a job at
or after `deadline_at` writes **nothing**, page work stops at the deadline, and no
finalize at or after it can be `succeeded` (D42).

What changed is what reconciliation accounts for:

- **Credit for durable pages.** The reconciler has no engine, so it cannot
  enumerate a document's pages. It now loads the durable succeeded page
  checkpoints (`pages/*.json`, validated against the job and its pin, D41) and
  finalizes with them: `partially_succeeded` when at least one page is durable,
  `failed` otherwise, always `reason: deadline_exceeded`.
- **Limit, stated plainly.** A page that failed, or that no attempt ever reached,
  leaves no checkpoint, so it cannot appear in a reconciliation-time result. The
  reported `page_count` is therefore the number of pages this path can account
  for, and `pages_total` stays unset rather than being invented. A deadline
  finalization reached by a worker that was actually running still reports every
  source page with the failed ones marked. Resuming page work at the deadline
  remains out of scope: reconciliation never recognizes anything.
- Reconciliation also completes the §6.3 prompt deletion (below) for a full
  success whose finalizing worker died first.

## Fencing late writes (D42, §5.5)

Every write that could revive a terminated job is refused at the storage seam:

- a page checkpoint at or after `deadline_at`, or after `final.json` exists, is not
  written (and the worker's own page loop already stops at the deadline);
- `job.json` is no longer flipped `accepted` → `running` for a finalized job or at
  or after its deadline, so a straggler cannot make a terminated job reappear as
  running;
- `final.json` remains the single arbiter: a losing finalize gets `412`, and an
  attempt arriving after finalization writes nothing and exits non-zero.

The residual is unchanged and disclosed by the contract: two executions of one job
are at-least-once, and staleness is made harmless by create-if-absent checkpoints,
the arbiter, and the cleanup guard — not by cross-object fencing (§5.3).

## Expiry and prompt deletion (§3.3, §6.3, D39)

- `expires_at = finalized_at + 24 h`, written once in `final.json`; it is the only
  expiry authority. Access ends there **whether or not cleanup has run**: the gate
  reads `job.json` and `final.json` and nothing else, so a read arriving while a
  cleanup pass is part-way through is denied `410 job_expired` instead of failing on
  content that pass already deleted. Once cleanup removes `job.json`, the same
  lookup is `404 not_found` (D3) — never a foreign team's data, and never `403`.
- Inside the retention window a `final.json` whose published result is missing is
  corruption, not expiry: `500 internal_error` plus an alert, and no content is
  ever served from it.
- Deduplication ends at the same instant (§3.4/D44): a same-key submission at or
  after `expires_at` is accepted as a **new** job, and the record is replaced by CAS.
- A **full success** deletes its input immediately, from the `final.json` winner
  only, so a partial or failed job keeps its input until cleanup. A same-key retry
  after a success writes nothing and does not re-upload it, so retention is not
  extended. A momentary failure is retried by reconciliation.

## Cleanup (§6.3, D26, D37)

`app/ocr_cleanup.cleanup(store=…, clock=…)` is one pass:

1. enumerate every job (`(team_id, job_id)`, `job.json` names only);
2. read that job's records; a record that cannot be read is **skipped and alerted**
   (`skipped_unreadable:`) rather than deleted on an undecidable expiry;
3. refuse the job until `cleanup_guard = max(expiry_bound, deadline_at +
   attempt_bound)`, where `expiry_bound` is `final.json.expires_at` when it exists
   and `deadline_at` otherwise (D39), and `attempt_bound` is the later of the
   deployment's `per_attempt_timeout` and the job's own pinned `task_timeout_s`
   (D43: the launch sends the pinned value as its override);
4. report `overdue:` when the job's `expires_at + 1 h` has passed and its records
   are still present, then delete anyway;
5. delete content before records, every delete conditional on the generation read:

```
inputs/{team}/{job}                     ← then
jobs/{team}/{job}/pages/*.json          ← every checkpoint
jobs/{team}/{job}/result.*.json         ← including results that never won final.json
jobs/{team}/{job}/claim.json            ← a crashed holder's lease
jobs/{team}/{job}/dispatch.json
jobs/{team}/{job}/final.json
jobs/{team}/{job}/job.json              ← last among the job's objects
idem/{team}/{key_hash}                  ← last of all
```

The `idem` delete additionally requires that the record still names this job, so a
record a later submission reused for a **new** job survives (D44): a name-addressed
delete would silently lose deduplication for the live job. A delete whose
precondition fails is skipped, not forced: the pass reports `cleanup_incomplete:`
and the next pass re-decides. A pass that finishes reports `cleaned:`. Enumeration
is what makes this correct — a fixed deletion list would leak a second
`result.{fence}.json` or a lease a crashed holder left behind.

**Target and guard arithmetic.** `guard - expires_at = max(0, attempt_bound -
(finalized_at - accepted_at))`, so the guard exceeds `expires_at` only for a job
finalized within one attempt bound of acceptance — the worst case being a job
finalized the instant it was accepted. `attempt_bound` is the deployment's
`per_attempt_timeout`, or a longer bound when the job's own pin is longer (D43);
the D32 arithmetic below is about the deployment's own value. With the D32 invariant
`per_attempt_timeout + cleanup_schedule_period ≤ 1 h` (provisional 30 + 15 min,
asserted at startup by `assert_cleanup_invariants` and in the tests), the first
legal pass starts within one hour of `expires_at` by construction. The residual is
scheduler and execution latency, which the `overdue:` action and its log count
(`ocr_cleanup actions=N overdue=M`, allowlisted fields only [D20]) are there to
surface. **No exact-second erasure is claimed**: the deletion here removes the live
objects, and what else might have retained a copy is bucket configuration (below).

## Entrypoint and deployment configuration (env-gated, no cloud call)

One more role on the existing image — `python -m app.ocr_cleanup`, run as Cloud Run
Job `dev-ocr-service-cleanup` on a Cloud Scheduler trigger. No live scheduler is
created by this slice, and the entrypoint makes no call until it is deployed.

Environment: `OCR_PROJECT`, `OCR_BUCKET`, `OCR_REGION`, `OCR_TASK_TIMEOUT_S` (the
pinned per-attempt timeout that bounds the guard), and
`OCR_CLEANUP_SCHEDULE_PERIOD_S` (the trigger's own period, for the D32 assertion).

Deployment evidence still required, and not claimed by any fake-mode test:

- the bucket's read-back ([B4]): soft-delete retention `0`, Object Versioning
  **disabled**, no Bucket/Object Retention Lock, no replication or backup that
  retains the data, and no lifecycle rule used as the deletion mechanism (a
  lifecycle rule only takes effect within up to 24 h and cannot meet the target);
- the Scheduler trigger, its period, and alerting on the `overdue:` /
  `cleanup_incomplete:` / `skipped_unreadable:` lines;
- operational-log retention and its own policy ([B4] / [D20]);
- the real GCS preconditions the protocol assumes ([B1]), including that a
  conditional delete against an object replaced since the read fails `412`.

Provisioning remains blocked exactly as `docs/ocr-v1-dispatch.md` describes; this
slice created no cloud resource, uploaded nothing, and made no cloud call.

## Verification (offline only)

- `tests/test_ocr_lifecycle.py` — the late-write fences (S2); the expiry gate
  denying mid-cleanup and failing closed on a missing result inside the window
  (S1); prompt input deletion and its reconciliation repair; deadline finalization
  from durable checkpoints; the guard, the D32 invariant and the one-hour
  arithmetic; the delete order at real generations; idempotence and a partial pass
  completed by the next; the overdue signal; the reused-key record surviving; and a
  straggler creating nothing after cleanup.
- `tests/test_ocr_store.py`, `tests/test_ocr_worker.py`, `tests/test_ocr_recovery.py`
  — the existing store, worker, reconciliation, and recovery contracts, updated
  only where the storage seam gained its `now` parameter.
- `scripts/ocr_tracer_demo.py` — the same paths end to end in fake mode, and it
  fails if a cloud client module is imported.

These are fake-client tests. They are **not** evidence of real GCS conditional
deletion, soft-delete or versioning behaviour, the real engine, the deployed
cleanup job, or its scheduler ([B1], [B3], [B4]).

## Not implemented in this slice

Named so they are not mistaken for done: the capacity admission set (§6.4) and its
B6 residual; numeric limits other than the provisional D21/D32 pair ([B3]); the
Cloud Scheduler triggers themselves, the bucket policy read-back, and the alert
channels ([B2], [B4]); lease-heartbeat renewal (#11); and resuming page work from
reconciliation, which this slice deliberately does not do. The D43 pre-launch
template comparison and real engine/worker pinning (#11) are now merged and stated
in `docs/ocr-v1-pinning.md`.

# OCR v1 — durable submission and idempotency (issue #4)

Status: implemented and covered by offline tests at the approved seams; **real GCS
behaviour is unverified** and the cloud integration test is blocked (see below).
It implements the approved protocol in `docs/ocr-v1-contracts.md` §3.3–§5.4 only
for the parts listed here; nothing in this document is a claim about deployed
behaviour.

## What is durable now

The tracer app (`app/ocr_tracer.py`) persists everything through
`app.ocr_store.DurableOcrStore`, which takes an injected object store. Job state
survives the API process: a second app built over the same objects serves the
same job and a worker continues from the stored input.

Object layout (one bucket, the §5.7 subset this slice writes):

```
idem/{team_id}/{key_hash}                  create-once claim (job id, content identity, claimed_at)
jobs/{team_id}/{job_id}/job.json           create-once identity, pins, accepted_at, deadline_at, state
jobs/{team_id}/{job_id}/dispatch.json      create-once launch intent; CAS-updated acknowledgment
jobs/{team_id}/{job_id}/result.{fence}.json create-once result
jobs/{team_id}/{job_id}/final.json         create-once arbiter: state, reason, finalized_at, expires_at
inputs/{team_id}/{job_id}                  create-once input bytes
```

Every write is one conditional single-object write with a real generation
(`ifGenerationMatch`), except creates, which use `0`. **No cross-object
transaction is claimed.** A crash between two writes leaves either an
unpublished result (readers ignore it) or a missing dispatch intent (a same-key
retry repairs it).

## Idempotency behaviour

- Key scope is `(team_id, Idempotency-Key)`; content identity is
  `(SHA-256(bytes), byte_length)`.
- Same team, same key, same content, record live: `200` with the existing job
  and no second job, dispatch, or recognition.
- Same team, same key, different content: `409 idempotency_conflict`, existing
  job untouched.
- Different teams or different keys are independent.
- Claim replacement and deletion are conditional on both the generation read and
  the job id the record still names, so a stale actor cannot replace or delete a
  live record.
- A claim whose `job.json` is absent is a crash-window retry while
  `now - claimed_at < 5 minutes`: the retry materializes at the recorded job id
  and answers `200`, never naming a job that does not exist. Beyond that window
  the record is stale, is replaced by CAS, and a new job is accepted — the old
  job is not resurrected.
- A failed input write answers `503 storage_unavailable` with `Retry-After: 5`
  and compensates only the claim creator's own record and objects.

## Deduplication window

A record is live for exactly as long as its job is served by the read gate:

- non-final: until `deadline_at` (24 h from acceptance);
- final: until `expires_at = finalized_at + 24 h`.

**After that instant the key no longer guarantees deduplication.** A later
submission with the same key (and even the same bytes) is accepted as a new job,
because the old record is expired or has been cleaned up. Callers must not rely
on a key older than the retention window; retry with a fresh key, or accept that
a new job may be created.

## Not implemented in this slice

Named so they are not mistaken for done. Worker lease/fencing, Cloud Run dispatch,
and reconciliation are now added by issue #6 (`docs/ocr-v1-dispatch.md`): a
fenced `claim.json` lease, a `jobs:run` dispatcher adapter, and a reconcile
entrypoint exist. The D43 template pin comparison is now added by issue #11
(`docs/ocr-v1-pinning.md`), and global capacity admission
(`capacity/inflight.json`) by issue #10 (`docs/ocr-v1-admission.md`). Per-page
checkpoints, bounded page retries, and partial finalization are now added by issue #8
(`docs/ocr-v1-recovery.md`); deadlines, expiry, prompt input deletion, and the
physical cleanup pass are now added by issue #9 (`docs/ocr-v1-lifecycle.md`).
The retention semantics above describe the read gate, which denies at
`expires_at` independently of that pass.

## Verification

Offline only:

- `tests/test_ocr_store.py` — the storage contract: create-once claims,
  conditional replace/delete, create-once job/input, `final.json` arbitration,
  an unpublished result being invisible, corrupt records failing closed.
- `tests/test_ocr_tracer.py` (durable section) — the public lifecycle: two
  simultaneous same-key submissions, same key with different content, restart
  recovery, crash between claim and job, beyond-grace replacement, lost dispatch
  intent repair, an interrupted write before `final.json`, `503` fail-closed, and
  creator-scoped compensation.
- `tests/test_gcs_objects.py` — the GCS adapter's error and generation mapping
  against a fake bucket.

These use deterministic in-memory objects with generations and 412s. **They are
not evidence of real GCS concurrency behaviour.**

Blocked: `tests/test_ocr_gcs_integration.py` is skipped until an administrator
provisions a bucket named `dev-ocr-service-*` in `thaiunion-ai-platform` and
grants object-level permissions for the test identity. The current account cannot
create a bucket and no unrelated bucket may be reused, so no live object call has
been made.

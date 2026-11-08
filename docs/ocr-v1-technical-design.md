> **SUPERSEDED — do not implement from this file.** rev 6 replaced it with
> `docs/ocr-v1-contracts.md`, which governs wherever the two disagree; §0 there lists the
> contradictions resolved. This rev 5 text is kept only for its revision log, its reference notes,
> and the audit trail of what was corrected. Three of its facts are stale: its header names the
> corporate target `thaiunion-ai-platform` although the approved development target is the personal
> project `proud-quasar-351116`; the draft rules and tests it points at are deliberately uncommitted;
> and its blocker/approval status is superseded by rev 6 §1.1 and §13.

# OCR v1 — technical design (rev 5, SUPERSEDED draft)

Issue: `TU-thanakrit/dev-service-google-doc-ai` #1 *OCR v1: Freeze contracts and recovery design*
Base branch `integration/ocr-service-v1`; target project `thaiunion-ai-platform`, region `asia-southeast1`,
new-resource prefix `dev-ocr-service`.

## Provenance of this document (read first)

| Label | Meaning |
|---|---|
| **[CONFIRMED]** | Explicitly confirmed by the user. |
| **[DIRECTION]** | The user approved rev 2 **as a design direction**, and §10 seams **S1–S3**. Approval of a direction is not verification of the protocol. |
| **[DEFAULT]** | This design chooses the minimal concrete option so implementation can proceed. It is **not** user-approved and **not** verified. Veto early if wrong; no rework is hidden behind it. |
| **[BLOCKED]** | Cannot be decided from documents alone. Requires real GCP evidence, credentials, a benchmark, or explicit approval. Stated as blocked, never as verified. |

Claims marked *(ref:*N*)* restate an official page in §11. Anything else is a proposal, including every
**[DEFAULT]**.

**What is confirmed today.** [CONFIRMED] a duplicate submission retry (same team, same idempotency key,
same content) must not start duplicate processing; [CONFIRMED] no cancellation endpoint in v1 (caller or
operator); [CONFIRMED] `404` after job cleanup is acceptable, so no stub is retained solely to keep a
`410` stable. [DIRECTION] the rev 2 design direction and seams S1–S3.

**What this revision does not claim.** Nothing here has been exercised against real GCS, real IAM, or a
real engine. No resource exists. No paid processing, upload, provisioning, commit, or push was performed.
§13 lists every remaining blocker; everything else in this document is decidable now. It also does **not**
claim single execution per job (only one logical job per key, at-least-once execution, and at most one
published result, §4.1), in-band enforcement of the worker image digest by the container itself (§8), or an
early classification of an execution that never starts (§4.1).

### Revision log

- **rev 5 (this revision) — resolves the two BLOCKERs rev 4 left open: worker-lifetime cleanup and global
  capacity.** No new capability; both are corrections of rev 4's own arithmetic and its own fail-open escape.
  1. §4.3 — the worker-lifetime guard was `deadline_at + (max_retries + 1) × per_attempt_timeout`, which
     exceeds `expires_at + 1 h` for jobs that finalize early, so the approved ≤1 h cleanup target was not
     met. A new worker rule (an attempt that starts at or after `deadline_at` writes nothing) bounds a
     straggler to one per-attempt timeout, so the guard becomes
     `max(expires_at, deadline_at + per_attempt_timeout)`, and the configuration invariant
     `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` (D32) makes it ≤ `expires_at + 1 h` by
     construction. The rev 4 sentence that said the guard "bites only for jobs that finalize within ~90 min
     of their deadline" was backwards and is corrected.
  2. §4.4 — the admission counter failed **open** on CAS contention, so the ceiling was unbounded under
     load, contradicting the approved global capacity bound. Fail-open is withdrawn: a submission that
     cannot record its admission is rejected `429` (fail closed), release happens exactly once (by the
     `final.json` winner), and repair recomputes the count exactly instead of tolerating 10 % drift. The
     residual is disclosed: a single-object counter's throughput bounds the submission rate, and a repair
     racing an un-persisted acquisition can transiently under-count by the number of acquisitions in flight.
- **rev 4 — protocol corrections from an independent review of rev 3.** No new capability.
  Seven defects were reproduced against rev 3 by line reference. Every fix is a conditional write, a
  recovery rule, a lifecycle guard, or the withdrawal of a claim; nothing here introduces cross-object
  atomicity, and two fixes *remove* a guarantee rather than add one.
  1. §5.3 — a crashed holder's `claim.json` could not be replaced: acquisition was create-if-absent only, so
     every later worker got `412` and the job could not resume at all. Lease **takeover by CAS-replace**
     after expiry is now specified, with the stop rule for a superseded holder.
  2. §4.1/§4.2 — `dispatch.acknowledged` gated re-dispatch, but an acknowledged `jobs:run` operation can
     still fail, or never start an execution, so an acknowledged-but-dead job was stranded until
     `deadline_at`. `acknowledged` is now diagnostic only (D31); the guard is the lease check, the attempt
     cap, and `now < deadline_at`, and the residual is stated as at-least-once execution. §9 case 4, which
     contradicted the §4.2 table, is rewritten.
  3. §5.2 — the retry path wrote the input with create-if-absent and treated any failure as
     `storage_unavailable`, whose compensation deletes `job.json` **and the `idem/*` record** — objects
     that, on the retry path, belong to another actor. Precondition `412` is now explicitly "already
     present", and compensation is restricted to the claim owner's own objects (D29). The same write also
     re-uploaded the input of an **already-final** job after that input had been deleted on full success;
     the `200` path now skips materialization and dispatch entirely for a final job.
  4. §4.2/§4.3 — cleanup deleted the `idem/*` record by name with no precondition, so a record replaced by
     a post-expiry submission could be deleted by the previous job's cleanup pass, silently losing
     deduplication. Every delete is now generation-conditional and job objects are deleted by prefix
     enumeration (D26).
  5. §4.3/§5.4 — nothing stopped a straggler from re-creating `pages/*`, `result.*.json` or `final.json`
     after cleanup removed the final marker, and `claim.json` was never deleted at all. Cleanup now waits
     for a derived worker-lifetime guard and deletes by enumeration.
  6. §8 — image pinning was an operational invariant with no dispatch-time check, so a re-pointed
     version-named job would silently run a different image. Dispatch now verifies the job's template image
     against `worker_revision_digest` before `jobs:run`, and states that a container cannot verify its own
     image from the runtime contract.
  7. §4.1 — the guarantee statement is now exact: one logical job per idempotency key, at-least-once
     execution, at most one published result. Rev 3 already said this in one paragraph, but the
     `attempts < 2` / `acknowledged` framing around it read as if executions were bounded by
     acknowledgment.
- **rev 3 — decisions, false-fact fixes, and honesty limits.** No new capability.
  1. Removed the `410`/`404` self-contradiction (§2.4, §7): the design now states one rule and the
     user's accepted consequence.
  2. Replaced internal contradictions in the fence story: rev 1/rev 2 claimed a stale worker "cannot
     write" and that "the fence bump rejects the straggler". Neither is true across the lease-expiry
     window. §5.3 now states exactly what the CAS guarantees and what it does not, and §9 no longer
     claims mutual exclusion.
  3. Found and fixed a real rev 2 cross-object hole: the idempotency record could name a job that was
     never materialized (crash between claim and persist), so a retry could return `200` for a job that
     does not exist. §5.2 now specifies claim-then-materialize with retry-path repair.
  4. Fixed dispatch-failure dishonesties: a failed launch was described as leaving state `queued` that
     may never be written, and duplicate dispatch was implied to be impossible. §4.1 specifies a
     dispatch-intent object and an at-most-2-attempts rule, and names the residual window.
  5. Removed the false "ownership and expiry must never be derived from a record that cleanup may
     delete" sentence, which contradicted the `410` gate it sat next to (§2.4).
  6. `Idempotency-Key` is stated as a **[DEFAULT]**, not as something the user approved (§2.2, §12/D5).
     The user approved the *behavior*, not the header requirement.
  7. Dropped the `queued` state [DEFAULT]: it is not required by the spec or the issue, its only
     producer was a conditional write that may never happen, and its information is already carried by
     `dispatch.acknowledged` (§3, §2.5).
  8. Merged `meta.json` + `state.json` into one `job.json` [DEFAULT]: removes one crash window and one
     repair rule (§5.7, §5.5).
  9. Every previously open choice is now either a concrete **[DEFAULT]** (§12) or **[BLOCKED]** (§13).
     The former §12 "unresolved" list is gone; §13 is the only open list.
  10. Revision notes cleaned: rev 2's own log mixed "unapproved" with "approved direction" and is
      condensed to one line below. Its verified corrections are kept, since they were correct.
  11. Closed a second cross-object hole rev 1/rev 2 both carried: a job whose `job.json` existed but whose
      input object was never written could be dispatched, which the spec forbids ("invalid input does not
      become an unexplained processing failure"). §5.2 now defines a job as usable only with both objects,
      with compensating removal on failure and a reconciliation rule; §9/16 and the §5.5 crash table cover
      it.
  12. Reference numbering corrected (Object Versioning is ref 12, Cloud Run autoscaling ref 13), and
      `claim.json` added to the bucket layout, which had omitted it while §5.3 relied on it.
- **rev 2** — independent safety/feasibility review. Correctly: removed the false
  `containerOverrides.image` claim; corrected the idempotency/dispatch `202`/`503` contradiction;
  established that `ifGenerationMatch=0` also tolerates noncurrent versions; disclosed that soft delete
  also applies to overwrites and that disabling it is not erasure. Its fencing "correction" replaced one
  wrong statement with another (see rev 3 item 2) and is superseded.
- **rev 1** — initial draft. Superseded.

---

## 1. Scope of this design

Fixed by the approved v1 spec: OCR-only; internal multi-team; n8n first; printed Thai/English;
PDF/JPEG/PNG/TIFF; async Cloud Run API + Cloud Run Jobs workers; GCS-only application state; GCP identity
auth; no classification or structured extraction; shared capacity with per-team data isolation; one
operator-pinned engine per job; 24h maximum lifetime from acceptance; delete inputs on full success;
expire other data one day after finalization; block API access at expiry.

This document freezes: routes, statuses, error vocabulary, result shape, page rules, job states and
timestamps, the dispatch/reconcile/cleanup arrangement, the GCS conditional-write protocol, the
verified-identity path, version pinning, and the seams to test at. Out of scope: everything in the
spec's "Out of Scope" (no callbacks, no direct-to-GCS upload, no caller engine selection, no batching,
no cancellation).

**Cancellation is absent by decision.** [CONFIRMED] There is no caller or operator cancellation route in
v1, and no `DELETE /v1/ocr/jobs/{id}`. The Cloud Run executions resource does expose `cancel` *(ref:*6*)*,
but the design does not call it. Runaway work is bounded only by `deadline_at`, the per-attempt task
timeout, and the `≤2` dispatch attempts (§4.1). This is a deliberate limitation, not an oversight.

### Issue #1 acceptance-criteria map

| AC (issue #1) | Where |
|---|---|
| Versioned request/result examples, routes, statuses, error codes, page numbering, text source, warnings, partial completeness, expired-result behavior, execution-vs-accuracy | §2.1–§2.9 |
| Job transitions, one authoritative final-state timestamp, acceptance/deadline/expiry timestamps, idempotency conflict rules, request/cleanup races | §3, §5.2, §9 |
| Concrete dispatch/reconciliation/cleanup arrangement; launch ack vs completion | §4 |
| GCS conditional-write, claim/lease/fencing, checkpoint/result publication, idempotency, crash recovery, no cross-object transactions | §5 |
| Verified GCP caller→team mapping; headers must not grant identity | §6 |
| Engine/model/config/worker-revision pinning and executability across deployments | §8 |
| Race/failure review with examples; approval of technical and testing choices; numeric limits gated by benchmark | §9, §10, §13 |
| Align project instructions with the async OCR-only scope during approved implementation | §12/D27 |

---

## 2. Public contract

### 2.1 Routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/ocr/jobs` | Submit one document; return job identity |
| `GET` | `/v1/ocr/jobs/{job_id}` | Status (state, timestamps, dispatch, opportunistic progress) |
| `GET` | `/v1/ocr/jobs/{job_id}/result` | Final result, or the pending/expired signal |
| `GET` | `/health` | Liveness. No cloud client is initialized; no identity check. Still unreachable without a valid ID token because the service is not public (§6.1) |

No update, no cancel, no delete, no list route in v1.

**The legacy synchronous route** `POST /v1/documents:process` is retired as part of the OCR-only
replacement described by the spec. [DEFAULT D27] That retirement, and the `AGENTS.md` / README /
five-template-contract update (issue AC #8), happen inside the **approved implementation**, not in this
design ticket. Until then the existing synchronous code and its tests stand unchanged, and this document
must not be read as evidence that the routes below exist.

### 2.2 Submission request

`multipart/form-data`, one `file` part plus headers. The part's `Content-Type` must be
`application/pdf`, `image/jpeg`, `image/png`, or `image/tiff`; anything else is `415`.

| Header | Rule |
|---|---|
| `Idempotency-Key` | **Required.** Opaque, 8–128 characters from `[A-Za-z0-9._:~-]`. Scoped to the verified `team_id`, never to the caller's claim. Violations are `400 invalid_idempotency_key`. |
| `X-Request-Id` | Optional client correlation id. Echoed in bodies and logs. Never used for identity, dedupe, or storage paths. |

[DEFAULT D5] The **required** `Idempotency-Key` is this design's choice, made because [CONFIRMED] a
duplicate retry must not start duplicate processing, and the only way to recognize a duplicate retry
without storing document content in an index is a caller-supplied key. It is explicitly **not** something
the user approved; the user approved the behavior. If the user prefers an optional key, the fallback
behavior for a missing key must be chosen instead (a per-request key makes retries non-idempotent), which
is why the required form is the minimal complete option. `team_id + "\n" + key` is hashed (§5.2); the raw
key is never written to a path or a log.

Content validation before acceptance, on bytes and not on the declared type:
1. Sniff the container: `%PDF-` → PDF; `\xFF\xD8\xFF` → JPEG; `\x89PNG\r\n\x1a\n` → PNG; `II*\0` / `MM\0*`
   → TIFF. A mismatch with the declared part type is `415`.
2. Reject archives and Office formats by their own magic (`PK\x03\x04`, `\xD0\xCF\x11\xE0`) with `415`,
   even when declared as PDF/JPEG.
3. PDFs containing `/Encrypt` in the trailer dictionary → `422 encrypted_document`.
4. Empty body or zero-byte file → `422 empty_document`.
5. Byte length over the limit → `413 file_too_large`; page/frame count over the limit → `422
   too_many_pages`.
6. Structurally unusable PDF/TIFF (header present, no pages/trailer) → `422 malformed_document`.

[DEFAULT D21] Provisional, explicitly **not** benchmark-derived bounds (final values are [BLOCKED B3]):
50 MiB per upload, 500 pages/frames per document. They are configuration, so tightening them later is not
a contract change.

### 2.3 Submission ordering and responses

[DEFAULT D6] **Persist-first.** Order: authenticate → validate → idempotency claim → materialize the job
and its input (§5.2) → record dispatch intent and attempt the launch → respond. A failed launch is **not**
a `503`; it leaves `dispatch.acknowledged = false` and reconciliation picks the job up (§4.2). A failure
**before** the job is usable — the input object could not be written — is a `503`, and the partial record
is removed so that retrying the same key is safe (§5.2).

| Status | When | Body |
|---|---|---|
| `202 Accepted` | New job persisted, launch attempted | Status object (§2.5) |
| `200 OK` | Idempotent hit: same key, same content, job not expired | Existing status object |
| `400` | Invalid request shape or key | Error |
| `401` | No credential, or the presented ID token fails in-app verification | Error |
| `403` | Verified caller not mapped to a team | Error |
| `409` | Same key, different content | Error `idempotency_conflict` |
| `413` / `415` / `422` | Size / declared type / content unusable | Error |
| `429` | Admission ceiling reached | Error `capacity_exceeded` + `Retry-After: 30` |
| `503` | Durable persistence failed: **no usable job exists** and the partial record was removed, so the same key can be retried | Error `storage_unavailable` + `Retry-After: 5` |

There is no `404` on this route by construction: the idempotency record is team-scoped, so a submission
can only ever reach a job belonging to the caller's own team.

`202` means: the job record and the input object are durably persisted, and a launch was **attempted and
its intent recorded**. It is a launch acknowledgment. It is not evidence that a worker started, that the
launch call was accepted by the platform, or that OCR succeeded. A `202` carrying
`dispatch.acknowledged = false` is a normal, valid response.

The idempotent-hit path (`200`) **still ensures the job is materialized and dispatched** (§5.2, §4.1) —
when the job is **not yet final**. A hit on a job that is already final returns the existing status/result
object and **writes nothing**: the only reason to re-run materialization is the claim-to-materialize crash
window of a non-final job (§5.2).

### 2.4 Result retrieval responses

| Status | When | Body |
|---|---|---|
| `200` | Job in a final state (`succeeded` / `partially_succeeded` / `failed`), `now < expires_at` | Result object |
| `202` | Job not final (`accepted` / `running`) | Status object + `Retry-After: 10` |
| `404` | Unknown id; **or** id not owned by the caller's team; **or** all of the job's records were already removed by cleanup | Error `not_found` |
| `410` | The job's records still exist **and** `now ≥ expires_at` | Error `job_expired` |

`202` does not depend on `deadline_at`: a job past its deadline is finalized by reconciliation, and until
that pass completes the honest answer is still "not final".

One rule, no second rule: **(a) cleanup has not removed the records yet → `410 job_expired`; (b) cleanup
has removed them → `404 not_found`.** [CONFIRMED] The user accepted `404` after cleanup, so no
contentless stub is kept merely to make `410` permanent, and this is a decided behavior rather than an
open option.

The `410` → `404` changeover sits inside the cleanup window (target ≤1h after `expires_at`, §4.3), so it
is a bounded disclosure and not a guarantee of a particular code. What *is* guaranteed, and is what the
spec requires, is that **access is denied at `expires_at` in both cases**: a late or failed cleanup task
cannot extend access, and an early cleanup cannot grant it. Because `expires_at` is evaluated from the
job's own record, and cleanup deletes that record, the code flips — that is the accepted consequence.
Ownership and expiry are therefore both read from the job record; there is no second source. [DEFAULT D10]

`404` (never `403`) for a foreign or cleaned-up job, so a caller cannot probe another team's job ids.
This holds only because lookup is **strictly team-prefixed**: a job id is resolved under the verified
caller's `team_id` (§5.7, §6.3), never by a global scan, so a foreign id is simply absent.

### 2.5 Status object

```json
{
  "schema_version": "ocr.v1",
  "job_id": "6d1f9b2a-…",
  "state": "accepted|running|succeeded|partially_succeeded|failed",
  "timestamps": {
    "accepted_at": "2026-11-08T04:31:07Z",
    "deadline_at": "2026-11-09T04:31:07Z",
    "finalized_at": null,
    "expires_at": null
  },
  "dispatch": {"acknowledged": false, "execution_ref": null},
  "progress": {"pages_total": 12, "pages_done": 5}
}
```

- `dispatch.execution_ref` is the Cloud Run execution name from the launch call *(ref:*5*)*. It is **not**
  completion.
- `dispatch.acknowledged = false` covers both "no launch has been accepted yet" and "persisted after a
  failed launch". It is not an error indicator; keep polling. It is **diagnostic only**: no decision in
  this design reads it, so a job is re-dispatched whether or not its launch was acknowledged (§4.1,
  [DEFAULT D31]).
- `progress` is opportunistic and may be absent. Callers must not depend on it. `pages_done` counts pages
  with a durable checkpoint.
- Attempt counters, lease and heartbeat state, and every engine identifier are **not** in this object
  (§8, [DEFAULT D19]).

### 2.6 Result object

```json
{
  "schema_version": "ocr.v1",
  "job_id": "6d1f9b2a-…",
  "state": "succeeded|partially_succeeded|failed",
  "reason": null,
  "completeness": "complete|partial",
  "accuracy": "not_verified",
  "page_count": 12,
  "text": "combined text of successful pages, in page order",
  "pages": [ /* §2.7 */ ],
  "warnings": ["no_text"],
  "timestamps": {
    "accepted_at": "2026-11-08T04:31:07Z",
    "finalized_at": "2026-11-08T04:33:41Z",
    "expires_at": "2026-11-09T04:33:41Z"
  }
}
```

- `state` is the final state; `reason` explains a non-success finalization: `null` on success, otherwise
  one of `deadline_exceeded`, `engine_unavailable`, `input_unreadable`, `internal_error`. [DEFAULT D22]
  This is where the spec's "deadline reason" is carried.
- `completeness = "complete"` iff every page is `succeeded` and there are no page-level errors, and the
  job state is `succeeded`. Otherwise `partial`.
- `accuracy` is the constant `"not_verified"`. It separates execution success from text accuracy
  (issue AC #1, spec "Further Notes"). No confidence score is invented anywhere in this contract.
- `text` is present only when at least one page succeeded: the successful pages' `text` joined by a single
  `\n` in ascending `page_number`. Failed pages contribute nothing and add no separator.
- `warnings` is the union of page warnings, in page order, deduplicated. `["no_text"]` on a document whose
  every page succeeded but produced no text.
- `timestamps.finalized_at` and therefore `expires_at` come from `final.json`, the one authoritative
  final-state record (§3, §5.4). `expires_at` is computed as `finalized_at + 24h` and stored in
  `final.json` for audit; readers compare against the computed value, so no second writer can make the
  gate disagree with the timestamp it reports.
- No engine identifier and no expense/classification field appears here (issue AC #1, spec "Out of
  Scope"). [DEFAULT D19]

### 2.7 Page object and numbering

```json
{
  "page_number": 1,
  "status": "succeeded|failed",
  "text_source": "embedded|ocr",
  "text": "…",
  "warnings": ["no_text", "possible_low_quality"],
  "error_code": null
}
```

- `page_number` is **1-based, contiguous, in document order**. `page_count == len(pages)`, one entry per
  source page: each PDF page, each TIFF frame (including multi-frame TIFF), and the single page of a JPEG
  or PNG. [DEFAULT D24] Failed pages are always present as `status: "failed"` with an `error_code`; they
  are never dropped.
- `text_source` [DEFAULT D11]: `embedded` only when (a) the input is a PDF, (b) the page's extracted text
  layer, after Unicode NFC normalization and trimming, contains at least one non-whitespace character, and
  (c) it contains no `U+FFFD` replacement character. Otherwise `ocr`. Non-PDF inputs are always `ocr`. The
  criteria are deliberately mechanical so they are testable at seam S3; refining them is a change to a
  [DEFAULT], not to the contract. Neither source is automatically authoritative.
- Text rules [DEFAULT D23]: a page's `text` is the engine output with leading and trailing whitespace
  stripped; a `succeeded` page with no recognized text has `text: ""` plus warning `no_text`; a `failed`
  page has `text: null` and an `error_code`. Warnings are quality signals, `error_code` is an execution
  failure; they are never conflated, and a page may be `succeeded` with warnings.

### 2.8 Error vocabulary (stable codes)

`unsupported_media_type`, `file_too_large`, `too_many_pages`, `empty_document`, `malformed_document`,
`encrypted_document`, `invalid_idempotency_key`, `idempotency_conflict`, `unauthorized_caller`,
`team_not_authorized`, `not_found`, `job_expired`, `capacity_exceeded`, `storage_unavailable`,
`deadline_exceeded`, `page_engine_error`, `engine_unavailable`, `internal_error`.

```json
{"schema_version": "ocr.v1",
 "error": {"code": "file_too_large", "message": "file exceeds the size limit", "request_id": "…"}}
```

`message` is a fixed allowlisted string per code — no paths, tokens, bucket, job, or processor names.

Changes from rev 2 [DEFAULT, decided by §2.3/§2.4]: `dispatch_unavailable` is **removed** — under
persist-first there is no path that reports a failed launch as an error, and the pre-persistence failure
is `storage_unavailable`. `job_not_finalized` is **removed** — a pending job is reported by `202` with a
status object, which is not an error. Both removals are contract simplifications, not behavior changes.
`deadline_exceeded`, `engine_unavailable`, and `input_unreadable` are reused as `reason` values in the
result object (§2.6) and never returned as HTTP error codes.

### 2.9 Versioning

`schema_version` (`"ocr.v1"`) appears in every request-scoped body and in `job.json`. Breaking shape
changes bump it; additive optional fields do not. Replacing the recognition engine does **not** change
this contract (spec: "one approved engine behind an application-owned interface").

---

## 3. Job state machine and timestamps

```
accepted ──► running ──► succeeded
                │   └──► partially_succeeded
                └──────► failed   (engine error, unreadable input, deadline)
   └────────────────────► failed   (nothing ran before the deadline, or the engine was never reachable)
```

[DEFAULT D8] `queued` is **removed** from the enum. It was not required by the spec or the issue; its
only producer was a conditional state write that may never happen; callers were already told to treat it
identically to `accepted`; and its information is the `dispatch.acknowledged` boolean, which now lives in
its own object instead of a second state transition (§5.7). Removing it eliminates a non-load-bearing
write and the "accepted → queued may never happen" caveat entirely.

- **`accepted`** — set when `job.json` is created, after auth, validation, and the idempotency decision.
- **`running`** — set by the first worker that holds a valid lease and has begun page work. One CAS write.
- **Final states** — set only by the `final.json` arbiter (§5.4).

Timestamps:
- **`accepted_at`** — server-set exactly once at `job.json` creation. The authoritative start of the 24h
  lifetime.
- **`deadline_at`** = `accepted_at + 24h` (configurable ceiling; the only tunable is the ceiling, and the
  24h value is from the spec). At this instant further processing stops and the job finalizes with
  `reason: deadline_exceeded`: `partially_succeeded` if usable page results exist, else `failed`.
- **`finalized_at`** — the **one** authoritative final-state timestamp, written exactly once inside
  `final.json` (§5.4). The retention clock starts here, not at upload.
- **`expires_at`** = `finalized_at + 24h`, computed on read and also stored in `final.json`. At
  `now ≥ expires_at` the API answers `410` while the record exists and `404` after cleanup removes it
  (§2.4). Cleanup targets ≤1h after that instant and alerts if overdue; the deletion pass may not start
  before the §4.3 worker-lifetime guard, which D32 keeps ≤ `expires_at + 1 h`.
- `expires_at` is `null` until finalization. A job pending past `deadline_at` is finalized by
  reconciliation, so `expires_at` is always eventually set.

**Exactly-once finalization** is enforced by create-if-absent on `final.json` *(ref:*1*)*: the first
writer wins, every other writer receives `412` and aborts. This is what prevents a stale worker from
replacing a final result — not the lease, and not a fence alone (§5.3) — and only while that object exists
(§5.4). **Execution is at-least-once** (§4.1): the guarantee is one logical job per key and at most one
published result, never one execution per job.

**Idempotency conflict rules** — key is `(team_id, Idempotency-Key)`, content identity is
`SHA-256(bytes) + declared content type + byte length` computed from the incoming request:

| Existing record | Same content | Different content |
|---|---|---|
| Live (job non-final, or final with `now < expires_at`) | `200` with the existing job; no new work | `409 idempotency_conflict` |
| Expired or absent | new job accepted (record replaced by CAS, §5.2) | new job accepted |

The comparison uses the hash stored in the record and the hash of the incoming bytes, so it never needs to
read the stored input object. That matters because the input is deleted on full success (§7) while the
record lives until `expires_at`: a retry after a successful job still gets `200` with the same job id
instead of a second job. After the record expires, deduplication is no longer guaranteed (spec).

---

## 4. Dispatch, reconciliation, cleanup

### 4.1 Dispatch

The API calls Cloud Run Admin `POST /v2/{name}:run` on the **version-pinned worker job** for the job's
recorded revision (§8) *(ref:*5*)*. The response is an `Operation` *(ref:*5*)*; the API records the
acknowledgment and, when present, the execution reference, and does not wait. `createTime` on an execution
is "acknowledged by the execution controller" and "not guaranteed to be set in happens-before order"
*(ref:*6*)*, which is a second reason not to read launch success as processing progress. Worker completion
is observed **only** from the GCS finalization record.

**Overridable at launch** *(ref:*5*)*: `overrides.containerOverrides[].{name,args,env,clearArgs}`,
`overrides.taskCount`, `overrides.timeout`. There is **no `image` field** — the image cannot be chosen at
execution time; it comes from the Job's template (§8). Sending `overrides` requires
`run.jobs.runWithOverrides` on the job *(ref:*5*)*, which the API service account must actually hold
([BLOCKED B2]).

[DEFAULT D7] Dispatch is direct `jobs:run` plus periodic reconciliation, with no queue service. This is
the minimal arrangement that works with GCS-only state and the spec's permission to use managed
dispatch/scheduling services.

**Dispatch intent and the at-most-2 rule** [DEFAULT D6, D14]. Duplicate dispatch is possible in principle,
so it is bounded rather than denied:

1. `dispatch.json` is created with `ifGenerationMatch=0` holding
   `{attempts: 1, last_attempt_at, acknowledged: false, execution_ref: null, last_error_code: null}`.
2. **Only the caller that created it calls `jobs:run`**, and immediately before the call it re-reads the
   pinned job and requires its template image to equal `job.json.worker_revision_digest` (§8); a mismatch
   is the non-retryable case in step 5. A caller that gets `412` on step 1 does not dispatch: another
   submitter of the same job already owns this attempt. This is what stops a concurrent duplicate
   submission from launching two executions.
3. On a launch response, the dispatcher CAS-updates `dispatch.json` to
   `{acknowledged: true, execution_ref: …}` with `ifGenerationMatch` = the generation it read on step 1.
   Losing this CAS is harmless: it degrades to the reconciliation path below. **`acknowledged` and
   `execution_ref` are diagnostic fields; no decision in this design reads them** [DEFAULT D31], because a
   launch response is an `Operation` *(ref:*5*)*, not an execution, and an acknowledged operation can still
   fail later or never start an execution at all.
4. Reconciliation may claim a further attempt by CAS-incrementing `attempts`, and only when **all** hold:
   the job is non-final; `now < deadline_at`; `now - last_attempt_at ≥ 5 min`; `attempts < 2`; and no live
   lease exists on the job (§5.3). The lease check is what avoids re-dispatching a worker that is actually
   running; the attempt cap is what bounds duplicate *execution*. `acknowledged` is deliberately not
   consulted. The `200` idempotent path is not a re-dispatcher: it only creates `dispatch.json` when it is
   absent (§2.3, §5.2).
5. If a launch returns a **non-retryable** error (`PERMISSION_DENIED`, `NOT_FOUND` for the pinned job, or
   the image/pin mismatch of step 2), reconciliation does not keep trying: it finalizes the job `failed`
   with `reason: engine_unavailable` and raises an alert, instead of letting the request sit until the
   24h deadline.

**Residual window, stated plainly.** Re-dispatch does not consult `acknowledged`, so it fires in three cases
that are indistinguishable without reading the execution resource: (a) a launch response was lost; (b) the
operation was acknowledged and then failed; (c) the execution is still queued and has not taken a lease.
In all three, once the lease check and the 5-minute spacing allow it, the reconciler starts a second
execution. Combined with the platform's own task retries *(ref:*8*)*, duplicate **work** is bounded by `≤2`
dispatch attempts × `1 + max-retries` task attempts, by the lease TTL, and by `deadline_at`.
**Guarantees, exactly:** one logical job per `(team_id, key)`; at-least-once execution; **at most one
published result** per job (§5.4). There is **no** single-execution guarantee, and nothing in this document
suppresses a second execution. [CONFIRMED] The user's requirement — a duplicate *submission retry* must not
start duplicate processing — is satisfied because a retry of the same key never creates a job and never owns
a dispatch attempt: it takes the `200` path (§2.3, §5.2).

**Stranded execution, stated plainly.** v1 does not read `Job.execution` state, so a job whose dispatched
execution never starts — or whose second attempt also dies before finalizing — is not classified early: it
waits for reconciliation at `deadline_at` and is finalized there (`partially_succeeded` if usable page
results exist, else `failed` / `deadline_exceeded`). The execution resource does expose terminal state and
error conditions *(ref:*6*)*, so an earlier classification is *possible*; it would add an Admin API
dependency and an IAM permission, so it is deferred and named in §13/B2 rather than assumed here. This is
the honest bound on rev 3's "stranded until the deadline": recovery is now guaranteed to be *attempted*
twice, not to be *fast*.

Worker (Cloud Run Job task) contract:
- Reads `job.json` **before writing any object**, acquires and renews a lease, processes pages, publishes
  per-page checkpoints, then finalizes (§5). A worker whose `job.json` read fails writes **nothing** and
  exits non-zero: after cleanup has removed the records, a straggler therefore finds no job to resume and
  creates nothing [DEFAULT D30].
- **Writes nothing for an attempt that starts at or after `deadline_at`**, and exits non-zero
  [DEFAULT D30, extended in rev 5]. Processing stops at `deadline_at` (§3). This is what bounds a
  straggler's last possible write to `deadline_at + per_attempt_timeout` — one in-flight attempt — instead
  of its whole platform retry chain *(ref:*7*,*8*)*, and the §4.3 cleanup guard is derived from it.
- Exits `0` on success and non-zero on failure *(ref:*10*)*; jobs must not listen on a port *(ref:*10*)*.
- Uses `CLOUD_RUN_JOB`, `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_ATTEMPT`,
  `CLOUD_RUN_TASK_COUNT` *(ref:*10*)* for identity and log correlation.
- `--max-retries` is per task, default 3, range 0–10 *(ref:*8*)*; the design sets **2** [DEFAULT, final
  value BLOCKED B3]. Application checkpointing is required alongside platform retries *(ref:*9*)*.
- Task timeout applies **per attempt**, default 10m, max 168h *(ref:*7*)*; a multi-hour timeout "is not a
  guarantee of uninterrupted execution" *(ref:*7*)* and non-GPU tasks are live-migrated with
  `SIGTSTP`/`SIGCONT` *(ref:*7*)*. [DEFAULT D21] provisional per-attempt timeout 30m.

### 4.2 Reconciliation

A scheduled trigger (Cloud Scheduler → Cloud Run Job `dev-ocr-service-reconcile`) periodically scans GCS
state and repairs, using only conditional writes, so concurrent reconcilers cannot double-finalize
(§5.4 arbitrates). Rows are evaluated in order; the missing-input row precedes any dispatch row, so a
partially materialized job is finalized rather than launched.

| Condition | Action |
|---|---|
| Non-final job, `now < deadline_at`, `dispatch.json` absent | dispatch it (crash between materialization and launch, or a lost `202` response) [DEFAULT D28] |
| Non-final job, `now < deadline_at`, ≥5 min since the last attempt, `attempts < 2`, no live lease — `acknowledged` is **not** consulted (§4.1) | claim and dispatch the next attempt (§4.1) |
| Non-final job, dispatch exhausted and the last error was non-retryable | finalize `failed` / `engine_unavailable` + alert |
| Non-final job, `now ≥ deadline_at` | finalize `deadline_exceeded` (§3) |
| Final job whose `job.json` state disagrees with `final.json` | repair `job.json` from `final.json` |
| Non-final job whose input object is absent, **and** `now - accepted_at ≥ 5 min`, **and** no live lease (a materialization that never completed, §5.2) | finalize `failed` / `internal_error`, alert, and delete its `idem/*` record only with `ifGenerationMatch` = the generation read and only if that record still names this `job_id` |
| Expired live `idem/*` record whose job is final and past `expires_at` (deleted with `ifGenerationMatch` = the generation read) | delete (same pass as cleanup, §4.3) |
| `capacity/inflight.json` drift | recompute from enumerated non-final jobs (§4.4) |

Everything else that is non-final and inside its deadline is left alone: the deadline will finalize it.
Reconciliation never resumes page work itself; it only dispatches, finalizes, repairs, or deletes.

Two guards in that table are new in rev 4, and both exist so a repair cannot destroy another actor's work or
resurrect deleted content. (a) The dispatch rows require `now < deadline_at`: a job at its deadline is
finalized, never launched, which is also what anchors the cleanup guard in §4.3. (b) The missing-input row
requires a grace window and no live lease, so a materialization that is still in flight is not finalized
underneath its author — the same class of bug as the retry-path compensation in §5.2. Every `idem/*`
deletion in this section is conditional on the generation read and on the record still naming that job.

### 4.3 Cleanup

Cloud Scheduler → Cloud Run Job `dev-ocr-service-cleanup` is the physical deleter, and it is
**application-driven**: Object Lifecycle Management changes take up to 24h to propagate *(ref:*4*)*, so a
lifecycle rule cannot meet the ≤1h cleanup target. A lifecycle rule is optional as an orphan backstop
only, never as the SLA mechanism.

- **Full success:** the input object `inputs/{team_id}/{job_id}` is deleted immediately after
  finalization — by the finalizing worker, and by reconciliation if the worker died before deleting
  (§5.5). A momentary failure is retried; overdue deletion is alerted. That deletion is not undone by a
  post-success retry, because a `200` hit on a final job writes nothing (§2.3, §5.2) — rev 3's retry path
  re-uploaded the input here, extending its retention to `expires_at`.
- **When the guard below allows it** (`now ≥ expires_at` = `finalized_at + 24h` is necessary but not
  sufficient), the cleanup job deletes the job in two passes. Both passes **enumerate the job's own prefix**
  (`jobs/{team_id}/{job_id}/`) plus the two named objects outside it (`inputs/{team_id}/{job_id}`,
  `idem/{team_id}/{key_hash}`) instead of walking a fixed name list, and **every delete carries
  `ifGenerationMatch` set to the generation read in this pass** *(ref:*1*)*:
  1. **Content:** the input object, every `pages/*.json`, and every `result.*.json` — including
     `result.{fence}.json` objects written by workers that never won `final.json`. There can be more than
     one such object (a straggler writes its own fence), so a fixed list would leak unpublished results.
  2. **Records:** `dispatch.json` → `final.json` → `job.json` → `claim.json` → the
     `idem/{team_id}/{key_hash}` record.
  Content first, records last, so the `410`/`404` flip (§2.4) can only happen after no content-bearing
  object remains: a reader in the window either sees the records (→ `410`, no content served) or sees
  nothing (→ `404`). Cleanup is idempotent and re-runnable; a partial pass is completed by the next pass.
  `claim.json` is deleted here because a crashed holder may never release it (§5.3) — rev 3 did not delete
  it at all, so every crashed lease was a permanent leak.
- **Every delete is conditional, including the `idem/*` record.** A delete whose precondition fails is
  **skipped, not retried blind**: a mismatch means another actor replaced the object after this pass read
  it. This matters most for the idempotency record, whose name is reused: an expired record is CAS-replaced
  by a new submission with the same key and different content (§5.2), so a name-addressed delete would
  remove a **live** record for a **new** job, silently losing deduplication (defeating [CONFIRMED] D1) and
  stranding a job mid-flight. The record is deleted only when the generation still matches the one read
  **and** its `job_id` field still names this job.
- **Worker-lifetime guard (corrected in rev 5).** A job's objects may not be deleted before
  `max(expires_at, deadline_at + per_attempt_timeout)`. `expires_at` is `finalized_at + 24h` and a job can
  finalize long before its deadline, so `expires_at` can fall earlier than the last possible write of a
  straggler dispatched before `deadline_at`. Without the guard such a straggler could re-create `pages/*`,
  an unpublished `result.*.json`, or even a fresh `final.json` (create-if-absent succeeds against a name
  cleanup just deleted) **after** the pass was reported complete — content reappearing after the retention
  window with no scheduled deleter.
  The guard is derived, not guessed, from three rules: no dispatch happens at or after `deadline_at`
  (§4.2); a task attempt is bounded by the per-attempt timeout *(ref:*7*)*; and a worker attempt that starts
  at or after `deadline_at` writes nothing (§4.1 worker contract, new in rev 5). The third rule is what
  bounds a straggler to **one** timeout past the deadline rather than the whole platform retry chain:
  attempt 1 may start just before `deadline_at` and write for up to one timeout past it, but every later
  attempt starts after `deadline_at` and writes nothing. So the last possible write is
  `deadline_at + per_attempt_timeout`. Rev 4 used `(max_retries + 1) × per_attempt_timeout` because it let
  the retry chain run past the deadline; with the third rule that term is gone.
  **Why this no longer conflicts with the ≤1 h target.** The guard exceeds `expires_at` only when the job's
  processing time is shorter than one per-attempt timeout
  (`guard − expires_at = max(0, per_attempt_timeout − (finalized_at − accepted_at))`) — i.e. for jobs that
  finalize *early*, the opposite of rev 4's "finalize within ~90 min of their deadline" claim. Because
  `expires_at ≥ deadline_at` always (`finalized_at ≥ accepted_at`), the guard is
  `≤ expires_at + per_attempt_timeout`. The enforced configuration invariant
  `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` [DEFAULT D32, asserted at startup] then puts the
  first legal pass at ≤ `expires_at + 1 h`, so the ≤1 h target is met by construction rather than by a
  longer wait.
- **Target:** the deletion pass completes within ≤1 h of `expires_at`; alert if overdue (mechanism
  [BLOCKED B4] as a configuration item, not a design gap). The guaranteed part is the *earliest legal pass
  start* being ≤ `expires_at + 1 h` under D32; the residual is scheduler/execution latency, which is
  alerted, not guaranteed. API access is denied from `expires_at` regardless (§2.4, §7).
- API access is denied from `expires_at` whether or not cleanup has run (§2.4, §7): the code is `410`
  while the records exist and `404` once they are gone, and content is never served after `expires_at`.

### 4.4 Capacity and overload

[DEFAULT D13, corrected in rev 5] Cluster-wide admission control needs a coordination object, and GCS-only
state gives exactly one (`capacity/inflight.json`). The counter is **fail-closed**: a submission that cannot
record its admission is rejected, never admitted unaccounted.

- `capacity/inflight.json` holds `{count, updated_at}`.
- **Acquire** at submission, after authentication and validation but before persisting the job: read
  `(generation, count)`. If `count ≥ ceiling`, do not write and return `429 capacity_exceeded` with
  `Retry-After: 30`. Otherwise CAS-write `count + 1` with `ifGenerationMatch = generation`. On `412`,
  re-read and re-decide, bounded to 5 attempts with jittered backoff. **On exhaustion, return
  `429 capacity_exceeded` — fail closed.** There is no fail-open path: an admission that cannot be recorded
  is exactly the unaccounted case the bound must exclude. (Rev 4 failed open here, which made the ceiling
  unbounded under contention; that is withdrawn.)
- **Release** exactly once, by the winner of `final.json` (§5.4) and by no other actor: CAS-decrement with
  `ifGenerationMatch`. On `412`, re-read and re-decide; on exhaustion, leave it — the residual is an
  over-count, repaired below. Reconciliation repairs `job.json` from `final.json` but **never** releases, so
  a repaired job cannot be released twice.
- **Repair** (reconciliation): recompute `count` as the number of enumerated non-final jobs (listing is
  strongly consistent *(ref:*2*)*) and CAS-set it. No percentage tolerance — a 10 % tolerance would be a
  bound violation by construction.
- **Why the bound holds.** Every path that could make the counter read *low* is removed: a job is released
  at most once (arbiter-only release), and an unrecorded admission is impossible (acquire fails closed).
  Residual drift is therefore one-directional — the counter reads **high**, which can only over-reject. The
  one disclosed exception: a repair pass that races an acquisition whose job is not yet persisted reads the
  enumeration as a lower bound and can transiently set `count` below the true in-flight number by at most
  the number of acquisitions in flight at that instant; the next pass restores it. This is a bounded,
  self-correcting residual of a single-object counter, **not a guarantee of an exact ceiling**.
- **Throughput limit, stated plainly.** `capacity/inflight.json` is one serialization point; every
  submission and every finalization rewrites it. Sustained CAS contention surfaces as `429` (fail closed),
  so the counter's throughput bounds the submission rate. [BLOCKED B6]: an exact ceiling at higher rates
  needs a transactional counter (Firestore/Spanner), which GCS-only state does not provide.
- The ceiling value is [BLOCKED B3].
- The API service is also capped by `--max-instances`, where Cloud Run queues briefly and then fails the
  request with `429` *(ref:*13*)*. That platform behavior is not relied on for the document ceiling; it is
  noted because it means a client can see a `429` that did not come from this design. [DEFAULT D21]
  provisional API `max-instances` 4, worker `--parallelism` 1 *(ref:*14*)* with one task per execution.

---

## 5. GCS conditional-write protocol

### 5.1 The only primitives assumed

Cloud Storage gives **single-object atomicity** and strong read-after-write, read-after-metadata-update,
read-after-delete, and list consistency *(ref:*2*)*, plus **generation/metageneration preconditions**
(`ifGenerationMatch`, `ifMetagenerationMatch`, HTTP `412` on mismatch) *(ref:*1*)*. A generation-match
value of `0` is a documented special case: the request proceeds only if no object with that name exists
**"or if there are only noncurrent versions of the object in the bucket"**; a live version fails with
`412` *(ref:*1*)*. This document calls that **create-if-absent** and depends on two consequences:

1. **Object Versioning must stay disabled on this bucket.** Versioning "retains a noncurrent object
   version each time you replace or delete a live object version" *(ref:*12*)* — noncurrent versions are
   exactly the condition that makes a `0`-precondition create succeed where "absent" was assumed, so an
   enabled-versioning bucket silently breaks every create-if-absent arbiter.
2. How a **soft-deleted** version interacts with the `0` precondition is **not stated** in the
   references and must be verified before this protocol is trusted ([BLOCKED B1]). Soft delete is on by
   default (7 days, 7–90, `0` disables) *(ref:*3*)*, so the default configuration is not the assumed one.

Batch and multi-object operations are **not** atomic *(ref:*2*)*; this design never assumes a
cross-object transaction (§5.6). Object Versioning, soft-delete retention, Bucket Lock, and Object
Retention Lock are configuration dependencies that must be verified, not assumed (§7, [BLOCKED B1/B4]).

### 5.2 Idempotency claim and job materialization

`idem/{team_id}/{key_hash}`, where `key_hash = SHA-256(team_id + "\n" + Idempotency-Key)`, holds
`{job_id, content_hash, content_type, byte_length, accepted_at}`.

Order [DEFAULT D9] — the record is the intent, the job directory is the fact, and the retry path repairs
the gap:

1. **Claim**: create `idem/…` with `ifGenerationMatch=0`, carrying a freshly generated `job_id`.
   - Created → this caller owns `job_id` (the `202` path).
   - `412` → re-read the record (read-after-write is strongly consistent *(ref:*2*)*, so the winner's
     record is visible). Same content → the `200` path, adopting `existing.job_id`. Different content →
     `409 idempotency_conflict`. The record is live if the job is non-final or `now < expires_at`.
     Expired → CAS-replace it with the new job's record (`ifGenerationMatch` = the generation just read);
     if that CAS fails, re-read and re-decide.
2. **Materialize**: create `jobs/{team_id}/{job_id}/job.json` with `ifGenerationMatch=0`, holding the
   identity and pin fields (§5.7) and `state: accepted`. This call is idempotent by construction. A
   `412` means the object already exists; read it and require that the identity fields match — a mismatch
   is corruption and fails closed with `internal_error`.
3. **Persist the input**: write `inputs/{team_id}/{job_id}` with `ifGenerationMatch=0`. **A `412` here is
   success, not failure**: the name is job-scoped and server-generated, and only an actor that matched the
   idempotency record's content identity can reach it (step 1), so an existing object means this job's input
   is already present — the normal case on the retry (`200`) path when the original submitter's write
   succeeded. Read its metadata and require `size == job.json.byte_length`; a mismatch is corruption → fail
   closed `internal_error` + alert. **A `412` must never trigger compensation** [DEFAULT D29].
4. **Dispatch**: §4.1 (create `dispatch.json` first; only its creator launches).

**A job is usable only when both `job.json` and its input object exist**, in that order of creation. A
*genuine* step-3 failure (a storage error, not `412`) responds `503 storage_unavailable`, and compensation
is scoped to what the compensating actor actually owns [DEFAULT D29]:

- Only the actor that **created the `idem/*` record** (the claim owner) may delete it, and only with
  `ifGenerationMatch` = the generation it read when it created it. A retry that adopted another actor's
  `job_id` (the `200` path) **must not** delete that record: the record belongs to a live job that another
  actor may still be materializing, so deleting it would lose deduplication for that key (defeating
  [CONFIRMED] D1) and strand an in-flight job. Rev 3 applied the same compensation on both paths, which on
  the retry path deleted a record it had not created.
- Only objects this actor itself created are deleted, each with the generation read at creation.
- A compensating actor that cannot complete compensation does nothing further; the repair falls to
  reconciliation's missing-input rule, after its grace window and with a conditional record delete (§4.2).
  An unreadable or missing input never becomes an unexplained worker crash.
- The dispatcher checks the input object first, so a partially materialized job is never launched.

Both the `202` and the `200` paths run steps 2–4 **for a non-final job**, which is what makes step 1's
crash window harmless: if the process died after the claim and before materializing, the retry (or the
`200` path) materializes the job at the idem record's `job_id` instead of returning a job that does not
exist. [DEFAULT D9] A `200` hit on a job that is **already final** skips steps 2–4 entirely — nothing is
materialized and nothing is dispatched — which also keeps the spec's "delete inputs after full success"
(§7) true: a post-success retry must not re-upload the input of a job that no longer needs one (rev 3
re-created it, because the input object had been deleted on success and create-if-absent therefore
succeeded). Reconciliation also dispatches any materialized non-final job with no `dispatch.json` (§4.2).
If the caller never retries, nothing runs and the record expires; no orphan job is created.

### 5.3 Claim, lease, and fencing — what is guaranteed, and what is not

Workers coordinate with a lease object, `claim.json`, holding
`{owner, fence, lease_expires_at, heartbeat_at}`. Lease TTL 5 min, heartbeat 60 s [DEFAULT D15]; final
values [BLOCKED B3]. Every step below is one conditional single-object write:

1. **Acquire** — create `claim.json` with `ifGenerationMatch=0` and `fence: 1`.
2. **On `412`** an object already exists; read it and compare `lease_expires_at` with `now`:
   - **Live lease** (`lease_expires_at > now`): another worker owns this job. Wait and retry with backoff,
     bounded by the task timeout, then exit non-zero **without writing any other object**.
   - **Expired lease**: **take over** by CAS-replacing the object — `ifGenerationMatch` = the generation
     just read, `fence = read.fence + 1`, `lease_expires_at = now + TTL`, `heartbeat_at = now`. A `412`
     means another taker won the race; re-read and re-decide. This is the only path that puts a second
     worker on one job, and it is what makes a crashed holder recoverable within one lease TTL instead of
     never (rev 3 created the object and never replaced it, so every later acquire got `412` forever).
3. **Renew** — rewrite with `ifMetagenerationMatch`, keeping `owner` and `fence`. A renewal that fails with
   `412` means the lease was taken over: the holder stops writing and exits non-zero.
4. **Release** — delete `claim.json` with `ifGenerationMatch` = the generation read with it, once
   finalization is complete or the worker gives up. Cleanup also removes a lease that was never released
   (§4.3).

**Recovery bound.** A crashed holder blocks takeover for at most one lease TTL, so resumption latency after
a worker crash is `≤ TTL + heartbeat` per attempt (5 min + 60 s provisional) — not the 24 h deadline.
**Honest limit:** the takeover CAS is a single-object compare-and-swap on `claim.json` only. It cannot stop
the superseded holder from writing a page or result object — that is precisely the exclusion the fence does
*not* provide (below). It guarantees only that two *acquirers* cannot both hold a live lease.

**Corrected statement of the fence (rev 3).** Every object a worker guards (`job.json`, `dispatch.json`,
`pages/*`) carries the writer's `{owner, fence}` **as its own field**, and each write uses
`ifGenerationMatch` set to the generation read **from that same object**. That makes each write a
single-object compare-and-swap: a concurrent write to the same object makes the later writer's
precondition fail with `412`. This is a genuine correction of rev 1, which read the fence from
`claim.json` and then wrote a different object — a check-then-act race that no GCS precondition can close.

**What the fence guarantees:** no write is silently interleaved; every object's content is the result of
one writer's read-modify-write against a known generation.

**What the fence does NOT guarantee — and rev 2 wrongly implied it did: exclusion between workers.**
Once a lease expires and another worker acquires it, the older worker may still be running. It can still
successfully CAS-write an object whose stored fence equals its own, because the newer fence lives in the
*other worker's* `claim.json`, and the object's generation has not changed yet. No cross-object compare
can close this window; the fence is coordination, not mutual exclusion. Rev 1/rev 2 sentences such as
"a worker with a stale fence aborts instead of writing" and "the fence bump rejects the straggler" are
therefore withdrawn.

**What makes staleness harmless** (this is the whole correctness argument):
1. `final.json` create-if-absent is the single arbiter: exactly one writer publishes a final state (§5.4).
2. The published result is a fence-unique object name that is created once and never rewritten, so no
   stale writer can replace a published result (§5.4).
3. Page checkpoints are *used* only when their `engine_ref` matches the job's pin, so a stale writer
   cannot cause a mixed-engine result even if its checkpoint is resumed (§5.5).
4. The `job.json` final flip is repaired from `final.json` by reconciliation, so a lost flip is not a
   divergence (§4.2).

**Residual cost:** duplicated OCR work while two workers overlap. Bounded by the lease TTL, the
≤2-attempt dispatch cap, and `deadline_at`. Never a duplicated or mixed published result.

### 5.4 Finalization — exactly once

1. Write the result under a **fence-unique, never-rewritten** name: `result.{fence}.json`, created with
   `ifGenerationMatch=0`.
2. Create `final.json` with `ifGenerationMatch=0`, holding
   `{finalized_at, state, reason, result_object, fence}`. Only one writer succeeds; `412` means another
   writer already finalized, so this one aborts. This is the **only** atomic cross-writer arbiter in the
   design, and it names the winning result object.
3. Flip `job.json` to the final state with `ifGenerationMatch` = the generation read with that object
   (the object carries the fence, so this CAS is single-object).

Readers resolve a result **only** through `final.json.result_object`; a `result.*.json` with no matching
`final.json` is unpublished and is ignored (and eventually cleaned up). `finalized_at` is written only
inside `final.json`, which is why it is the single authoritative final-state timestamp (§3).

**Assurance limit.** Steps 1–3 are three independent single-object writes with no cross-object
transaction, so a crash can leave a `result.{fence}.json` or a `final.json` without the `job.json` flip;
reconciliation repairs that from `final.json` (§4.2). "Exactly once" here means **exactly one winning
`final.json`**, not an atomic multi-object commit. [BLOCKED B1] covers verifying the underlying `412` and
create-if-absent behavior against real GCS before any reliability claim.

**The arbiter's authority lasts exactly as long as the object exists.** `ifGenerationMatch=0` on
`final.json` arbitrates only while that name is occupied; once cleanup deletes it, a straggler could create
a *new* `final.json` at the same name and publish a result for a job whose content was already deleted
(rev 3 had no rule against this). That window is closed by lifecycle and worker rules, not by any
cross-object property: cleanup may not start before the worker-lifetime guard (§4.3), and a worker that
cannot read `job.json` writes nothing (§4.1 worker contract). Published results are also not revoked by the
deletion of the arbiter: `final.json` names the winning `result.{fence}.json`, and readers resolve results
only through it while it exists (§2.4, §2.6).

### 5.5 Crash recovery

| Crash point | Recovery |
|---|---|
| After the idempotency claim, before `job.json` | retry path materializes the job at the recorded `job_id` (§5.2); if no retry, nothing runs |
| After `job.json`, before the input object | the job is not usable: the submitter compensates and returns `503`; otherwise reconciliation finalizes `failed` / `internal_error` and deletes the `idem/*` record (§5.2, §4.2) |
| After `job.json`, before `dispatch.json` | reconciliation dispatches (§4.2) |
| After `dispatch.json`, before `jobs.run` returns | `acknowledged` stays `false`; reconciliation re-dispatches after ≥5 min if no live lease exists and `attempts < 2` — with or without `acknowledged` (§4.1) |
| After the lease, before any page | the lease expires; the next attempt **takes the lease over** by CAS-replace with `fence + 1` and recomputes (§5.3) |
| Holder crashes with the lease held | same as the row above: takeover is possible only after the TTL, so a crashed holder blocks resumption for ≤1 TTL, not for the job's lifetime (§5.3) |
| Between page checkpoints | the next attempt lists `pages/*` and resumes from the present ones (§5.5 note below) |
| After `result.{fence}.json`, before `final.json` | the result is unpublished; the next writer or the reconciler finalizes and abandons the earlier object |
| After `final.json` | the job is final: a late writer cannot create a second `final.json` (`412`, while that name exists), cannot rewrite the published fence-unique result, and its `job.json` flip fails its CAS |
| After finalization, before deleting a full-success input | reconciliation deletes it (§4.3) |
| Cleanup starts while a straggler is still writing | prevented by construction, not by locking: no dispatch happens at or after `deadline_at`, no attempt that starts at or after `deadline_at` writes (§4.1), and cleanup cannot start before `max(expires_at, deadline_at + per_attempt_timeout)` (§4.3). A straggler that somehow outlives that bound finds no `job.json` and writes nothing (§4.1 worker contract) |
| During cleanup, partially | cleanup is idempotent and re-runnable (§4.3); a delete whose precondition fails is skipped and re-decided next pass |

Resumption is safe because listing is strongly consistent *(ref:*2*)* and a present checkpoint object is
durable. A checkpoint is accepted only when its `engine_ref` equals the job pin; a checkpoint from a
different engine is never mixed in. The writer's `fence` is recorded on each checkpoint for diagnostics
only: correctness does not depend on it, because the publication arbiter (§5.4) is what makes a stale
worker harmless. [DEFAULT D16] This deliberately drops rev 2's requirement that correctness depend on
fence-tagged resumption, which was a dependency on unverified behavior.

Task restarts reset process memory, so resume state must be read from GCS on startup, matching the
official checkpoint guidance *(ref:*9*)*.

### 5.6 No-transaction invariant

There is no moment at which two objects must change together. Every invariant is "eventually repaired by
reconciliation from single-object state", and every state change is one conditional single-object write.
This is the deliberate trade-off for GCS-only state, and it is why §5.3 does not claim mutual exclusion.

### 5.7 Bucket layout

Single bucket, prefix `dev-ocr-service-*`; `team_id` is the first path segment so IAM, audits, and
listing are team-scoped.

```
inputs/{team_id}/{job_id}
jobs/{team_id}/{job_id}/job.json           # create-if-absent: identity, pin, state, fence; CAS-updated
jobs/{team_id}/{job_id}/claim.json         # lease + heartbeat; created once, CAS-replaced on lease
                                           #   takeover (§5.3), deleted on release or by cleanup (§4.3);
                                           #   its fence value is not the authority for any other object
jobs/{team_id}/{job_id}/dispatch.json      # create-if-absent intent; CAS-updated ack
jobs/{team_id}/{job_id}/pages/{n:05d}.json # one per page checkpoint
jobs/{team_id}/{job_id}/result.{fence}.json# published result; created once, never rewritten
jobs/{team_id}/{job_id}/final.json         # finalization arbiter + authoritative final state
idem/{team_id}/{key_hash}
capacity/inflight.json
```

This layout, not a fixed deletion list, is the cleanup contract: cleanup enumerates these prefixes (§4.3),
because `result.{fence}.json` can exist for more than one fence and `claim.json` can be left behind by a
crashed holder.

`job.json` create payload: `{schema_version, job_id, team_id, accepted_at, deadline_at, content_hash,
content_type, byte_length, idempotency_key_hash, engine_ref, worker_job_name, worker_revision_digest}`.
[DEFAULT D8] It replaces rev 2's `meta.json` + `state.json` pair: one object, one crash window, one
repair rule. Later CAS updates carry **only** `{state, fence, updated_at, reason}`; a reader that finds a
changed identity field treats the record as corrupt, alerts, and fails closed with `internal_error`.
`reason` on `job.json` is a projection of `final.json` for cheap status reads; `final.json` remains
authoritative when they disagree (§4.2).

---

## 6. Authentication and team mapping

### 6.1 Transport authentication

The Cloud Run API service is deployed with IAM authentication required — no `allUsers`, no public access.
Callers present a Google-signed OIDC ID token whose `aud` is the service URL (or a configured custom
audience) and whose caller identity holds `roles/run.invoker` *(ref:*11*)*. Cloud Run's authenticating
front end validates the token before the container sees the request, for every path including `/health`.

### 6.2 Verified caller identity inside the application

Arbitrary request headers must never grant identity (issue AC #5).

- **[DEFAULT D12] Primary: in-process re-verification of the ID token** using Google's official
  authentication library: signature against Google's keys, `iss` in
  {`accounts.google.com`, `https://accounts.google.com`}, `aud` equals the service URL, `exp`/`iat` valid
  with 60 s clock skew. Identity = the `email` claim, with `sub` recorded. The library's key cache must be
  warmable at startup, but `/health` must not depend on it (§2.1).
- The header must be `Authorization: Bearer <id token>`. **[DEFAULT]** `X-Serverless-Authorization` is
  **not** accepted for identity: Cloud Run strips that header's signature before passing the token to the
  container *(ref:*11*)*, so it cannot be re-verified in-process, and accepting it would mean trusting an
  unverified header. A request without a verifiable `Authorization` token is `401 unauthorized_caller`.
- If both headers are present, Cloud Run only checks `X-Serverless-Authorization` *(ref:*11*)* and may
  admit a caller whose `Authorization` token is unverifiable; the application then rejects it with `401`.
  This is the intended fail-closed direction.
- The application never reads `X-Goog-Authenticated-User-Email` or any similar identity header for
  authorization. Such headers are only meaningful behind a proxy that signs them, which is the alternative
  path below.
- Alternative retained for a future revision [BLOCKED B2]: front the service with a Google-managed proxy
  (IAP / external ALB) and verify its signed identity header instead of the raw token.

Two independent layers (Cloud Run IAM, then in-app verification) mean neither a forged header nor a
misconfigured invoker alone can impersonate a team.

### 6.3 Caller → team mapping

A deployed allowlist maps a verified principal to an internal `team_id`, loaded from configuration at
startup. A key binds the **pair** of verified claims, `<email>|<subject>`: an allowlisted address
presented with another subject is unmapped, so a reused or forged address cannot borrow a team. A key
without both claims is a startup failure.

- Unmapped principal → `403 team_not_authorized`.
- `team_id` is derived **only** from verified identity, never from the body, path, query, or a header, and
  it is written into `job.json` and into the idempotency path at creation.
- Ownership is enforced on every job/result operation by comparing the stored `team_id` with the caller's
  mapped `team_id`; a mismatch is `404` (§2.4).
- [BLOCKED B2] The actual n8n identity-token path must be verified, not assumed: n8n on GCP uses the
  metadata server, n8n off GCP needs `iamcredentials.generateIdToken` or workload identity federation
  *(ref:*11*)*. The mapping table and the token path are deployment evidence, not design choices.

Separating verification (§6.1–6.2) from authorization (§6.3) keeps a future Entra integration from
rewriting ownership checks or stored team ids.

---

## 7. Lifecycle, expiry, and retention

- Input deleted immediately after **full** success (finalizing worker, or reconciliation if it died).
  Partial and failed inputs are retained until `expires_at`.
- Everything else is deleted at `now ≥ max(expires_at, deadline_at + per_attempt_timeout)`, in the order
  given in §4.3. The second term is the worker-lifetime guard: cleanup may not start while a straggler
  dispatched before `deadline_at` could still write, because a straggler re-creating
  `pages/*`/`result.*.json`/`final.json` after the pass would leave content with no scheduled deleter. A
  straggler is bounded to one per-attempt timeout past `deadline_at` because no attempt that starts at or
  after `deadline_at` writes (§4.1), and D32 keeps this guard within 1 h of `expires_at`.
- **Access is denied at `expires_at` whether or not cleanup has run**: while the records exist the read is
  `410 job_expired`; once cleanup removed them the same read is `404 not_found` [CONFIRMED]. A late or
  failed cleanup cannot extend access; an early cleanup cannot grant access to content, because content is
  deleted before records.
- Cleanup target ≤1h after `expires_at`; alert on overdue.
- [DEFAULT D17] Bucket configuration: **soft-delete retention `0`, Object Versioning disabled, no Bucket
  Lock, no Object Retention Lock**, and no lifecycle rule used as the deletion mechanism (optional
  long-age backstop only). Rationale: soft delete is enabled by default and applies to **overwrites as
  well as deletions** ("preserves objects and buckets that get deleted or overwritten... in a soft-deleted
  state"), it is on by default at 7 days, and soft-deleted objects still accrue cost, with an explicit
  caution for buckets holding temporary data *(ref:*3*)*. Versioning independently retains a noncurrent
  version on every replace or delete *(ref:*12*)*, which also breaks the `0`-precondition semantics the
  protocol depends on (§5.1).
- **Erasure is disclosed, not guaranteed.** Setting soft-delete retention to `0` removes one recovery path
  only. Retained copies can also come from Object Versioning, Bucket Lock / Object Retention Lock,
  replication or backups, and project-level recovery windows. No exact-second physical-erasure guarantee
  is claimed; the spec's "no exact-second erasure guarantee" language stands. Verifying that none of these
  mechanisms retains content in the deployed project is [BLOCKED B4]. Two propagation facts that make
  "configured" unequal to "in effect": soft-delete retention changes take up to 30 s to propagate
  *(ref:*3*)*, versioning configuration changes require ≥30 s before deletions/replacements follow the new
  setting *(ref:*12*)*, and lifecycle configuration changes take up to 24h *(ref:*4*)*.
- [DEFAULT D20] Operational logs carry an allowlisted field set only: `job_id`, `team_id`, `state`,
  `error_code`, `page_number`, counts, durations, `request_id`, `execution_ref`. Document content, OCR
  text, idempotency keys, and tokens are never logged. Error messages are fixed per code (§2.8). Log
  retention and the alerting mechanism are configuration items [BLOCKED B4].

---

## 8. Version and configuration pinning

Recorded once in `job.json` at creation and **frozen** for the job and all of its retries:

- `engine_ref`: engine id, model id, config version/hash, pre-/post-processing parameters.
- `worker_revision_digest`: the worker **image by immutable digest**, never a tag.
- `worker_job_name`: the version-pinned Cloud Run **Job** that runs that revision.
- `schema_version` and any decode parameter that affects output bytes.

**What is and is not overridable at launch** *(ref:*5*)*: `overrides` can change
`containerOverrides[].{name,args,env,clearArgs}`, `taskCount`, and `timeout` — there is **no `image`
field**, so an image cannot be selected at dispatch time. Rev 1's claim that dispatch set
`containerOverrides.image` to a digest was false and is removed.

**Feasible pinning: one job per revision** [DEFAULT D18]. Each worker revision gets its own job,
`dev-ocr-service-worker-<slug>` (short digest), created with that revision's image by digest.
`job.json.worker_job_name` records it, dispatch runs *that* job by name, and overrides carry only `env`
(job id, engine ref, decode parameters) and `timeout`.

**Guarantee scope — do not overstate.** Re-executability holds **only if** (a) the versioned job
definition is never deleted or re-pointed at a different image, (b) its image digest and layer blobs
remain in Artifact Registry, and (c) its runtime service account still exists. The platform enforces none
of this: Cloud Run jobs are updatable and deletable in place, and Artifact Registry cleanup policies can
delete a digest's blobs. "An old job id always re-runs its exact worker" is therefore an **operational
invariant, not a platform guarantee**, and its enforcement (deny job delete/update on versioned jobs, no
AR cleanup for pinned digests, alert on registry deletion) is [BLOCKED B2] as a deployment/IAM artifact.

**What is checked at dispatch (rev 4).** Because nothing in the platform prevents a version-named job from
being re-pointed, dispatch verifies the pin itself: immediately before `jobs:run`, the dispatcher re-reads
the version-pinned job and requires `containers[0].image` in its template to equal
`job.json.worker_revision_digest`. A mismatch, a missing job (`NOT_FOUND`), or a denied read (the read needs
`run.jobs.get`, [BLOCKED B2]) is the non-retryable `failed` / `engine_unavailable` case (§4.1 step 5) with an
alert. **This is detection, not a lock:** it is a read-then-act check, so a job re-pointed between the read
and the `jobs:run` call still runs
the wrong image. Closing that residual would need a platform-side conditional — the `jobs:run` request does
accept an `etag` field documented to "detect modification conflict" *(ref:*5*)*, but its run-time failure
semantics are **not documented for this call and are not assumed here**; establishing them belongs to
[BLOCKED B2]. Until then, the enforced layer is the operational guard above plus this check.
**Honest limit — the worker cannot verify its own image.** The runtime contract exposes `CLOUD_RUN_JOB`,
`CLOUD_RUN_EXECUTION`, and `CLOUD_RUN_TASK_*` *(ref:*10*)* — the job and execution names, never the image
digest — so the rule below applies to the engine/config pin and **not** to the image. Image identity is
enforced only at dispatch and by the operational guards, and a wrong-image execution that is not detected at
dispatch is not detectable by the worker itself.

- [DEFAULT D18] If the pinned job is missing at dispatch (`NOT_FOUND`) or the launch is denied
  (`PERMISSION_DENIED`), the job is finalized `failed` with `reason: engine_unavailable` and an alert is
  raised (§4.1), rather than silently retrying to the deadline.
- A worker whose resolved engine/config disagrees with `job.json` must refuse to run rather than mix
  checkpoints. Changes affect new jobs only; there is no cross-engine fallback.

---

## 9. Race and failure scenarios

1. **Duplicate submit, same key and content** — two `POST`s race on `idem`: one creates the record, the
   other gets `412` (atomic create-if-absent), re-reads under strong consistency, sees the same content,
   adopts the same `job_id`, materializes idempotently, and returns `200`. One job, one dispatch owner
   (§4.1 step 2). [CONFIRMED] behavior.
2. **Duplicate submit, same key, different content** — the loser returns `409 idempotency_conflict`; the
   original job is untouched.
3. **Crash between the idempotency claim and job creation** — the retry materializes the job at the
   recorded `job_id` (§5.2) and proceeds; a `200` never names a job that does not exist. If no retry
   happens, nothing runs.
4. **Launch returns, worker never starts, or the acknowledged operation later fails** — `acknowledged` is
   `true` but no lease exists, so reconciliation re-dispatches after ≥5 min because it never consults
   `acknowledged` (§4.1). After the second attempt the job waits for `deadline_at` and is finalized there
   (`partially_succeeded` or `failed`/`deadline_exceeded`). An execution that is merely still queued can be
   duplicated this way: that is the accepted at-least-once cost, and it can never duplicate the published
   result (§5.4). v1 does not read execution state, so "queued" and "dead" are indistinguishable here.
5. **Launch fails, or its response is lost** — `202` with `acknowledged = false`; the same reconciliation
   path. A retry of the *submission* still returns `200` with the same job, so a client-side retry can
   never create a second job.
6. **Two workers, one job** — both may run (lease expiry / lost ack window, §4.1). Their writes CAS against
   the objects they touch; whichever writes `final.json` first wins, the other gets `412` and aborts. The
   published result is fence-unique and never rewritten. Cost: duplicated work only.
7. **Crash after `result.{fence}.json`, before `final.json`** — unpublished; the reconciler finalizes and
   `finalized_at` is written once (§5.4).
8. **Crash between page checkpoints** — the next attempt lists `pages/*` and resumes; checkpoints are
   accepted only with a matching `engine_ref` (§5.5).
9. **Deadline while running** — the reconciler finalizes `deadline_exceeded`: `partially_succeeded` if
   usable pages exist, else `failed`. A straggler cannot replace the published result (§5.3, §5.4).
10. **Stale worker returns after finalization** — its `final.json` create fails with `412` **while that name
    exists**, and the published fence-unique result is never rewritten. Its `job.json` flip fails its own
    CAS. It may still write a page checkpoint or an unpublished `result.{fence}.json`; neither can change
    the published result, and cleanup removes both by enumeration (§4.3). Once cleanup has deleted the
    records the `412` no longer protects the name — that window is closed by the worker-lifetime guard
    (§4.3) and the "read `job.json` before writing" rule (§4.1), not by the arbiter.
11. **Read during/after cleanup** — before `expires_at` the result is served even if cleanup is late;
    at/after `expires_at` with records present → `410 job_expired`; after cleanup removed the records →
    `404 not_found` [CONFIRMED]. A read never serves content after `expires_at`.
12. **Cleanup partially fails** — content first, records last, idempotent and re-runnable (§4.3). No state
    exists in which records are gone but content is served.
13. **Idempotency record expired, job gone** — the same key and content creates a **new** job; dedupe is
    not guaranteed after expiry (spec), which is documented behavior, not a bug. The record is replaced by
    CAS, so two concurrent post-expiry submissions still elect one winner.
14. **Malformed, encrypted, or over-limit input** — rejected at submission (`413`/`415`/`422`), or, if
    only the worker discovers it, finalized `failed` with `reason: input_unreadable` and a page/job error —
    never an unexplained crash.
15. **Pinned job deleted or overridden** — dispatch cannot run it: finalize `failed` /
    `engine_unavailable` with an alert (§8), not an infinite retry.
16. **Job materialized without its input** (crash or storage error between the two writes) — it is never
    launched, because the dispatcher checks the input first; the claim owner compensates its own objects,
    and otherwise reconciliation finalizes it `failed` / `internal_error` **after a 5-minute grace window
    with no live lease**, then deletes the `idem/*` record conditional on its generation and `job_id`, so a
    same-key retry starts cleanly while an in-flight materialization is never finalized underneath its
    author (§5.2, §4.2).
17. **Admission counter drift or contention** — a submission that cannot advance the counter is rejected
    `429` (fail closed), so contention can never over-admit; drift can only read high (over-reject) until the
    next repair, which recomputes the count exactly from enumeration (§4.4).
18. **Concurrent reconcilers** — every action is a conditional single-object write, and `final.json`
    arbitrates, so a double finalize is impossible (§4.2, §5.4).
19. **Worker crashes holding the lease** — the lease expires and the next worker takes it over by CAS-replace
    with `fence + 1` (§5.3), resuming from `pages/*`. Rev 3 could not do this at all: `claim.json` was
    create-only, so every later acquire got `412` and a single worker crash made the job unresumable until
    `deadline_at`.
20. **Cleanup races a post-expiry re-submission with the same key** — the new submission CAS-replaces the
    expired `idem/*` record; the old job's cleanup delete then fails its `ifGenerationMatch` and is skipped,
    so the new job keeps deduplication instead of being silently exposed to a duplicate job (§4.3).
21. **Cleanup races a straggler** — no such race by construction: dispatch stops at `deadline_at`, no attempt
    that starts at or after `deadline_at` writes (§4.1), and cleanup cannot start before
    `max(expires_at, deadline_at + per_attempt_timeout)` (§4.3). This is the rule that rev 3 lacked, and it is
    what allows the deletion pass to be reported complete without a second deleter.
22. **Retry writes an input object that already exists** — `412` from the input create on the `200` path
    means the input is present, not that storage failed: the retry verifies `size` against
    `job.json.byte_length`, continues to dispatch, and compensates nothing (§5.2).
23. **Retry after a full success** — the idempotency record is still live until `expires_at`, and the input
    object was already deleted on success. The `200` path returns the existing status/result and writes
    nothing, because the job is final, so the deleted input is not re-uploaded and retention is not
    extended (§2.3, §5.2, §7).

---

## 10. Test interface

**Approved seams [DIRECTION].** Per the `tdd` rule that no test is written at an unconfirmed seam, tests
live only here:
- **S1 Public lifecycle** — submission/status/result behaviour through the HTTP API.
- **S2 Storage coordination** — the persistence adapter's conditional-write protocol: idempotency claim
  and materialization, lease/fence CAS, checkpoint/result publication, finalization arbiter, dispatch
  intent, capacity counter.
- **S3 OCR engine interface** — the application-owned recognition adapter, exercised with a fake engine.

**Proposed, not yet approved:** the worker entrypoint / job state machine as a separate seam. Its
behaviour is largely observable through S1 + S2. Ask before testing it in isolation. Where the shape of
S2/S3 is itself in question, consult the `codebase-design` vocabulary rather than inventing an interface.

- Offline tests at S1–S3: reused `TestClient` with injected dependencies, a fake recognition engine (S3),
  a fake dispatcher, an injected **fake clock** (no wall-clock sleeps), and an in-memory store that
  emulates generation preconditions and `412` (S2).
- S2 contract tests: concurrent idempotent submission, post-claim crash then retry-materialize, **retry-path
  input write returning `412` and continuing without compensation**, **`412` never deleting an adopted
  `job_id`'s record**, claim race, **lease takeover after the TTL with a crashed holder, plus the superseded
  holder's renewal failing**, lease expiry with an overlapping worker, lost dispatch ack then bounded
  re-dispatch, **acknowledged-but-dead execution re-dispatched and the job finalized at `deadline_at` after
  the attempt cap**, **dispatch-time image/pin mismatch → `failed`/`engine_unavailable`**, **a worker that
  cannot read `job.json` writing nothing**, non-retryable dispatch error, crash between writes at each row of
  §5.5, restart-resume, partial cleanup, **a cleanup delete skipped because the idempotency record was
  replaced after the read**, **cleanup refused before the worker-lifetime guard**, **cleanup deleting an
  unpublished `result.{fence}.json` and a leaked `claim.json` by enumeration**, **a `200` hit on a final job
  materializing, dispatching, and re-uploading nothing**, finalization race, counter drift repair.
- S2 fault tests for the two rev 5 blockers: **an attempt that starts at or after `deadline_at` writes
  nothing**, **the cleanup guard equals `max(expires_at, deadline_at + per_attempt_timeout)` and the pass is
  refused before it**, **D32 is asserted at startup (a configuration with
  `per_attempt_timeout + cleanup_schedule_period > 1 h` fails to start)**, **a straggler that writes up to
  `deadline_at + per_attempt_timeout` is removed by the pass and writes nothing after it**, **admission
  fails closed: CAS exhaustion yields `429` and no job object is created (no fail-open path)**, **release
  happens exactly once — a second finalizer or a reconciler repair cannot double-decrement**, **a leaked
  over-count is repaired to the exact enumerated value**, **a repair racing an un-persisted acquisition
  under-counts by at most the number of in-flight acquisitions and the next pass restores it**.
- S1 contract tests, guarantee statement: a job with two dispatched executions still yields exactly one
  result object with one `finalized_at` and `schema_version: ocr.v1`; nothing in the public contract exposes
  attempt counts, so "one logical job, one published result, at-least-once execution" is asserted through
  observable outcomes only (§4.1).
- S1 contract tests: exact keys and types; 1-based contiguous pages with one entry per source page;
  combined-text order and separator rules; failed-page visibility; warning-vs-error separation;
  `accuracy == "not_verified"`; `reason` on the deadline path; absence of structured expense fields;
  required `Idempotency-Key` rejection; foreign-job `404`; `410` at `expires_at` and `404` after the
  records are removed; `429` and `Retry-After`.
- Tests never assert internal call counts and never assert through a side channel.
- **Fakes do not prove real GCS behaviour** *(ref:*1*,*2*)*. The items in [BLOCKED B1] must be exercised
  against real GCS in an explicitly approved budget before any reliability claim.
- Engine benchmark (separate, for engine selection): CPU-only, labelled expected text on selected pages,
  accuracy reported **separately** from schema/integration success; corpus, metrics, normalization, and
  thresholds frozen before comparing candidates. All numeric limits in this document are [BLOCKED B3]
  behind it.

---

## 11. References (official Google Cloud documentation, read 2026-11-08)

1. Request preconditions — `ifGenerationMatch`/`ifMetagenerationMatch`, `412`; the `0` special case
   ("only proceeds if no object with the specified name exists in the bucket or if there are only
   noncurrent versions of the object in the bucket"; a live version → `412`). Also: use **both** generation
   and metageneration preconditions together.
   https://docs.cloud.google.com/storage/docs/request-preconditions
2. Cloud Storage consistency — strong object read-after-write / read-after-metadata-update /
   read-after-delete, and strongly consistent listing; single-object atomicity; batch and multi-object
   operations are not atomic.
   https://docs.cloud.google.com/storage/docs/consistency
3. Soft delete — enabled by default, default retention 7 days (7–90; `0` disables); "preserves objects and
   buckets that get deleted **or overwritten**"; soft-deleted objects "cannot be read, modified, or
   overwritten"; cost caution for buckets holding temporary data; retention changes propagate in up to
   30 s.
   https://docs.cloud.google.com/storage/docs/soft-delete
4. Object Lifecycle Management — `Delete`/other actions; configuration changes "can take up to 24 hours to
   go into effect".
   https://docs.cloud.google.com/storage/docs/lifecycle
5. Cloud Run Admin API `jobs.run` — `POST /v2/{name}:run`; body `{validateOnly, etag, overrides}`;
   `Overrides = {containerOverrides[]{name,args[],env[],clearArgs}, taskCount, timeout}` with **no
   `image`**; sending `overrides` requires `run.jobs.runWithOverrides`; response is an `Operation`.
   https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run
6. Job executions resource — `createTime` "acknowledged by the execution controller. It is not guaranteed
   to be set in happens-before order across separate operations"; a `cancel` method exists (not used here).
   https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions
7. Task timeout — default 10 minutes, max 168 hours, applies per attempt; "It is not a guarantee of
   uninterrupted execution"; non-GPU tasks are live-migrated (`SIGTSTP` 10 s before, `SIGCONT` after),
   GPU tasks are terminated instead.
   https://docs.cloud.google.com/run/docs/configuring/task-timeout
8. Maximum retries — per task, default 3, integer 0–10; `0` means tasks run once.
   https://docs.cloud.google.com/run/docs/configuring/max-retries
9. Jobs retries and checkpoints best practices — use checkpointing so a restarting task resumes from its
   last saved state; check for checkpoints on startup; keep job tasks idempotent.
   https://docs.cloud.google.com/run/docs/jobs-retries
10. Container runtime contract — jobs must exit 0 on success and non-zero on failure and must not listen on
    a port; the `CLOUD_RUN_JOB`, `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_ATTEMPT`,
    `CLOUD_RUN_TASK_COUNT` environment variables for jobs.
    https://docs.cloud.google.com/run/docs/container-contract
11. Authenticating service-to-service — Google-signed ID token, `aud` = the receiving service URL (or a
    custom audience); caller needs `roles/run.invoker`; token sent as `Authorization: Bearer` or
    `X-Serverless-Authorization: Bearer`, where the latter "removes the signature before passing the token
    to the user container"; if both are present only `X-Serverless-Authorization` is checked.
    https://docs.cloud.google.com/run/docs/authenticating/service-to-service
12. Object Versioning — "retains a noncurrent object version each time you replace or delete a live object
    version"; noncurrent versions are readable and charged; in a soft-delete bucket, deleting a noncurrent
    version makes it soft-deleted; configuration changes need ≥30 s before behaviour follows.
    https://docs.cloud.google.com/storage/docs/object-versioning
13. About instance autoscaling — when a `max-instances` limit is reached, requests pend for up to 3.5×
    average startup time or 10 s, whichever is greater, and then fail with `429`.
    https://docs.cloud.google.com/run/docs/about-instance-autoscaling
14. Set parallelism for jobs — the maximum number of tasks in one execution that run in parallel.
    https://docs.cloud.google.com/run/docs/configuring/parallelism

---

## 12. Resolved decisions (proposed defaults)

Every row is a decision this document makes so that implementation can start. Rows marked [DEFAULT] are
**not** user-approved and **not** verified; they are the veto list.

| ID | Decision | Provenance |
|---|---|---|
| D1 | A duplicate submission retry (same team + key + content) returns the existing job and never starts a second job or owns a dispatch attempt. | **[CONFIRMED]** |
| D2 | No cancellation endpoint in v1; no execution `cancel` call. Runaway work is bounded by `deadline_at`, task timeout, and the 2-attempt dispatch cap. | **[CONFIRMED]** |
| D3 | `404` after cleanup is acceptable; no stub is kept solely to make `410` permanent. | **[CONFIRMED]** |
| D4 | rev 2 design direction and seams S1–S3. | **[DIRECTION]** |
| D5 | `Idempotency-Key` is a **required** header, 8–128 chars of `[A-Za-z0-9._:~-]`, scoped to `team_id`. The user approved the behavior, **not** this header requirement. | [DEFAULT] |
| D6 | Persist-first ordering; failed launch → `202` with `acknowledged=false`; `503` only for pre-persistence failure (`storage_unavailable`). | [DEFAULT] |
| D7 | Dispatch = direct `jobs:run` on the pinned revision job + Cloud Scheduler reconciliation + Cloud Scheduler cleanup. No queue service. | [DEFAULT] |
| D8 | State enum `accepted/running/succeeded/partially_succeeded/failed` (no `queued`); dispatch ack lives in `dispatch.json`; `meta.json`+`state.json` merged into `job.json`. | [DEFAULT] |
| D9 | Claim-then-materialize idempotency, with retry-path materialization that repairs the claim-to-persist crash window. A job is usable only when `job.json` **and** its input object exist; an input-write failure returns `503` with compensating removal, and reconciliation finalizes any job left without an input. | [DEFAULT] |
| D10 | One expiry rule: `410 job_expired` while the job records exist, `404 not_found` after cleanup removed them; `expires_at` computed from `final.json.finalized_at`. | [DEFAULT] |
| D11 | Embedded-text usability: PDF only, ≥1 non-whitespace character after NFC + trim, no `U+FFFD`; else `ocr`. | [DEFAULT] |
| D12 | In-app ID-token re-verification via the official library; `Authorization: Bearer` only (`X-Serverless-Authorization` rejected because its signature is stripped); 60 s skew. | [DEFAULT] |
| D13 | Admission ceiling via one CAS counter object, **fail-closed** (CAS exhaustion → `429`, never admit unaccounted), release exactly once by the `final.json` winner, exact enumeration-based repair; `429` with `Retry-After: 30`. | [DEFAULT] |
| D14 | `max-retries = 2`; ≤2 dispatch attempts with ≥5 min spacing, a lease check and `now < deadline_at` (never gated on `acknowledged`); non-retryable dispatch errors finalize `failed`/`engine_unavailable` + alert. | [DEFAULT] |
| D15 | Lease TTL 5 min, heartbeat 60 s; acquire is create-if-absent, an expired lease is **taken over by CAS-replace** with `fence + 1`, renewal is `ifMetagenerationMatch` and a `412` stops the holder, release deletes with `ifGenerationMatch`; checkpoints record the fence for diagnostics only. | [DEFAULT] |
| D16 | Result published as fence-unique `result.{fence}.json`, `final.json` arbitrates; correctness does not depend on fence-tagged resumption. | [DEFAULT] |
| D17 | Bucket: soft-delete retention `0`, Object Versioning disabled, no Bucket Lock, no Object Retention Lock, no lifecycle rule as the deletion mechanism. | [DEFAULT] |
| D18 | One Cloud Run job per worker revision, image by digest; dispatch re-reads that job and requires its template image to equal the pinned digest before `jobs:run` (detection, not a lock); pinned-job `NOT_FOUND`/`PERMISSION_DENIED`/image mismatch → `failed`/`engine_unavailable` + alert. | [DEFAULT] |
| D19 | `engine_ref` is operator-only; not exposed to callers. | [DEFAULT] |
| D20 | Allowlisted log fields only; fixed per-code error messages; no content, keys, or tokens. | [DEFAULT] |
| D21 | Provisional limits (not evidence-based): 50 MiB, 500 pages, 30 min per attempt, API `max-instances` 4, worker parallelism 1, ceiling value. | [DEFAULT] |
| D22 | Non-success finalization carries `reason` ∈ {`deadline_exceeded`, `engine_unavailable`, `input_unreadable`, `internal_error`}; `null` on success. | [DEFAULT] |
| D23 | Text rules: per-page trim; combined = successful pages joined by `\n`; empty success `text: ""` + `no_text`; failed page `text: null` + `error_code`. | [DEFAULT] |
| D24 | One page entry per PDF page / TIFF frame; JPEG and PNG are single-page. | [DEFAULT] |
| D25 | An idempotency record is live while the job is non-final or `now < expires_at`; absent or expired → new job (record replaced by CAS). | [DEFAULT] |
| D26 | Cleanup is two passes (content, then records) over **enumerated prefixes**, every delete carrying `ifGenerationMatch` (the `idem/*` record is additionally matched by `job_id`), idempotent and re-runnable, and refused before `max(expires_at, deadline_at + per_attempt_timeout)`; full-success input deleted immediately after finalization. | [DEFAULT] |
| D27 | `AGENTS.md` / README / five-template-contract alignment and retirement of `POST /v1/documents:process` happen inside the approved implementation, not in this ticket. | [DEFAULT] |
| D28 | Reconciliation also dispatches materialized non-final jobs with no `dispatch.json`. | [DEFAULT] |
| D29 | An input-object `412` means "already present" (verified against `job.json.byte_length`) and never triggers compensation; compensation is limited to the claim owner's own objects and its own `idem/*` record, matched by generation; reconciliation's missing-input finalization waits 5 min with no live lease and deletes the record conditionally. | [DEFAULT] |
| D30 | A worker reads `job.json` before writing any object and writes nothing if that read fails; it deletes `claim.json` on completion or give-up, and cleanup deletes a lease that was never released. | [DEFAULT] |
| D31 | `dispatch.acknowledged` and `dispatch.execution_ref` are diagnostic fields: no decision in this design reads them. | [DEFAULT] |
| D32 | Configuration invariant `per_attempt_timeout + cleanup_schedule_period ≤ 1 h`, asserted at startup; it is what keeps the §4.3 worker-lifetime guard within 1 h of `expires_at` and therefore what makes the ≤1 h cleanup target achievable. Provisional: 30 min + 15 min = 45 min. | [DEFAULT] |

Removed from rev 2: `queued` state (D8), `meta.json`/`state.json` split (D8), `dispatch_unavailable` and
`job_not_finalized` error codes (§2.8), the `containerOverrides.image` claim, the cross-object fence rule,
and the `410`-is-independent-of-cleanup claim.

---

## 13. Remaining blockers (the open list)

Nothing on this list can be resolved by writing more design. Everything not on this list is decided above.

| ID | Blocker | Why it blocks | What closes it |
|---|---|---|---|
| **B1** | **Real GCS protocol verification.** (a) Does `ifGenerationMatch=0` succeed when only a *soft-deleted* version of the name exists? (b) Do concurrent CAS writes to one object reliably yield `412` as the protocol assumes, including concurrent create-if-absent? (c) Does a `0`-precondition create actually fail against a *noncurrent* version (the documented reason Object Versioning must be off)? (d) Does a **conditional delete** (`ifGenerationMatch`, documented as applying to mutating requests "such as uploads, deletes, or metadata updates") return `412` against an object replaced since the read, as §4.3's cleanup and §5.3's release assume? | §5 rests on documented semantics, and the soft-deleted case is **not documented at all**. Until this is exercised, §5 is a design, not a verified protocol. | An approved cloud test budget with a scratch bucket; sustained by issue #1's requirement not to treat fakes as cloud acceptance. |
| **B2** | **Credentials, IAM, and deployment facts.** Does the API service account hold `run.jobs.runWithOverrides` (not just `run.jobs.run`), and `run.jobs.get` for the dispatch-time pin check (§8)? Can the worker SA write the bucket? What is the real n8n identity-token path (on-GCP metadata server vs external `generateIdToken`/WIF)? Do the bucket, prefix, versioned jobs, and AR retention/digest-guard actually exist? What are the run-time semantics of the `jobs.run` `etag` field (an unverified candidate for closing the §8 read-then-act window)? Is reading execution state (`jobs.executions.get`, §4.1) wanted for earlier stranded-execution classification? | §4.1, §6.3, and §8 assert permissions and identities that nothing has verified, and two rev 4 mechanisms are gated on permissions that do not exist yet. Currently there is no deployment. | Scoped approval for provisioning and a live IAM/token check. |
| **B3** | **CPU benchmark and every numeric limit.** Engine selection, max bytes, max pages, per-attempt timeout, worker CPU/memory, concurrency ceiling, retry counts, lease/heartbeat intervals — and the D32 invariant `per_attempt_timeout + cleanup_schedule_period ≤ 1 h`. | The issue explicitly gates numeric limits on the benchmark; the values in §12/D14/D15/D21/D32 are placeholders so implementation can start, not evidence. **If the benchmark needs `per_attempt_timeout + cleanup_schedule_period > 1 h` (e.g. a per-attempt timeout above ~45 min), the ≤1 h cleanup target and the stale-worker guard conflict and the user must choose between them; the design will not silently prefer one.** | The approved CPU benchmark (spec "Release-blocking technical work" #1). |
| **B4** | **Retention/erasure and operational configuration verification.** Actual soft-delete policy state, Object Versioning state, absence of Bucket Lock / Object Retention Lock, absence of replication/backups, project-level recovery windows, cleanup alerting, and log retention. | §7 claims erasure is *disclosed, not guaranteed*; which mechanisms are active is a property of the deployed project. | Approval to inspect/provision the real bucket; then a read-back of each setting. |
| **B5** | **User approval of the frozen contract and of the [DEFAULT] rows**, plus a cloud test budget for B1/B2/B3/B4. | Issue #1 AC #7 requires approval of technical and testing choices before this design is "reviewed and implementation-ready". This document supplies the choices; only the user can approve them. | Explicit approval per row group (contract §2, protocol §5, defaults §12) recorded on issue #1. |
| **B6** | **Exact or high-throughput admission ceiling.** GCS-only state offers one coordination object (`capacity/inflight.json`); it is a serialization point, so sustained contention surfaces as fail-closed `429` and a repair racing an un-persisted acquisition can transiently under-count. | §4.4's ceiling is bounded but not exact under contention, and its throughput is bounded by the single object. An exact counter at higher submission rates needs a transactional store (Firestore/Spanner), which the GCS-only constraint excludes. | A decision to accept the single-object bound, or approval to add a transactional counter. |

B5 and B6 are decision blockers; B1–B4 are evidence blockers and none of them can be retired by further
drafting. Issue #1 has no issue-level blockers (`blocked-by: none`); it blocks #3.

---

## 14. Implementation notes

Order follows the seams and keeps the design honest: engine adapter first (S3), storage protocol second
(S2), public lifecycle third (S1), worker last.

1. S3: the recognition adapter behind a fake-engine-tested interface, including checkpoint-shaped
   per-page output and the `embedded`/`ocr` decision of §2.7.
2. S2: the storage adapter — claim/materialize, lease+fence CAS, checkpoint publication, `final.json`
   arbitration, dispatch intent, capacity counter — tested against the in-memory fake, with the §5.5 crash
   matrix as the test list.
3. S1: the HTTP lifecycle, the closed error vocabulary, the expiry gate, and the required
   `Idempotency-Key`.
4. Worker entrypoint + the pinning/refusal rules of §8.
5. Only then: B1/B2 verification against real GCP in an approved budget, then AC #8's project-instruction
   alignment (D27).

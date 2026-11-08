# OCR v1 — implementation-ready contract (rev 6, amended by the design commit)

Issue: `TU-thanakrit/dev-service-google-doc-ai` #1 *OCR v1: freeze contracts and recovery design*.
Worktree `/home/danny/service_google_doc_ai/.worktrees/ocr-v1-contracts`, branch
`task/ocr-v1-contracts`. Development/testing target: personal project `proud-quasar-351116`, account
`pb.danny@gmail.com`, region `asia-southeast1`, new-resource prefix `dev-ocr-service`. Corporate
`thaiunion-ai-platform` stays a separate future deployment.

rev 6 replaced the rev 5 draft (`docs/ocr-v1-technical-design.md`), which is kept as history and marked
superseded. That replacement added no capability: every change was a contract correction, a removed
contradiction, or a claim downgraded to what can be proven.

**Amendment (design commit).** §2–§6, the normative contract, are unchanged. This amendment records the
user's approval (§1.1), corrects the acceptance-criteria status to match it, commits the approved spec
(`docs/ocr-service-spec.md`) and `GLOSSARY.md` alongside this file, and marks the uncommitted draft
rules/tests (§11.1). No capability is added and no number is claimed. It does **not** apply the
`AGENTS.md` alignment of issue #1 AC #8: that revision is prepared outside the repository
(`/home/danny/.hermes/cache/scratch/ocr-v1-proposed-repo-instructions.md`) because the in-repo write was
refused by the local protected-file guard and needs the user's interactive approval — see the AC table.
The copied spec is byte-identical to the approved local original, so its status line ("technical design
and testing proposals below require review") predates this approval: §1.1 is the current status.

Labels: **[CONFIRMED]** user-confirmed · **[DIRECTION]** approved as a direction, not verified ·
**[DEFAULT]** this document's own choice — approved as part of the design (§1.1), individually
vetoable, not verified (veto list §12) · **[BLOCKED]** needs real GCP evidence, a benchmark, or approval
(§13).

Reading order: **§1–§8 are the normative contract** (what to build, and what must stay true).
**§0 and §9–§15 are informative** — change log, race review, test list, decision/veto table,
evidence gaps, references. If you want the short version, §2–§8 is it (~600 lines).

**Approval and verification status.** The user approved the technical design in this document and the
testing seams S1–S3, and requested implementation (§1.1). Nothing cloud-side is verified: no cloud
call, no GCP write, no paid run, no deployment, no push, and no resource exists — B1–B4 remain evidence
blockers (§13). Verified locally for the design commit: `uv run pytest -q` → **66 passed** (the 30
existing tracked tests plus the 36 uncommitted draft tests of §11.1) and `uv run ruff check .` → clean.
The 30 existing tests are baseline evidence for the *old* synchronous app, not evidence that this
design works. Three load-bearing Google Cloud facts were re-read while drafting (request preconditions,
soft delete, Cloud Run request limits); the rest is cited from the rev 5 reference list (§14).

---

## 0. What rev 6 changes

The design-commit amendment (front matter, §1.1) is not listed below: it changes no contract text. This
section covers the rev 5 → rev 6 step only.

| # | Contradiction or gap in rev 5 | Resolution |
|---|---|---|
| 1 | The expiry gate read `expires_at` from `final.json`, which cleanup deleted before `job.json`, so a reader mid-pass had an undefined expiry. | **D33**: `job.json` carries the final-state projection `{state, finalized_at, expires_at, reason, fence}`; the gate reads it before any content, and `job.json` is deleted last among the job's objects. |
| 2 | Admission accounting was unspecified on the idempotent-retry path, where a retry would have incremented a counter for a job it does not create. | **D34**: the claim decides first; only the actor that **created** the record may acquire an admission. |
| 3 | A leaked admission could read high, but the direction of drift was never stated. | §6.4: only the arbiter decrements; abandoned admissions are corrected by repair. Drift is one-directional (over-count = over-reject) except the disclosed repair race. |
| 4 | Two CAS primitives were mixed (`ifGenerationMatch`, `ifMetagenerationMatch`), and renewal versus takeover was distinguishable only by re-reading the object. | **D36**: one primitive, `ifGenerationMatch` on the object being written, no metadata-only updates; renewal and takeover are the same conditional replace. |
| 5 | The 50 MiB provisional upload limit was unreachable: Cloud Run caps an HTTP/1 request at 32 MiB and the limit cannot be raised. | **D38**: ≤32 MiB by construction, 20 MiB provisional, or an explicit decision to serve HTTP/2. |
| 6 | Dedupe identity included the declared media type, so identical bytes with a different declared type would have been a `409`. | **D35**: content identity is `(SHA-256(bytes), byte_length)`; the declared type is a validation gate only. |
| 7 | `GET /v1/ocr/jobs/{id}` had no response table. | §2.2. |
| 8 | Per-team GCS IAM was implied by "`team_id` is the first path segment so IAM is team-scoped". | §7.3: isolation is application-enforced with one bucket-wide service account; that is the honest bound. |
| 9 | `final.json` versus `job.json` authority on the read path was ambiguous. | §3.3: `final.json` is authoritative for *state*, `job.json` for *existence and expiry*; divergence is repaired. |
| 10 | The deadline/cleanup arithmetic was argued in prose rather than checked. | The rev 5 formulas are kept verbatim and are now asserted functions with boundary tests (§11.1). |

---

## 1. Scope and issue #1 map

Fixed by the approved v1 spec, committed alongside this document as `docs/ocr-service-spec.md` (domain
language in `GLOSSARY.md`): OCR-only; internal multi-team; n8n first;
printed Thai/English; PDF/JPEG/PNG/TIFF; async Cloud Run API + Cloud Run Jobs workers; GCS-only
application state; GCP identity auth; no classification or structured extraction; shared capacity with
per-team data isolation; one operator-pinned engine per job; 24 h maximum lifetime from acceptance;
inputs deleted on full success; other data expired one day after finalization; API access denied at
expiry. Out of scope: callbacks, direct-to-GCS upload, caller engine selection, batching,
cancellation, Entra, Cloud SQL.

Frozen here: routes, statuses, error vocabulary, result shape, page rules, job states and timestamps,
idempotency and ownership, the GCS conditional-write protocol, dispatch/reconciliation/cleanup,
verified identity, version pinning, and the test seams.

**Cancellation is absent by decision** [CONFIRMED]: no caller or operator cancel route, no call to the
Cloud Run execution `cancel` method. Runaway work is bounded only by `deadline_at`, the per-attempt
task timeout, and the ≤2 dispatch attempts.

### 1.1 Approval record

Recorded explicitly because it is the one input that cannot be re-derived from the document:

- **Approved:** the technical design in §2–§10, and the testing seams S1–S3 in §11. The user also
  requested implementation of the approved scope.
- **Issue #1 AC #7's "obtain approval for technical and testing choices" is therefore met.**
- **Not approved, and not implied by that approval:** every numeric value (benchmark-gated [B3], §10);
  the disclosed residuals — capacity under-count [B6], at-least-once execution with no single-execution
  guarantee (§5.3, §9), the read-then-act pin window (§8), erasure disclosed rather than guaranteed
  (§10); and any cloud behaviour [B1–B4].
- **[DEFAULT] rows (§12) are this document's own choices.** They are approved as part of the design as a
  whole, but a row whose value is a placeholder for a benchmark result stays benchmark-gated: approving
  the design is not evidence for the number.

| Issue #1 acceptance criterion | Status |
|---|---|
| Versioned examples, routes, statuses, error codes, page numbering, text source, warnings, partial completeness, expired-result behavior, execution-vs-accuracy | Specified exactly, §2 (`schema_version` in every request-scoped body, §2.4–§2.7); approved (§1.1). Execution success is separated from accuracy by the constant `accuracy: "not_verified"`. |
| Transitions, one authoritative final-state timestamp, acceptance/deadline/expiry timestamps, idempotency conflicts, request/cleanup races | Specified, §3–§5, §9; approved (§1.1). |
| Concrete dispatch/reconciliation/cleanup; launch ack vs completion | Specified, §6; approved. Acknowledgment is diagnostic and no decision reads it (§6.1). |
| GCS conditional-write, claim/lease/fencing, checkpoint/result publication, idempotency, crash recovery, no cross-object transactions | Specified, §5; approved as design. **Integration verification pending — real-GCS behaviour unverified [B1]**; an evidence gap, not a specification gap. |
| Verified GCP caller→team mapping; headers must not grant identity | Specified, §7; approved as design. **Integration verification pending — deployment evidence unverified [B2]**. Identity comes only from a re-verified ID token; no identity header is read. |
| Engine/model/config and worker-revision pinning, executable across deployments | Specified, §8, including what is *not* guaranteed; approved as design. **Integration verification pending — enforcement unverified [B2]**. |
| Race/failure review with examples; approval of technical/testing choices; numeric limits benchmark-gated | Race review done, §9 (23 scenarios). Technical and testing choices approved (§1.1). Every numeric engine-dependent limit stays benchmark-gated [B3] — not approved and not claimed. |
| Align project instructions with the async OCR-only scope | **Prepared, not applied.** The intended revision (planned asynchronous OCR-only scope recorded; the existing synchronous expense app kept and described accurately as the current implementation; baseline commands, verification rules, and security rules preserved) is written to `/home/danny/.hermes/cache/scratch/ocr-v1-proposed-repo-instructions.md`. The in-repo `AGENTS.md` write was refused by the local protected-file guard, which requires the user's interactive approval, so the repository's instruction file is still unaligned. AC #8's precondition does hold: no code contradicting the original synchronous/template rules was introduced by this commit. The README and the retirement of `POST /v1/documents:process` belong to the implementation tickets (D27). |

**Issue #1's design criteria are met, with one instruction-file edit outstanding.** AC #1–#7 are
answered at design level and the technical and testing choices are approved (§1.1). AC #8 is prepared and
its precondition (no contradicting code) holds, but the `AGENTS.md` edit itself was refused by the local
protected-file guard and awaits the user's approval; the prepared text is outside the repository, as the
table says. What is **not** met — and is not a design gap — is evidence: B1 (real-GCS protocol), B2
(IAM/token/pinning), B3 (the numbers, benchmark-gated by the criterion itself), B4 (retention
configuration), B6 (capacity exactness). No cloud behaviour is proven by this document or by the commit
that adds it.

---

## 2. Public contract

### 2.1 Routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/ocr/jobs` | Submit one document; return job identity |
| `GET` | `/v1/ocr/jobs/{job_id}` | Status: state, timestamps, dispatch, opportunistic progress |
| `GET` | `/v1/ocr/jobs/{job_id}/result` | Final result, or the not-final / expired signal |
| `GET` | `/health` | Liveness; no cloud client, no identity check. Unreachable without a valid ID token, since the service is not public (§7.1) |

No update, cancel, delete, or list route in v1. Framework `404`/`405` for unknown paths or methods
carry no `schema_version` body and are outside this contract.

### 2.2 Statuses

`POST /v1/ocr/jobs`:

| Status | When | Body |
|---|---|---|
| `202` | New job: record and input durably persisted, launch attempted and its intent recorded | Status object |
| `200` | Idempotent hit: same key, same content, job not expired | Existing status object |
| `400` | Malformed request, or `invalid_idempotency_key` | Error |
| `401` | No credential, or the presented ID token fails in-app verification | Error |
| `403` | Verified caller not mapped to a team | Error |
| `409` | Same key, different content | Error `idempotency_conflict` |
| `413` / `415` / `422` | Size / declared type / content unusable | Error |
| `429` | Admission ceiling reached, or admission unrecordable | Error `capacity_exceeded`, `Retry-After: 30` |
| `503` | No usable job exists and the partial record was removed, so the same key is retryable | Error `storage_unavailable`, `Retry-After: 5` |

No `404` on this route: the idempotency record is team-scoped, so a submission only ever resolves a
job of the caller's own team. `202` is a launch acknowledgment, not evidence that a worker started,
that the platform accepted the launch, or that OCR succeeded; `202` with `acknowledged = false` is
normal.

`GET /v1/ocr/jobs/{job_id}` — evaluate the gate (§3.3) first:

| Status | When |
|---|---|
| `200` | Job record exists and `now < expires_at` (any state) → status object |
| `404` | Unknown id, foreign-team id, or records removed by cleanup → `not_found` |
| `410` | Job record exists and `now ≥ expires_at` → `job_expired` |

`GET /v1/ocr/jobs/{job_id}/result` — same gate, then:

| Status | When |
|---|---|
| `200` | Job final and `now < expires_at` → result object |
| `202` | Job not final (`accepted`/`running`) → status object, `Retry-After: 10` |
| `404` | Unknown, foreign, or cleaned up → `not_found` |
| `410` | Records exist and `now ≥ expires_at` → `job_expired` |

Never `403` for a foreign or cleaned-up job, so job ids cannot be probed across teams; that holds
only because lookup is strictly team-prefixed (§5.7, §7.3). `202` does not depend on `deadline_at`: a
job past its deadline is finalized by reconciliation, and until that pass completes the honest answer
is "not final". A missing published result *after* the gate passed cannot come from cleanup (cleanup
never starts before `expires_at`): that is `500 internal_error` plus an alert.

### 2.3 Submission request

`multipart/form-data`: one `file` part plus headers. The declared part `Content-Type` must be
`application/pdf`, `image/jpeg`, `image/png`, or `image/tiff`, **and** it must match the sniffed
container — bytes decide, never the declared type.

| Header | Rule |
|---|---|
| `Idempotency-Key` | **Required.** Opaque, 8–128 chars from `[A-Za-z0-9._:~-]`, scoped to the verified `team_id`. Violation is `400 invalid_idempotency_key`. |
| `X-Request-Id` | Optional correlation id, echoed in bodies and logs. Never used for identity, dedupe, or storage paths. |

[DEFAULT D5] The required header is this design's choice: a duplicate retry must not start duplicate
processing, and a caller-supplied key is the only way to recognize a duplicate without storing content
in an index. The user approved the *behavior*, not the header. The raw key never reaches a path or a
log; the record path is `idem/{team_id}/{SHA-256(team_id + "\n" + key)}`.

Validation gates, in order, on bytes: empty body or zero bytes → `422 empty_document`; container not
PDF/JPEG/PNG/TIFF (including archive and Office magic) or declared-type mismatch → `415
unsupported_media_type`; byte length over the limit → `413 file_too_large`; page/frame count over the
limit → `422 too_many_pages`; PDF trailer containing `/Encrypt` → `422 encrypted_document`; header
present but no pages/trailer → `422 malformed_document`.

### 2.4 Status object

```json
{
  "schema_version": "ocr.v1",
  "job_id": "6d1f9b2a-…",
  "state": "accepted|running|succeeded|partially_succeeded|failed",
  "timestamps": {"accepted_at": "2026-11-08T04:31:07Z", "deadline_at": "2026-11-09T04:31:07Z",
                 "finalized_at": null, "expires_at": null},
  "dispatch": {"acknowledged": false, "execution_ref": null},
  "progress": {"pages_total": 12, "pages_done": 5}
}
```

`execution_ref` is the Cloud Run execution name from the launch call, not completion.
`acknowledged = false` covers both "no launch accepted yet" and "persisted after a failed launch"; it
is **diagnostic only** [D31] — no decision reads it, which §11.1 asserts. `progress` is opportunistic
and may be absent; `pages_done` counts pages with a durable checkpoint. Attempt counters, lease state,
and engine identifiers are absent [D19].

### 2.5 Result object

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
  "pages": [ /* §2.6 */ ],
  "warnings": ["no_text"],
  "timestamps": {"accepted_at": "…", "finalized_at": "…", "expires_at": "…"}
}
```

- `reason` is `null` on success, else one of `deadline_exceeded`, `engine_unavailable`,
  `input_unreadable`, `internal_error` [D22].
- `completeness = "complete"` iff the job state is `succeeded` and every page is `succeeded`.
- `accuracy` is the constant `"not_verified"`: execution success is not accuracy (AC #1). No
  confidence score is invented anywhere in this contract.
- `text` is present only when at least one page succeeded: the successful pages' `text` joined by a
  single `\n` in ascending `page_number`; failed pages contribute nothing and add no separator.
- `warnings` is the union of page warnings in page order, deduplicated.
- Timestamps come from `final.json`; `expires_at = finalized_at + 24 h`, stored there for audit and
  computed on read from the same value.
- No engine identifier and no expense/classification field appears here.

### 2.6 Page object

```json
{"page_number": 1, "status": "succeeded|failed", "text_source": "embedded|ocr",
 "text": "…", "warnings": ["no_text", "possible_low_quality"], "error_code": null}
```

- `page_number` is 1-based, contiguous, in document order; one entry per source page (PDF page, TIFF
  frame, the single page of a JPEG/PNG) [D24]. Failed pages are always present as `status: "failed"`
  with an `error_code`; they are never dropped.
- `text_source` [D11]: `embedded` only when the input is a PDF **and** its extracted text layer,
  after NFC normalization and trimming, has ≥1 non-whitespace character **and** contains no `U+FFFD`;
  otherwise `ocr`. Non-PDF inputs are always `ocr`. Mechanical so it is testable; refining it changes
  a [DEFAULT], not the contract. Neither source is automatically authoritative.
- Text rules [D23]: per-page trim; a `succeeded` page with no text has `text: ""` plus warning
  `no_text`; a `failed` page has `text: null` and an `error_code`. Warnings are quality signals,
  `error_code` is an execution failure; never conflated.

### 2.7 Error vocabulary (closed set) and versioning

`unsupported_media_type`, `file_too_large`, `too_many_pages`, `empty_document`, `malformed_document`,
`encrypted_document`, `invalid_idempotency_key`, `idempotency_conflict`, `unauthorized_caller`,
`team_not_authorized`, `not_found`, `job_expired`, `capacity_exceeded`, `storage_unavailable`,
`deadline_exceeded`, `page_engine_error`, `engine_unavailable`, `internal_error`.

```json
{"schema_version": "ocr.v1",
 "error": {"code": "file_too_large", "message": "file exceeds the size limit", "request_id": "…"}}
```

`message` is a fixed allowlisted string per code: no paths, bucket, job, processor, or token.
`deadline_exceeded`, `engine_unavailable`, and `input_unreadable` are also `reason` values and never
HTTP error codes. Codes and messages live in `app/ocr_contract.py` and are asserted (§11.1), so a new
code cannot appear by accident. `schema_version` (`"ocr.v1"`) appears in every request-scoped body and
in `job.json`; breaking shape changes bump it, additive optional fields do not, and replacing the
recognition engine does not change this contract.

---

## 3. Lifecycle, timestamps, expiry gate

```
accepted ──► running ──► succeeded
   │             │   └──► partially_succeeded
   │             └──────► failed      (engine error, unreadable input, deadline)
   └────────────────────► failed      (nothing ran before the deadline)
```

`accepted` is written with `job.json`; `running` is set by the first worker holding a valid lease;
final states are set only through the `final.json` arbiter (§5.4). `queued` is not in the enum [D8]:
it was not required, its producer was a conditional write that might never happen, and its
information is the diagnostic `dispatch.acknowledged`.

- `accepted_at` — server-set once at `job.json` creation; start of the 24 h lifetime.
- `deadline_at = accepted_at + job_lifetime` [DEFAULT 24 h ceiling]. At this instant further processing
  stops and the job finalizes `reason: deadline_exceeded`: `partially_succeeded` if usable page results
  exist, else `failed`.
- `finalized_at` — the **one** authoritative final-state timestamp, written once inside `final.json`;
  the retention clock starts here, not at upload.
- `expires_at = finalized_at + retention` (24 h), stored in `final.json` and projected into `job.json`
  (D33). `null` until finalization; a job pending past `deadline_at` is finalized by reconciliation, so
  it is always eventually set.

**Exactly-once finalization** is create-if-absent on `final.json`: the first writer wins, every other
writer gets `412` and aborts. That — not the lease and not a fence — is what stops a stale worker from
replacing a final result, and it holds only while that object exists (§5.4). **Execution is
at-least-once**: the guarantee is one logical job per `(team_id, key)` and at most one published
result, never one execution per job.

### 3.3 One expiry rule

Evaluated **from `job.json`, before any content object is read**:

- `job.json` absent → `404 not_found` (also for a foreign team: lookup is team-prefixed).
- Present and `now ≥ expires_at` → `410 job_expired`, no content read.
- Present and `now < expires_at` → serve.

`final.json` is authoritative for *state*; `job.json` for *existence and expiry*; a divergence is
repaired by reconciliation (§6.2). Ownership and expiry share that one source, which is why the code
flips to `404` when cleanup finally deletes `job.json`. [CONFIRMED] The user accepted `404` after
cleanup, so no contentless stub is kept to make `410` permanent. What is guaranteed is that access is
denied at `expires_at` in both cases: a late cleanup cannot extend access, and an early cleanup cannot
grant it, because cleanup never starts before `expires_at` and content is deleted before records (D37).

### 3.4 Idempotency conflict rules

Key is `(team_id, Idempotency-Key)`; content identity is `(SHA-256(bytes), byte_length)` (D35).

| Existing record | Same content | Different content |
|---|---|---|
| Live (job non-final, or final with `now < expires_at`) | `200` with the existing job; no new work | `409 idempotency_conflict` |
| Expired or absent | new job accepted (record replaced by CAS, §4) | new job accepted |

The comparison uses the stored hash and the incoming hash, so it never reads the stored input object.
That matters because the input is deleted immediately on full success while the record lives until
`expires_at`: a retry after a successful job still gets `200` with the same job id. After the record
expires, deduplication is no longer guaranteed (spec).

---

## 4. Submission: claim, admission, materialization [D34]

Order: authenticate → validate → **idempotency claim** → **admission** → materialize `job.json` →
persist input → record dispatch intent → launch → respond.

1. **Claim** `idem/{team_id}/{key_hash}` with `ifGenerationMatch=0`, carrying a fresh `job_id` and the
   content identity. Created → this caller owns the job (the `202` path) and is the only actor allowed
   to acquire an admission for it. `412` → re-read (strong read-after-write): same content and live →
   `200` path, adopt `existing.job_id`, **acquire nothing**; different content and live → `409`;
   expired → CAS-replace (on `412`, re-read and re-decide).
2. **Admission** (§6.4) on the `202` path only, before materialization, so the counter bounds admitted
   jobs rather than HTTP requests.
3. **Materialize** `jobs/{team_id}/{job_id}/job.json` with `ifGenerationMatch=0`, holding identity,
   pins, `accepted_at`, `deadline_at`, `state: accepted`. `412` → re-read and require identical
   identity fields, else fail closed `internal_error`.
4. **Persist input** `inputs/{team_id}/{job_id}` with `ifGenerationMatch=0`. **A `412` here is
   success**: the name is job-scoped and server-generated, so it means the input is already present.
   Verify `size == job.json.byte_length`; mismatch is corruption → fail closed `internal_error` +
   alert. **A `412` never triggers compensation** [D29].
5. **Dispatch intent and launch** (§6.1).

**A job is usable only when `job.json` and its input both exist**, created in that order. A genuine
(non-`412`) input-write failure responds `503 storage_unavailable`, and compensation is scoped to what
the actor actually owns: only the claim creator may delete its own `idem` record, at the generation it
read at creation, and only objects it created itself. A retry that adopted another actor's `job_id`
must not delete that record — it belongs to a live job another actor may still be materializing, and
deleting it would lose deduplication and strand the job. A compensating actor that cannot finish does
nothing further; the repair falls to reconciliation's missing-input rule (§6.2).

Both the `202` and the `200` path run steps 3–5 **for a non-final job**, which makes the
claim-to-materialize crash window harmless: a retry materializes at the recorded `job_id` instead of
returning a job that does not exist. A `200` hit on an **already final** job skips steps 3–5 entirely:
nothing is materialized, nothing dispatched, and a deleted full-success input is not re-uploaded (which
would otherwise extend its retention).

---

## 5. GCS conditional-write protocol

### 5.1 Primitives (re-verified today)

- Single-object atomicity; strong read-after-write, read-after-metadata-update, read-after-delete, and
  list consistency. Batch and multi-object operations are **not** atomic.
- `ifGenerationMatch`/`ifMetagenerationMatch` fail a mutating request with `412` on mismatch. Official
  guidance is to always pair a metageneration with a generation precondition — satisfied trivially,
  because this design uses only `ifGenerationMatch` (D36).
- The `0` value is a special case: the write proceeds **only if** no object with that name exists **or
  if there are only noncurrent versions of it**; a live version fails `412`. The same page adds: "The
  `0` value should only be used when writing object data: no other requests can succeed with a `0`
  precondition" — so every delete and update here carries a **real** generation, never `0`.
- Consequences: **Object Versioning must stay disabled** on this bucket (noncurrent versions would make
  an assumed-absent name creatable), and how a **soft-deleted** version interacts with the `0`
  precondition is **not documented** and must be tested [B1]. Soft delete is on by default (7 days,
  7–90, `0` disables) and preserves deleted **or overwritten** objects, so the default configuration
  is not the assumed one.

### 5.2 No-transaction invariant

No moment exists at which two objects must change together. Every invariant is "eventually repaired by
reconciliation from single-object state", and every state change is one conditional single-object
write. That trade-off is why §5.3 claims no mutual exclusion.

### 5.3 Claim, lease, fencing

`claim.json` holds `{owner, fence, lease_expires_at, heartbeat_at}`; lease TTL 5 min, heartbeat 60 s
[D15; values benchmark-gated B3]. Every step is one conditional write with `ifGenerationMatch` (D36):

1. **Acquire** — create with `ifGenerationMatch=0`, `fence: 1`.
2. **On `412`** — read it. Live (`lease_expires_at > now`) → another worker owns the job: retry with
   backoff within the task timeout, then exit non-zero **writing nothing else**. Expired → **take
   over** by CAS-replacing at the generation just read, `fence + 1`, fresh TTL. This is the only path
   that puts a second worker on one job, and it makes a crashed holder recoverable within one TTL
   instead of never.
3. **Renew** — CAS-replace at the generation last read, keeping `owner` and `fence`. `412` means the
   lease was taken over: stop writing, exit non-zero.
4. **Release** — conditional delete at the generation read, on completion or give-up. Cleanup also
   removes an unreleased lease.

**Recovery bound:** a crashed holder blocks takeover for ≤1 TTL, so resumption latency after a crash is
`≤ TTL + heartbeat` per attempt, not the 24 h deadline.

**What the fence guarantees:** no write is silently interleaved; every object's content is one writer's
read-modify-write against a known generation. Each guarded object carries the writer's `{owner, fence}`
as its own field and is written with the generation read *from that same object*.

**What it does not guarantee — exclusion between workers.** After a lease expires and another worker
takes over, the older worker may still run and can still CAS-write an object whose stored fence equals
its own, because the newer fence lives in the other worker's `claim.json`. No cross-object compare
closes that window. This document makes no "a stale worker cannot write" claim.

**What makes staleness harmless:** (1) `final.json` create-if-absent is the single arbiter; (2) the
published result has a fence-unique name, created once and never rewritten; (3) checkpoints are *used*
only when their `engine_ref` matches the job pin; (4) the `job.json` final flip is repaired from
`final.json`. Residual cost: duplicated OCR work, bounded by the lease TTL, the ≤2-attempt dispatch
cap, and `deadline_at` — never a duplicated or mixed published result.

### 5.4 Finalization

1. Write `result.{fence}.json` with `ifGenerationMatch=0` — fence-unique, never rewritten.
2. Create `final.json` with `ifGenerationMatch=0` holding `{finalized_at, state, reason,
   result_object, fence}`. One writer succeeds; `412` means another already finalized. This is the
   **only** cross-writer arbiter.
3. Flip `job.json` at the generation read from it, carrying `{state, finalized_at, expires_at, reason,
   fence, updated_at}` (the D33 projection), and decrement the admission counter (§6.4) — only this
   winner may.

Readers resolve a result **only** through `final.json.result_object`; a `result.*.json` without a
matching `final.json` is unpublished and ignored (cleanup removes it by enumeration). **Assurance
limit:** the three steps are independent single-object writes with no cross-object transaction, so a
crash can leave a result or a `final.json` without the `job.json` flip; reconciliation repairs that.
"Exactly once" means **exactly one winning `final.json`**, not an atomic multi-object commit. The
arbiter's authority lasts as long as the object exists: once cleanup deletes `final.json`, a straggler
could create a new one at that name — closed by §6.3's guard and the worker rule below, not by any
cross-object property.

### 5.5 Worker contract

- Reads `job.json` **before writing any object**; if that read fails it writes **nothing** and exits
  non-zero [D30]. After cleanup removed the records, a straggler finds no job to resume and creates
  nothing.
- **Writes nothing for an attempt that starts at or after `deadline_at`**, and exits non-zero. This
  bounds a straggler's last write to `deadline_at + per_attempt_timeout` — one in-flight attempt, not
  a retry chain.
- Publishes one checkpoint per page (`pages/{n:05d}.json`, carrying `engine_ref` and the writer's
  fence), resumes from present checkpoints, accepts one only when its `engine_ref` matches the job
  pin, and refuses to run if its resolved engine/config disagrees with `job.json` [D16].
- Exits `0` on success, non-zero on failure, and never listens on a port. Uses `CLOUD_RUN_JOB`,
  `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_*` for identity and log correlation.
- `--max-retries` 2 [DEFAULT, benchmark-gated]; task timeout per attempt, provisional 30 min.
  Application checkpointing is required alongside platform retries.

### 5.6 Crash recovery matrix

| Crash point | Recovery |
|---|---|
| After the claim, before `job.json` | retry materializes at the recorded `job_id`; with no retry nothing runs |
| After `job.json`, before the input | not usable: submitter compensates and returns `503`; else reconciliation finalizes `failed`/`internal_error` and conditionally deletes the `idem` record |
| After `job.json`, before `dispatch.json` | reconciliation dispatches it |
| After `dispatch.json`, before the launch returns | `acknowledged` stays `false`; reconciliation re-dispatches after ≥5 min when no live lease exists and attempts remain |
| After the lease, before any page | lease expires; the next attempt takes it over with `fence + 1` and recomputes |
| Holder dies holding the lease | same: takeover possible after ≤1 TTL |
| Between page checkpoints | the next attempt lists `pages/*` and resumes from present ones |
| After `result.{fence}.json`, before `final.json` | unpublished; the next writer or the reconciler finalizes and abandons the earlier object |
| After `final.json` | a second `final.json` gets `412` while the name exists, the published result is never rewritten, the `job.json` flip fails its own CAS |
| Cleanup starts while a straggler still writes | impossible by construction (§6.3) |
| During cleanup, partially | idempotent and re-runnable; a failed precondition is skipped and re-decided next pass |

### 5.7 Bucket layout

One bucket, name prefixed `dev-ocr-service-*`; `team_id` is the first path segment, so listing,
audits, and application-scoped queries are team-scoped (IAM caveat in §7.3).

```
inputs/{team_id}/{job_id}
jobs/{team_id}/{job_id}/job.json            # create-if-absent: identity, pins, accepted/deadline;
                                            # CAS-updated with the D33 projection
jobs/{team_id}/{job_id}/claim.json          # lease + heartbeat
jobs/{team_id}/{job_id}/dispatch.json       # create-if-absent intent; CAS-updated ack
jobs/{team_id}/{job_id}/pages/{n:05d}.json  # one checkpoint per page
jobs/{team_id}/{job_id}/result.{fence}.json # published result; created once, never rewritten
jobs/{team_id}/{job_id}/final.json          # finalization arbiter + authoritative final state
idem/{team_id}/{key_hash}
capacity/inflight.json
```

Created `job.json` holds `{schema_version, job_id, team_id, accepted_at, deadline_at, content_hash,
byte_length, idempotency_key_hash, engine_ref, worker_job_name, worker_revision_digest}`; later CAS
updates carry only the projection fields. A reader finding a changed identity field treats the record
as corrupt, alerts, and fails closed `internal_error`. This layout — not a fixed deletion list — is the
cleanup contract, because more than one `result.{fence}.json` can exist and a crashed holder can leave
`claim.json` behind.

---

## 6. Dispatch, reconciliation, cleanup, capacity

### 6.1 Dispatch [D6/D7/D14]

The API calls Cloud Run Admin `POST /v2/{name}:run` on the **version-pinned worker job** recorded in
`job.json`; the response is an `Operation`, recorded and not awaited. Overridable: `containerOverrides
[].{name,args,env,clearArgs}`, `taskCount`, `timeout` — there is **no `image` field**, and sending
`overrides` needs `run.jobs.runWithOverrides` [B2]. Launch success is not processing progress; worker
completion is observed **only** from the GCS finalization record.

1. Create `dispatch.json` with `ifGenerationMatch=0` holding `{attempts: 1, last_attempt_at,
   acknowledged: false, execution_ref: null, last_error_code: null}`.
2. **Only the creator calls `jobs:run`**, and immediately before it re-reads the pinned job and
   requires its template image to equal `job.json.worker_revision_digest` (§8). A `412` on step 1 means
   another submitter already owns this attempt, so this caller does not dispatch — that is what stops a
   concurrent duplicate submission from launching two executions.
3. On a launch response, CAS-update `dispatch.json` at the generation read in step 1. Losing that CAS
   is harmless. `acknowledged`/`execution_ref` are diagnostic [D31]: an acknowledged operation can
   still fail or never start an execution.
4. A further attempt may be claimed only when **all** hold: non-final, `now < deadline_at`, ≥5 min since
   the last attempt, `attempts < 2`, no live lease. This is `may_dispatch()` in `app/ocr_contract.py`,
   asserted in §11.1; it deliberately takes no acknowledgment parameter.
5. A **non-retryable** launch error (`PERMISSION_DENIED`, `NOT_FOUND` for the pinned job, or the
   image/pin mismatch of step 2) does not retry: reconciliation finalizes `failed` with
   `reason: engine_unavailable` and alerts.

**Residual, stated plainly.** Re-dispatch does not read `acknowledged`, so it fires when (a) a launch
response was lost, (b) the operation was acknowledged then failed, or (c) the execution is still queued
and holds no lease. Duplicate *work* is bounded by `≤2` dispatch attempts × `1 + max-retries` task
attempts, the lease TTL, and `deadline_at`. Guarantees: one logical job per `(team_id, key)`,
at-least-once execution, at most one published result; there is **no** single-execution guarantee.
[CONFIRMED] The user's requirement — a duplicate submission retry must not start duplicate processing —
holds because a same-key retry never creates a job and never owns a dispatch attempt (§4).

**Stranded execution.** v1 does not read `Job.execution` state, so a job whose launch never starts, or
whose second attempt also dies before finalizing, is not classified early: it waits for `deadline_at`
and is finalized there. The executions resource does expose terminal state, so earlier classification
is possible but would add an Admin API dependency and permission; it is deferred and named in B2 rather
than assumed.

### 6.2 Reconciliation

Cloud Scheduler → Cloud Run Job `dev-ocr-service-reconcile` periodically scans GCS state and repairs
with conditional writes only, so concurrent reconcilers cannot double-finalize (`final.json`
arbitrates). Rows are evaluated in order, so a partially materialized job is finalized rather than
launched.

| Condition | Action |
|---|---|
| Non-final, `now < deadline_at`, `dispatch.json` absent | dispatch (crash between materialization and launch, or a lost `202`) [D28] |
| Non-final, `now < deadline_at`, ≥5 min since last attempt, `attempts < 2`, no live lease | claim and dispatch the next attempt (`acknowledged` not consulted) |
| Non-final, attempts exhausted, last error non-retryable | finalize `failed`/`engine_unavailable` + alert |
| Non-final, `now ≥ deadline_at` | finalize `deadline_exceeded` (§3) |
| Final job whose `job.json` disagrees with `final.json` | repair `job.json` from `final.json` (never releases capacity) |
| Non-final, input absent, `now - accepted_at ≥ 5 min`, no live lease | finalize `failed`/`internal_error`, alert, delete the `idem` record only at the generation read and only if it still names this `job_id` |
| Expired live `idem` record whose job is final and past `expires_at` | delete, same generation-conditional rule |
| `capacity/inflight.json` drift | recompute exactly from enumerated non-final jobs (§6.4) |

Everything else non-final and inside its deadline is left alone: the deadline will finalize it.
Reconciliation never resumes page work; it only dispatches, finalizes, repairs, or deletes.

### 6.3 Cleanup

Cloud Scheduler → Cloud Run Job `dev-ocr-service-cleanup` is the physical deleter, and it is
**application-driven**: Object Lifecycle Management changes take up to 24 h to propagate, so a
lifecycle rule cannot meet the 1 h target. A lifecycle rule is at most an orphan backstop.

- **Full success:** the input object is deleted immediately after finalization — by the finalizing
  worker, and by reconciliation if the worker died first. A momentary failure is retried; overdue
  deletion is alerted. A post-success submission retry cannot undo it, because a `200` hit on a final
  job writes nothing (§4).
- **Guard.** No pass may start before `cleanup_guard = max(expires_at, deadline_at +
  per_attempt_timeout)` (asserted in §11.1). Derived, not guessed: no dispatch happens at or after
  `deadline_at`; an attempt is bounded by the per-attempt timeout; and an attempt that starts at or
  after `deadline_at` writes nothing. So an attempt may start just before the deadline and write for up
  to one timeout past it, and later attempts write nothing — the last possible write is
  `deadline_at + per_attempt_timeout`. Without the guard a straggler could re-create `pages/*`, an
  unpublished result, or a fresh `final.json` after the pass was reported complete.
- **1 h target.** `guard - expires_at = max(0, per_attempt_timeout - (finalized_at - accepted_at))`, so
  the guard exceeds `expires_at` only for jobs that finalize *early*. With the startup invariant
  `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` [D32], the first legal pass starts at
  ≤ `expires_at + 1 h` by construction. Residual: scheduler and execution latency, alerted, not
  guaranteed.
- **Order (D37).** Content, then records, `job.json` last among the job's objects: (1) `inputs/…`,
  every `pages/*.json`, every `result.*.json` — including results from workers that never won
  `final.json`, so a fixed list would leak; (2) `claim.json`, `dispatch.json`, `final.json`, then
  `job.json`; (3) `idem/{team_id}/{key_hash}`. Because the gate reads `job.json`, the `410`/`404` flip
  happens only after no content remains, and no reader sees a job record whose expiry is unknown.
- **Every delete is conditional, including the `idem` record.** A delete whose precondition fails is
  **skipped, not retried blind** — a mismatch means another actor replaced the object after this pass
  read it. This matters most for the record, whose name is reused: an expired record is CAS-replaced by
  a new submission with the same key and different content, so a name-addressed delete would remove a
  **live** record for a **new** job, silently losing deduplication and stranding that job. The record
  is deleted only when the generation still matches the one read **and** its `job_id` still names this
  job. Cleanup is idempotent; a partial pass is completed by the next one.

### 6.4 Global capacity [D13/D34]

One coordination object, `capacity/inflight.json` holding `{count, updated_at}`.

- **Acquire** on the `202` path only, after the claim, before materialization. Read
  `(generation, count)`; `count ≥ ceiling` → `429 capacity_exceeded` (no write); else CAS-write
  `count + 1` at that generation. On `412`, re-read and re-decide, bounded to 5 attempts with jittered
  backoff. **On exhaustion, `429` — fail closed.** There is no fail-open path: an admission that cannot
  be recorded is exactly the unaccounted case the bound must exclude.
- **Release** by the `final.json` winner only (§5.4 step 3), CAS-decrement at the generation read. No
  other actor decrements — not the submitter on a `503` path, and not reconciliation, which never
  releases. An abandoned admission therefore leaks as an **over-count** until repair.
- **Repair:** recompute `count` as the number of enumerated non-final jobs (listing is strongly
  consistent) and CAS-set it. No percentage tolerance: a tolerance would be a bound violation by
  construction.
- **Why the bound holds, and its residual.** Every path that could read low is removed: release happens
  at most once and an unrecorded admission is impossible, so drift is one-directional (high =
  over-reject). Disclosed exception: a repair racing an acquisition whose job is not yet persisted reads
  the enumeration as a lower bound and can transiently set `count` below the true in-flight number by at
  most the acquisitions in flight; the next pass restores it. This is a bounded, self-correcting
  residual of a single-object counter, **not an exact ceiling** [B6].
- **Throughput:** the counter is one serialization point rewritten by every submission and every
  finalization; sustained contention surfaces as fail-closed `429`, so its throughput bounds the
  submission rate. The ceiling value is benchmark-gated [B3]. The API is additionally capped by
  `--max-instances`, where Cloud Run queues briefly then fails with `429`; that is not relied on for the
  document ceiling, but a client can see a `429` this design did not produce [D21].

---

## 7. Authentication, team mapping, isolation

**Transport (§7.1).** The API requires IAM authentication: no `allUsers`, no public access. Callers
present a Google-signed OIDC ID token whose `aud` is the service URL (or a configured custom audience)
and whose identity holds `roles/run.invoker`. Cloud Run validates it before the container sees the
request, on every path including `/health`.

**[D12] In-app identity.** Primary: re-verify the ID token in-process with Google's official library —
signature against Google's keys, `iss` ∈ {`accounts.google.com`, `https://accounts.google.com`}, `aud`
= the service URL, `exp`/`iat` valid with 60 s skew. Identity is the `email` claim, with `sub`
recorded; a key-fetch failure is `401` (fail closed). Only `Authorization: Bearer <token>` is accepted:
`X-Serverless-Authorization` is **not**, because Cloud Run strips its signature before the container
sees it, so it cannot be re-verified and accepting it would mean trusting an unverified header. If both
headers are present, Cloud Run checks only the stripped one and may admit a caller whose
`Authorization` token is unverifiable; the application then rejects it `401` — the intended fail-closed
direction. No identity header (`X-Goog-Authenticated-User-Email`, `X-Forwarded-User`, …) is ever read
for authorization. Fronting with a Google-managed proxy that signs its own identity header is a
retained future alternative [B2]. Two independent layers mean neither a forged header nor a
misconfigured invoker alone can impersonate a team.

**Mapping (§7.3).** A deployed allowlist maps a verified principal (service-account `email`, keyed
additionally by `sub`) to an internal `team_id`, loaded from configuration at startup. Unmapped →
`403 team_not_authorized`. `team_id` is derived **only** from verified identity — never from body,
path, query, or a header — and is written into `job.json` and the idempotency path at creation.
Ownership is enforced on every job/result operation by comparing the stored `team_id` with the caller's
mapped `team_id`; a mismatch is `404`.

**Isolation bound.** All teams share one bucket and one service account; there is no per-team GCS IAM
and none is planned for v1. Job-prefix scoping is what makes a foreign id simply absent, and the
isolation guarantee is enforced by the application (team-prefixed paths + the ownership check), not by
storage ACLs. A defect in that check would expose other teams' objects, which is why it is a seam test
(§11.2/S1) rather than an operational assumption.

[B2] The real n8n identity-token path must be verified, not assumed: n8n on GCP uses the metadata
server; n8n off GCP needs `iamcredentials.generateIdToken` or workload identity federation. The mapping
table and the token path are deployment evidence, not design choices.

---

## 8. Version and configuration pinning

Recorded once in `job.json` and frozen for the job and all its retries: `engine_ref` (engine id, model
id, config version/hash, pre-/post-processing parameters), `worker_revision_digest` (the worker image
by immutable digest, never a tag), `worker_job_name`, `schema_version`, and any decode parameter that
affects output bytes. `engine_ref` is operator-only [D19].

**One job per revision** [D18]: each worker revision gets its own Cloud Run Job
`dev-ocr-service-worker-<slug>` created with that revision's image by digest. Dispatch runs *that* job
by name, with overrides carrying only `env` and `timeout`; an image cannot be selected at dispatch time.

**Detection, not a lock.** Immediately before `jobs:run` the dispatcher re-reads the pinned job and
requires `containers[0].image` to equal `job.json.worker_revision_digest`; a mismatch, a missing job,
or a denied read is the non-retryable `failed`/`engine_unavailable` case with an alert. Being
read-then-act, a job re-pointed between the read and the call still runs the wrong image. The
`jobs:run` request accepts an `etag` documented to "detect modification conflict", but its run-time
failure semantics are not documented for this call and are not assumed here [B2].

**What re-executability needs, and does not get.** It holds only if (a) the versioned job is never
deleted or re-pointed, (b) its image digest and layer blobs remain in Artifact Registry, and (c) its
runtime service account still exists. Cloud Run jobs are updatable and deletable in place and registry
cleanup policies can delete blobs, so this is an operational invariant, not a platform guarantee; its
enforcement (deny delete/update on versioned jobs, no AR cleanup for pinned digests, alert on registry
deletion) is a deployment/IAM artifact [B2].

**The worker cannot verify its own image.** The runtime contract exposes the job and execution names
via `CLOUD_RUN_JOB`/`CLOUD_RUN_EXECUTION`/`CLOUD_RUN_TASK_*`, never the image digest, so image identity
is enforced only at dispatch and by the operational guards; a wrong-image execution that dispatch does
not catch is not detectable by the worker itself.

---

## 9. Race and failure scenarios

1. **Duplicate submit, same key and content** — one creates the record, the other gets `412`, re-reads
   under strong consistency, adopts the same `job_id`, materializes idempotently, returns `200`,
   acquires no admission, owns no dispatch attempt. [CONFIRMED]
2. **Duplicate submit, same key, different content** — `409`; the original job is untouched.
3. **Crash between claim and `job.json`** — a retry materializes at the recorded `job_id`; a `200`
   never names a job that does not exist. With no retry, nothing runs and no orphan job is created.
4. **Launch acknowledged, execution never starts or fails later** — no lease exists, so reconciliation
   re-dispatches after ≥5 min without consulting `acknowledged`; after the attempt cap the job waits for
   `deadline_at`. A merely-queued execution can be duplicated: the accepted at-least-once cost.
5. **Launch fails or its response is lost** — `202` with `acknowledged = false`; same path. A retry of
   the *submission* still returns `200` with the same job.
6. **Two workers, one job** — both may run; whoever writes `final.json` first wins, the other gets
   `412`. Cost: duplicated work, never a duplicated published result.
7. **Crash after `result.{fence}.json`, before `final.json`** — unpublished; the next writer or the
   reconciler finalizes once.
8. **Crash between page checkpoints** — the next attempt lists `pages/*` and resumes, accepting only
   checkpoints whose `engine_ref` matches the pin.
9. **Deadline while running** — finalized `deadline_exceeded`, `partially_succeeded` if usable pages
   exist, else `failed`; a straggler cannot replace the published result.
10. **Stale worker returns after finalization** — its `final.json` create fails `412` while the name
    exists, the published result is never rewritten, its `job.json` flip fails its own CAS, its extra
    objects are removed by enumeration. Once cleanup deleted the records the `412` no longer protects
    the name; §6.3's guard closes that window, not the arbiter.
11. **Read during or after cleanup** — before `expires_at` the result is served even if cleanup is
    late; at/after with the record present → `410`; after the record is gone → `404` [CONFIRMED].
    Content is never served after `expires_at`.
12. **Cleanup partially fails** — content first, `job.json` last, idempotent and re-runnable; no state
    exists in which the job record is gone but content is being served.
13. **Idempotency record expired, job gone** — the same key and content creates a new job; dedupe is no
    longer guaranteed (spec). The record is replaced by CAS, so concurrent post-expiry submissions
    still elect one winner.
14. **Malformed, encrypted, or over-limit input** — rejected at submission, or, if only the worker
    discovers it, finalized `failed`/`input_unreadable` with a page error, never an unexplained crash.
15. **Pinned job deleted or overridden** — `failed`/`engine_unavailable` with an alert, not an infinite
    retry.
16. **Job materialized without its input** — never launched (the dispatcher checks the input first);
    the claim owner compensates its own objects; else reconciliation finalizes it
    `failed`/`internal_error` after the 5-minute grace window with no live lease, then deletes the
    record conditionally, so a same-key retry starts cleanly.
17. **Admission drift or contention** — an unrecordable admission is rejected `429` (fail closed);
    drift reads high until the next exact repair.
18. **Concurrent reconcilers** — every action is a conditional single-object write and `final.json`
    arbitrates, so a double finalize is impossible.
19. **Worker crashes holding the lease** — the lease expires and the next worker takes it over with
    `fence + 1`, resuming from `pages/*`.
20. **Cleanup races a post-expiry re-submission with the same key** — the new submission CAS-replaces
    the expired record; the old job's delete fails its precondition and is skipped, so the new job
    keeps deduplication.
21. **Cleanup races a straggler** — no such race by construction: dispatch stops at `deadline_at`, no
    attempt starting at or after it writes, no pass starts before the guard.
22. **Retry writes an input that already exists** — `412` means present; the size is verified against
    `job.json.byte_length`, dispatch proceeds, nothing is compensated.
23. **Retry after full success** — the record is live until `expires_at` and the input is already
    deleted; the `200` path returns the existing object and writes nothing, so retention is not
    extended and no input is re-uploaded.

---

## 10. Retention, erasure, logging, limits

- Input deleted immediately after **full** success; partial and failed inputs are retained until the
  cleanup pass (`now ≥ cleanup_guard`, §6.3). Access is denied at `expires_at` whether or not cleanup
  has run (§3.3). Cleanup target ≤1 h after `expires_at`, alerted if overdue; the guaranteed part is
  the earliest legal pass start under D32, the residual is scheduler latency.
- [D17] Bucket configuration: **soft-delete retention `0`, Object Versioning disabled, no Bucket
  Lock, no Object Retention Lock**, no lifecycle rule as the deletion mechanism. Rationale: soft delete
  is on by default, applies to overwrites as well as deletions, and soft-deleted objects still accrue
  cost with an explicit caution for temporary data; versioning independently retains a noncurrent
  version on every replace or delete, which would break the `0`-precondition semantics the protocol
  depends on.
- **Erasure is disclosed, not guaranteed.** Retention `0` removes one recovery path; copies can also
  come from versioning, Bucket/Object Retention Lock, replication or backups, and project-level
  recovery windows. No exact-second erasure guarantee is claimed. "Configured" is not "in effect":
  soft-delete retention changes take up to 30 s to propagate, versioning ≥30 s, lifecycle up to 24 h.
  Which mechanisms are active in the deployed project is [B4].
- [D20] Logs carry an allowlisted field set only: `job_id`, `team_id`, `state`, `error_code`,
  `page_number`, counts, durations, `request_id`, `execution_ref`. Document content, OCR text,
  idempotency keys, and tokens are never logged; error messages are fixed per code. Log retention and
  alerting are configuration items [B4].
- **Limits [D21/D38].** Platform facts, re-verified today: Cloud Run caps an HTTP/1 request at
  **32 MiB and the limit cannot be raised**, 60 minutes is the maximum time before a request timeout,
  and per-instance concurrency is 1000. Provisional configuration, benchmark-gated except the ceiling:
  20 MiB upload, 500 pages/frames, 30 min per attempt, worker parallelism 1, API `max-instances` 4,
  ceiling value [B3].
- **Memory bound.** The body is buffered and hashed in full before the idempotency decision (the hash
  *is* the content identity), so worst-case memory per instance is roughly `max_upload_bytes` plus
  multipart overhead, times `max-instances`. The 20 MiB cap leaves headroom under the 32 MiB ceiling on
  a 512 MiB-class instance; raising it, or serving HTTP/2 to escape the ceiling, is a
  benchmark-and-configuration decision, not a contract change.
- **D32 startup invariant** `per_attempt_timeout + cleanup_schedule_period ≤ 1 h`, asserted at startup
  and in §11.1 (provisional 30 + 15 min).

---

## 11. Seams and tests

**Approved seams [CONFIRMED S1–S3]** (§1.1), per the `tdd` rule that no test is written at an unconfirmed
seam: **S1** public lifecycle through the HTTP API; **S2** storage coordination (claim/materialize,
lease+fence CAS, checkpoint and result publication, finalization arbiter, dispatch intent, capacity
counter, cleanup predicates); **S3** the application-owned recognition adapter, exercised with a fake
engine. A worker-entrypoint seam is deliberately *not* added: its behavior is observable through S1 and
S2.

### 11.1 Draft rules, executable locally, not committed (no cloud, no sleep)

**Not committed by the design commit.** In the working tree, `app/ocr_contract.py` holds the decidable
rules of §2–§6 and `tests/test_ocr_contract.py` asserts them; both are left uncommitted deliberately,
pending parent review. They assert pure decision functions directly rather than behaviour through the
approved S1–S3 seams, so they are not evidence for S1–S3, and the committed contract stands on §2–§6
alone. Local evidence from the design-commit session: `uv run pytest tests/test_ocr_contract.py -q` →
36 passed; `uv run pytest -q` → 66 passed (30 existing + 36); `uv run ruff check .` → clean. Covered:
configuration invariants (upload ceiling versus the platform limit, D32, heartbeat <
lease); `deadline_at`/`expires_at`/`cleanup_guard` including the early-finalization case and the proof
that the guard stays within 1 h of `expires_at` under D32; the expiry gate at its boundary
(`now == expires_at` → expired, record absent → gone) and that it takes no caller-identity parameter;
idempotency `create`/`reuse`/`conflict`, byte-length mismatch, and dedupe ending exactly at expiry;
`may_dispatch` bounds (deadline, live lease, 5-minute spacing, 2-attempt cap) and that it has no
`acknowledged` parameter; deadline finalization preserving usable pages; admission fail-closed at the
ceiling and on CAS exhaustion, the single-release rule, exact repair; container sniffing, declared-type
mismatch, gate order and codes, `Idempotency-Key` format; the closed error vocabulary with no message
leaking a path, bucket, or URL; page rules (embedded-text usability, `text_source`, trim,
combined-text order and separator, warning union, completeness, 1-based contiguity, closed
vocabularies).

### 11.2 Actionable tests for the implementation (not yet writable)

Each line is one test: setup → action → assertion. They reuse the existing injected-dependency and
`TestClient` approach, a fake recognition engine, a controllable dispatcher, an injected fake clock,
and an in-memory store that emulates generations and `412`. Fakes do **not** prove real GCS behavior
[B1].

S2 — storage coordination: (1) two concurrent submissions, same key and content → one `idem` create,
both return the same `job_id`, exactly one job directory; (2) same key, different bytes → one `409`,
winner untouched; (3) crash after the claim, before `job.json` → the next same-key request materializes
at the recorded `job_id` and never returns `200` for a nonexistent job; (4) input write returns `412`
on the `200` path → size verified, dispatch proceeds, nothing deleted; (5) genuine input-write failure
→ `503`, the claim creator's own record and objects removed, an adopted record untouched; (6) lease
acquirer dies → after the TTL the next worker takes over with `fence + 1`, and the superseded holder's
next renew gets `412` and stops; (7) live lease held elsewhere → the second worker exits non-zero
having written nothing; (8) worker whose `job.json` read fails → writes no object, exits non-zero;
(9) lost launch ack → one further attempt only after ≥5 min, blocked while a lease is live and at/after
`deadline_at`; (10) attempts exhausted with a non-retryable error → `failed`/`engine_unavailable` +
alert, never a retry loop; (11) pinned job re-pointed → `failed`/`engine_unavailable`, and a job
re-pointed between the read and the launch is asserted as the documented residual (not caught);
(12) attempt starting at or after `deadline_at` → writes nothing; (13) cleanup before
`cleanup_guard` → refused, at the guard → one pass deletes every enumerated `pages/*`,
`result.*.json` (including unpublished fences), `claim.json`, `inputs/*`; (14) cleanup racing a
post-expiry re-submission → the stale delete is skipped and the new record survives; (15) 5 CAS
failures on admission → `429` and no job object; (16) a second finalizer and a reconciler repair cannot
double-decrement; (17) an over-count leaked by an abandoned admission → repaired to the exact
enumerated value; (18) crash at each row of §5.6 → restart recovers to the same observable state.

S1 — public lifecycle: (19) exact result keys and types, `schema_version == "ocr.v1"`, no engine or
expense field; (20) 1-based contiguous pages, one entry per source page for PDF, multi-frame TIFF,
JPEG, PNG; (21) combined-text order and separator, failed pages visible with `error_code` and
`text: null`; (22) warning-vs-error separation, `accuracy == "not_verified"`, `completeness` on a
partial job; (23) `reason == "deadline_exceeded"` on the deadline path, `null` on success; (24) two
dispatched executions → exactly one published result with one `finalized_at`, with no assertion on
internal call counts; (25) missing or malformed `Idempotency-Key` → `400 invalid_idempotency_key`;
(26) foreign-team id → `404` on both read routes, `404` after cleanup, `410` at `expires_at` while
records remain; (27) `429` carries `Retry-After`, every error body has the fixed allowlisted shape;
(28) `/health` returns `{"status": "ok"}` without initializing a cloud client.

S3 — engine seam: (29) a per-page engine failure → that page `failed`, others preserved, job
`partially_succeeded`; (30) engine returning no text → `text: ""` + `no_text`, page still `succeeded`;
(31) engine config disagreeing with the job pin → the worker refuses to run and writes nothing;
(32) native PDF with a clean text layer → `text_source: "embedded"`, asserted through the returned
page rather than a call count.

### 11.3 Separate campaigns (not this ticket)

Engine benchmark: CPU-only, labelled expected text on selected pages, accuracy reported separately
from schema/integration success, corpus/metrics/normalization/thresholds frozen before comparing
candidates; every numeric limit in §10 is gated behind it. Cloud verification needs its own approved
budget: the B1 protocol probes, the B2 IAM/token/pin checks, the B4 bucket read-back and monitored
deletion, and duplicate-delivery/recovery checks against a real deployment.

---

## 12. Decisions

**The design as a whole is approved (§1.1).** [DEFAULT] rows are this document's own choices: approved
as part of that design, still individually vetoable, and **not verified** — a row whose value is a
placeholder for a benchmark result stays benchmark-gated [B3]. [CONFIRMED] rows are the user's explicit
confirmations.

| ID | Decision | Provenance |
|---|---|---|
| D1 | A duplicate submission retry (same team + key + content) returns the existing job, starts no second job, owns no dispatch attempt. | **[CONFIRMED]** |
| D2 | No cancellation endpoint and no execution `cancel` call in v1. | **[CONFIRMED]** |
| D3 | `404` after cleanup is acceptable; no stub is kept to make `410` permanent. | **[CONFIRMED]** |
| D4 | rev 2 design direction and seams S1–S3. | **[DIRECTION]** |
| D5 | `Idempotency-Key` required, 8–128 chars of `[A-Za-z0-9._:~-]`, scoped to `team_id`. | [DEFAULT] |
| D6 | Persist-first: failed launch → `202` with `acknowledged=false`; `503` only for pre-persistence failure. | [DEFAULT] |
| D7 | Dispatch = direct `jobs:run` on the pinned revision job + Scheduler reconciliation + Scheduler cleanup; no queue service. | [DEFAULT] |
| D8 | States `accepted/running/succeeded/partially_succeeded/failed`; one `job.json` instead of `meta.json`+`state.json`. | [DEFAULT] |
| D9 | Claim-then-materialize; usable only with `job.json` **and** the input; `503` + scoped compensation on input-write failure. | [DEFAULT] |
| D10 | One expiry rule: `410` while the job record exists, `404` after cleanup; `expires_at` from `final.json.finalized_at`. | [DEFAULT] |
| D11 | Embedded-text usability: PDF only, ≥1 non-whitespace char after NFC + trim, no `U+FFFD`; else `ocr`. | [DEFAULT] |
| D12 | In-app ID-token re-verification with the official library; `Authorization` only; 60 s skew; key-fetch failure → `401`. | [DEFAULT] |
| D13 | Admission ceiling via one CAS counter, **fail-closed**; release only by the `final.json` winner; exact enumeration repair. | [DEFAULT] |
| D14 | `max-retries = 2`; ≤2 dispatch attempts, ≥5 min spacing, lease check, `now < deadline_at`; non-retryable launch error → `failed`/`engine_unavailable` + alert. | [DEFAULT] |
| D15 | Lease TTL 5 min, heartbeat 60 s; acquire = create-if-absent, expired lease taken over by CAS-replace with `fence + 1`, `412` stops the holder, release = conditional delete; checkpoint fences are diagnostic. | [DEFAULT] |
| D16 | Result published as fence-unique `result.{fence}.json`; `final.json` arbitrates; correctness does not depend on fence-tagged resumption. | [DEFAULT] |
| D17 | Bucket: soft-delete retention `0`, versioning disabled, no locks, no lifecycle rule as the deletion mechanism. | [DEFAULT] |
| D18 | One Cloud Run Job per worker revision, image by digest; dispatch re-reads and compares the image (detection, not a lock); mismatch/`NOT_FOUND`/`PERMISSION_DENIED` → `failed`/`engine_unavailable` + alert. | [DEFAULT] |
| D19 | `engine_ref` is operator-only, never exposed to callers. | [DEFAULT] |
| D20 | Allowlisted log fields only; fixed per-code error messages; no content, keys, or tokens. | [DEFAULT] |
| D21 | Provisional limits: 500 pages, 30 min per attempt, worker parallelism 1, API `max-instances` 4, ceiling value (benchmark-gated). | [DEFAULT] |
| D22 | Non-success finalization carries `reason` ∈ {`deadline_exceeded`, `engine_unavailable`, `input_unreadable`, `internal_error`}; `null` on success. | [DEFAULT] |
| D23 | Text rules: per-page trim; combined = successful pages joined by `\n`; empty success `text: ""` + `no_text`; failed page `text: null` + `error_code`. | [DEFAULT] |
| D24 | One page entry per PDF page / TIFF frame; JPEG and PNG are single-page. | [DEFAULT] |
| D25 | A record is live while the job is non-final or `now < expires_at`; absent or expired → new job, record replaced by CAS. | [DEFAULT] |
| D26 | Cleanup: enumerated prefixes, every delete generation-conditional (the `idem` record also matched by `job_id`), idempotent, refused before the guard; full-success input deleted immediately. | [DEFAULT] |
| D27 | `AGENTS.md`/README/five-template-contract alignment and retirement of `POST /v1/documents:process` happen inside the approved implementation, not in this ticket. | [DEFAULT] |
| D28 | Reconciliation also dispatches materialized non-final jobs with no `dispatch.json`. | [DEFAULT] |
| D29 | An input `412` means "already present" (size-verified) and never triggers compensation; compensation is limited to the claim creator's own objects and record. | [DEFAULT] |
| D30 | A worker reads `job.json` before writing anything and writes nothing if that read fails; it releases its lease on completion or give-up; cleanup removes an unreleased lease. | [DEFAULT] |
| D31 | `dispatch.acknowledged` and `execution_ref` are diagnostic: no decision reads them. | [DEFAULT] |
| D32 | Startup invariant `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` (provisional 30 + 15 min), asserted at startup. | [DEFAULT] |
| D33 | `job.json` carries the final-state projection `{state, finalized_at, expires_at, reason, fence}`; it is the existence+expiry source for the read gate and is deleted last among the job's objects. | [DEFAULT] |
| D34 | Submission order: authenticate → validate → claim → admission (new jobs only) → materialize → input → dispatch intent → launch. | [DEFAULT] |
| D35 | Content identity is `(SHA-256(bytes), byte_length)`; the declared media type is a validation gate, not part of dedupe. | [DEFAULT] |
| D36 | Every mutation is one conditional write with `ifGenerationMatch` on the same object; no metadata-only updates; renewal and lease takeover are the same conditional replace. | [DEFAULT] |
| D37 | Cleanup order: content → `claim.json` → `dispatch.json` → `final.json` → `job.json` → `idem` record. | [DEFAULT] |
| D38 | Upload cap ≤ the 32 MiB Cloud Run HTTP/1 request ceiling; 20 MiB provisional, or an explicit decision to serve HTTP/2. | [DEFAULT] |

Not reintroduced from earlier revisions: the `queued` state, the `meta.json`/`state.json` split, the
`dispatch_unavailable` and `job_not_finalized` codes, the `containerOverrides.image` claim, the
cross-object fence rule, "`410` is independent of cleanup", the 50 MiB default limit, and the per-team
GCS IAM implication.

---

## 13. Remaining blockers

Nothing here can be retired by writing more design.

| ID | Blocker | What closes it |
|---|---|---|
| **B1** | **Real GCS protocol verification.** (a) Does `ifGenerationMatch=0` succeed when only a *soft-deleted* version of the name exists? (b) Do concurrent CAS writes to one object reliably yield `412`, including concurrent create-if-absent? (c) Does a `0`-precondition create fail against a *noncurrent* version (the documented reason versioning must be off)? (d) Does a conditional delete return `412` against an object replaced since the read, as cleanup and lease release assume? | An approved cloud test budget against a scratch bucket. §5 is a design, not a verified protocol, until then. |
| **B2** | **Credentials, IAM, deployment facts.** Does the API SA hold `run.jobs.runWithOverrides` and `run.jobs.get`? Can the worker SA write the bucket? What is the real n8n identity-token path? Do the bucket, prefix, versioned jobs, and Artifact Registry retention exist? What are the run-time semantics of the `jobs.run` `etag`? Is reading execution state wanted for earlier stranded-execution classification? | Scoped approval to provision and run a live IAM/token check. No deployment exists today. |
| **B3** | **CPU benchmark and every numeric limit** — engine selection, max bytes/pages, per-attempt timeout, worker CPU/memory, ceiling, retry counts, lease/heartbeat, and the D32 pair. If the benchmark needs `per_attempt_timeout + cleanup_schedule_period > 1 h`, the 1 h cleanup target and the stale-worker guard conflict and the user must choose; this document will not silently prefer one. | The approved CPU benchmark (spec "Release-blocking technical work" #1). |
| **B4** | **Retention, erasure, operational configuration.** Actual soft-delete and versioning state, absence of locks/replication/backups, project recovery windows, cleanup alerting mechanism, log retention. | Approval to inspect/provision the real bucket, then a read-back of each setting. |
| **B5** | **Cloud test budget and per-number evidence.** *Half retired:* the user approved the technical design and the testing seams and requested implementation (§1.1), so AC #7's approval requirement is met. Still open: a budget to run B1/B2/B4, and the benchmark that turns each provisional number in §10/§12 into evidence. No approval is claimed for any number, and no cloud behaviour is claimed at all. | Scoped cloud/budget approval, then the B1/B2/B4 read-backs and the CPU benchmark for B3. |
| **B6** | **Exact or high-throughput admission ceiling.** GCS-only state offers one coordination object; it is a serialization point, so sustained contention surfaces as fail-closed `429` and a repair racing an un-persisted acquisition can transiently under-count. | Accept the single-object bound, or approve a transactional counter (Firestore/Spanner), which the GCS-only constraint excludes. |

Issue #1 has no issue-level blockers; it blocks #3. B5 is now a budget/evidence blocker only (its
approval half is retired, §1.1); B6 is a decision blocker; B1–B4 are evidence blockers.

---

## 14. References

Official Google Cloud documentation. Items 1, 3, and the Cloud Run limits in item 15 were re-read in
this session; the rest are cited from the rev 5 reference list (read 2026-11-08), not re-verified here.

1. Request preconditions — `ifGenerationMatch`/`ifMetagenerationMatch`, `412`; the `0` special case
   ("only proceeds if no object with the specified name exists in the bucket or if there are only
   noncurrent versions of the object in the bucket"); "The `0` value should only be used when writing
   object data: no other requests can succeed with a `0` precondition"; "If you use a metageneration
   precondition in an object request, you should always use a generation precondition as well."
   https://docs.cloud.google.com/storage/docs/request-preconditions
2. Consistency — strong read-after-write/update/delete and listing; single-object atomicity; batch and
   multi-object operations are not atomic. https://docs.cloud.google.com/storage/docs/consistency
3. Soft delete — enabled by default, retention 7 days (7–90; `0` disables); "preserves objects and
   buckets that get deleted or overwritten"; soft-deleted objects "cannot be read, modified, or
   overwritten"; cost caution for temporary data; retention changes propagate in up to 30 s.
   https://docs.cloud.google.com/storage/docs/soft-delete
4. Lifecycle Management — configuration changes "can take up to 24 hours to go into effect".
   https://docs.cloud.google.com/storage/docs/lifecycle
5. `jobs.run` — `POST /v2/{name}:run`; body `{validateOnly, etag, overrides}`; `Overrides =
   {containerOverrides[]{name,args[],env[],clearArgs}, taskCount, timeout}` with **no `image`**;
   `overrides` requires `run.jobs.runWithOverrides`; response is an `Operation`.
   https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run
6. Executions resource — `createTime` "acknowledged by the execution controller. It is not guaranteed
   to be set in happens-before order"; a `cancel` method exists (not used).
   https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions
7. Task timeout — default 10 min, max 168 h, per attempt; "not a guarantee of uninterrupted
   execution"; non-GPU tasks are live-migrated (`SIGTSTP`/`SIGCONT`).
   https://docs.cloud.google.com/run/docs/configuring/task-timeout
8. Maximum retries — per task, default 3, range 0–10.
   https://docs.cloud.google.com/run/docs/configuring/max-retries
9. Jobs retries and checkpoints — check for checkpoints on startup, keep tasks idempotent.
   https://docs.cloud.google.com/run/docs/jobs-retries
10. Container runtime contract — exit 0 on success and non-zero on failure, no port; `CLOUD_RUN_JOB`,
    `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_ATTEMPT`, `CLOUD_RUN_TASK_COUNT`.
    https://docs.cloud.google.com/run/docs/container-contract
11. Service-to-service auth — Google-signed ID token, `aud` = receiving service URL, caller needs
    `roles/run.invoker`; `X-Serverless-Authorization` "removes the signature before passing the token
    to the user container"; if both headers are present only that one is checked.
    https://docs.cloud.google.com/run/docs/authenticating/service-to-service
12. Object Versioning — "retains a noncurrent object version each time you replace or delete a live
    object version"; configuration changes need ≥30 s.
    https://docs.cloud.google.com/storage/docs/object-versioning
13. Instance autoscaling — at `max-instances`, requests pend up to 3.5× average startup time or 10 s,
    then fail with `429`. https://docs.cloud.google.com/run/docs/about-instance-autoscaling
14. Job parallelism — the maximum number of tasks in one execution that run in parallel.
    https://docs.cloud.google.com/run/docs/configuring/parallelism
15. Cloud Run quotas — "Maximum HTTP/1 request size: 32 MiB if using HTTP/1 server. No limit if using
    HTTP/2 server"; "Maximum time before timeout per request: 60 minutes"; "Maximum number of
    concurrent requests per instance: 1000". https://cloud.google.com/run/quotas

---

## 15. Sources and attribution

- `docs/ocr-service-spec.md` — the user-confirmed product scope and ticket breakdown, copied unchanged
  from the primary checkout into this repository by the design commit.
- `GLOSSARY.md` — the domain language this contract uses, likewise copied unchanged.
- `docs/ocr-service-design.md` — the interview record behind the spec, including the personal
  project/account/region/prefix decision; still untracked in the primary checkout, not in this commit.
- Issue #1 body and its eight acceptance criteria, read this session (`gh issue view 1 --comments`;
  zero comments).
- `docs/ocr-v1-technical-design.md` (rev 5) — the draft this revision replaces, kept for its revision
  log and reference details. Its rev 5 corrections (worker-lifetime guard, fail-closed admission) are
  preserved in the corrected arithmetic and the single-decrement rule; its rev 4 corrections are folded
  into §5 and §6.
- `app/ocr_contract.py` + `tests/test_ocr_contract.py` — a working-tree draft of the executable form of
  §2–§6, retained uncommitted for parent review (§11.1); not part of the design commit.

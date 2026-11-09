# OCR v1 — implementation-ready contract (rev 7, corrected by the independent protocol review)

Issue: `TU-thanakrit/dev-service-google-doc-ai` #1 *OCR v1: freeze contracts and recovery design*.
This file is the normative product/protocol contract. Mutable deployment facts are not part of that contract and live only in `deploy/ocr-v1/sandbox.toml`; the matching read-back is under `verification/deployments/`. Older project and permission references retained later in this document are historical evidence, not current configuration.

rev 6 replaced the rev 5 draft (`docs/ocr-v1-technical-design.md`), which is kept as history and marked
superseded. That replacement added no capability: every change was a contract correction, a removed
contradiction, or a claim downgraded to what can be proven.

rev 7 corrects six protocol defects that an independent review found in rev 6 (§0.1). It adds no
capability either: every change removes a race, a stale derived field, an unenforced pin, or an unproven
claim. Where a rev 6 rule was wrong it is replaced rather than annotated, and the retracted items are
named in §0.1 and §12. All six findings were checked against the rev 6 text before being accepted.

**Amendment (design commit).** §2–§6, the normative contract, are unchanged. This amendment records the
user's approval (§1.1), corrects the acceptance-criteria status to match it, commits the approved spec
(`docs/ocr-service-spec.md`) and `GLOSSARY.md` alongside this file, and marks the uncommitted draft
rules/tests (§11.1). No capability is added and no number is claimed. The user subsequently approved
updating `AGENTS.md`, and its in-repository alignment was applied; see the AC table.
The copied spec is byte-identical to the approved local original, so its status line ("technical design
and testing proposals below require review") predates this approval: §1.1 is the current status.

Labels: **[CONFIRMED]** user-confirmed · **[DIRECTION]** approved as a direction, not verified ·
**[DEFAULT]** this document's own proposed choice, not individually user-approved or verified (list §12)
· **[BLOCKED]** needs real GCP evidence, a benchmark, or approval
(§13).

Reading order: **§1–§8 are the normative contract** (what to build, and what must stay true).
**§0 and §9–§15 are informative** — change log, race review, test list, decision/veto table,
evidence gaps, references. If you want the short version, §2–§8 is it (~600 lines).

**Approval and verification status at contract freeze.** The user approved the technical design and testing seams S1–S3 and requested implementation (§1.1). The cloud statements and B1–B4 rows in this section record evidence gaps at that time; current deployment and acceptance facts are authoritative only in `deploy/ocr-v1/sandbox.toml`. The design commit was verified locally, and rev 7 changed only this document: `uv run pytest -q` → **66 passed** (the 30 existing tracked tests plus the 36 uncommitted draft
tests of §11.1) and `uv run ruff check .` → clean. The 30 existing tests are baseline evidence for the
*old* synchronous app, not evidence that this design works; the 36 draft tests encode rev 6 rules and are
stale under rev 7 (§11.1). Three load-bearing Google Cloud facts were re-read while drafting (request
preconditions, soft delete, Cloud Run request limits); the rest is cited from the rev 5 reference list
(§14). Rev 7 adds no code and no test: the corrected rules are written in §2–§6 and listed for the
implementation ticket in §11.2.

---

## 0. What rev 6 changes

The design-commit amendment (front matter, §1.1) is not listed below: it changes no contract text. This
section covers the rev 5 → rev 6 step only.

| # | Contradiction or gap in rev 5 | Resolution |
|---|---|---|
| 1 | The expiry gate read `expires_at` from `final.json`, which cleanup deleted before `job.json`, so a reader mid-pass had an undefined expiry. | **D33**: `job.json` carries the final-state projection `{state, finalized_at, expires_at, reason, fence}`; the gate reads it before any content, and `job.json` is deleted last among the job's objects. |
| 2 | Admission accounting was unspecified on the idempotent-retry path. | **D34**: the claim decides first; a nonfinal retry atomically ensures the same job ID is in the admission set before materialization, without adding a second slot. |
| 3 | A leaked admission could read high, but the direction of drift was never stated. | §6.4: only the arbiter decrements; abandoned admissions are corrected by repair. Drift is one-directional (over-count = over-reject) except the disclosed repair race. |
| 4 | Two CAS primitives were mixed (`ifGenerationMatch`, `ifMetagenerationMatch`), and renewal versus takeover was distinguishable only by re-reading the object. | **D36**: one primitive, `ifGenerationMatch` on the object being written, no metadata-only updates; renewal and takeover are the same conditional replace. |
| 5 | The 50 MiB provisional upload limit was unreachable: Cloud Run caps an HTTP/1 request at 32 MiB and the limit cannot be raised. | **D38**: ≤32 MiB by construction, 20 MiB provisional, or an explicit decision to serve HTTP/2. |
| 6 | Dedupe identity included the declared media type, so identical bytes with a different declared type would have been a `409`. | **D35**: content identity is `(SHA-256(bytes), byte_length)`; the declared type is a validation gate only. |
| 7 | `GET /v1/ocr/jobs/{id}` had no response table. | §2.2. |
| 8 | Per-team GCS IAM was implied by "`team_id` is the first path segment so IAM is team-scoped". | §7.3: isolation is application-enforced with one bucket-wide service account; that is the honest bound. |
| 9 | `final.json` versus `job.json` authority on the read path was ambiguous. | §3.3: `final.json` is authoritative for *state*, `job.json` for *existence and expiry*; divergence is repaired. |
| 10 | The deadline/cleanup arithmetic was argued in prose rather than checked. | The rev 5 formulas are kept verbatim and are now asserted functions with boundary tests (§11.1). |

### 0.1 Independent review of rev 6 → rev 7

Rows 1–3 of the rev 5 → rev 6 table above are themselves superseded: they introduced the projection and
the counter that rev 7 removes. Read them as history, not as current rules.

Six defects were raised by an independent review. Each was checked against the rev 6 text before being
accepted; all six are valid, and none is a style preference. Each row names the correction, which replaces
the affected rule rather than annotating it.

| # | Confirmed defect in rev 6 | Correction |
|---|---|---|
| 1 | The read gate and the cleanup guard took `expires_at` from the mutable `job.json` projection of §5.4 step 3. A crash between `final.json` and that flip leaves the projection `null`, so the gate can never expire the job and cleanup cannot compute its guard. | **D39**: the projection is deleted. `final.json.expires_at`, written once at finalization, is the only expiry authority; `job.json` keeps `accepted_at`/`deadline_at` only. With no `final.json`, the gate and the guard both use `deadline_at` and deny access there, and a `final.json` that is present but unreadable denies rather than serves. |
| 2 | The single counter's repair (enumerate non-final jobs → CAS-set the count) can race an acquisition that has not yet materialized, reading the enumeration low. No cross-object check makes a counter safe. | **D40**: the counter becomes an **admission set** keyed by `job_id` in the same single object — acquire adds, the `final.json` winner removes, entries past their own `expires_at` are pruned inside the same CAS write. No enumerating recount exists at all. |
| 3 | A stale worker could overwrite a `succeeded` page checkpoint, because rev 6 rewrote `pages/{n}.json` unconditionally. | **D41**: checkpoints are create-if-absent plus validation against the pin; only a checkpoint that fails validation may be replaced, conditionally on its read generation. Failed pages are never checkpointed, so they are retried. |
| 4 | The deadline was checked only at attempt start, so an attempt could finalize `succeeded` after `deadline_at`, against the spec's 24 h lifetime. | **D42**: the deadline is checked before every page and before finalizing; page calls are bounded by the deadline; a finalize at or after `deadline_at` can never be `succeeded`. |
| 5 | `task_timeout` and `max_retries` lived only in the mutable Cloud Run job template, so the guard's `per_attempt_timeout` and the retry bound were not job-frozen. | **D43**: both are pinned in `job.json`, the timeout travels as a dispatch override, and the template is re-read and compared (image, container name, args/env, `maxRetries`, `timeoutSeconds`) before `jobs:run`; a mismatch fails closed and drift is alerted. |
| 6 | A dedupe hit could return a job whose records cleanup had already removed, and the claim-to-materialize retry could resurrect a cleaned-up job. | **D44**: dedupe liveness uses the same authority as the read gate; a job record that is absent is never served, and materialization at the recorded `job_id` is bounded by `materialization_grace`; both delete and replace are generation- and `job_id`-conditional. |

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
- **[DEFAULT] rows (§12) are this document's own proposed choices.** Approval of an earlier draft does
  not imply that every later default was individually reviewed or approved. Benchmark-dependent numbers
  remain gated by measured results.
- **Review direction for rev 7.** An independent review raised six protocol defects in rev 6 (§0.1). The
  user directed that valid findings be corrected in place, fail closed, without cross-object fencing or
  naive cross-object checks and with one single-object atomic capacity protocol. That direction is
  recorded here; the specific rev 7 text is this document's own proposal and is not user-approved.

| Issue #1 acceptance criterion | Status |
|---|---|
| Versioned examples, routes, statuses, error codes, page numbering, text source, warnings, partial completeness, expired-result behavior, execution-vs-accuracy | Specified exactly, §2 (`schema_version` in every request-scoped body, §2.4–§2.7); approved (§1.1). Execution success is separated from accuracy by the constant `accuracy: "not_verified"`. |
| Transitions, one authoritative final-state timestamp, acceptance/deadline/expiry timestamps, idempotency conflicts, request/cleanup races | Specified, §3–§5, §9; approved (§1.1). |
| Concrete dispatch/reconciliation/cleanup; launch ack vs completion | Specified, §6; approved. Acknowledgment is diagnostic and no decision reads it (§6.1). |
| GCS conditional-write, claim/lease/fencing, checkpoint/result publication, idempotency, crash recovery, no cross-object transactions | Specified, §5; approved as design. **Integration verification pending — real-GCS behaviour unverified [B1]**; an evidence gap, not a specification gap. |
| Verified GCP caller→team mapping; headers must not grant identity | Specified, §7; approved as design. **Integration verification pending — deployment evidence unverified [B2]**. Identity comes only from a re-verified ID token; no identity header is read. |
| Engine/model/config and worker-revision pinning, executable across deployments | Specified, §8, including what is *not* guaranteed; approved as design. **Integration verification pending — enforcement unverified [B2]**. |
| Race/failure review with examples; approval of technical/testing choices; numeric limits benchmark-gated | Race review done, §9 (23 scenarios). Technical and testing choices approved (§1.1). Every numeric engine-dependent limit stays benchmark-gated [B3] — not approved and not claimed. |
| Align project instructions with the async OCR-only scope | Applied to `AGENTS.md` with the user's explicit approval. It distinguishes the planned OCR-only service from the existing expense app. README updates and retirement of `POST /v1/documents:process` belong to implementation tickets. |

**Issue #1 is specified but remains under review.** The user approved an earlier design direction and
the testing seams; later defaults and protocol changes require independent review. The `AGENTS.md` edit
was applied with explicit user approval. What is **not** met is evidence: B1 (real-GCS protocol), B2
(IAM/token/pinning), B3 (the numbers, benchmark-gated by the criterion itself), B4 (retention
configuration), and B6 (the admission set's straggler tail and its single-object throughput). Rev 7 is
the response to one independent review (§0.1) and has been read once, by the reviewer who raised the
findings; it needs a second pass over the changed sections before #1 is called complete. No cloud
behaviour is proven by this document or by the commit that adds it.

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
| `429` | Admission ceiling reached, admission unrecordable, or the capacity object unreadable/unparseable — all fail closed | Error `capacity_exceeded`, `Retry-After: 30` |
| `503` | No usable job exists and the partial record was removed, so the same key is retryable; or the dedupe decision could not be read, so no second job may be created | Error `storage_unavailable`, `Retry-After: 5` |

No `404` on this route: the idempotency record is team-scoped, so a submission only ever resolves a
job of the caller's own team. `202` is a launch acknowledgment, not evidence that a worker started,
that the platform accepted the launch, or that OCR succeeded; `202` with `acknowledged = false` is
normal.

`GET /v1/ocr/jobs/{job_id}` — evaluate the gate (§3.3) first:

| Status | When |
|---|---|
| `200` | `job.json` exists and the gate serves (any state) → status object |
| `404` | Unknown id, foreign-team id, or records removed by cleanup → `not_found` |
| `410` | Gate denies: `final.json` exists and `now ≥ final.json.expires_at`, or `final.json` is absent and `now ≥ deadline_at` → `job_expired` |
| `500` | `final.json` exists but cannot be read or fails validation → `internal_error` plus an alert; content is never served from an undecidable expiry |

`GET /v1/ocr/jobs/{job_id}/result` — same gate, then:

| Status | When |
|---|---|
| `200` | Job final (`final.json` exists) and the gate serves → result object |
| `202` | Gate serves and the job is not final (`accepted`/`running`) → status object, `Retry-After: 10` |
| `404` | Unknown, foreign, or cleaned up → `not_found` |
| `410` | Gate denies (same rule as above) → `job_expired` |
| `500` | `final.json` unreadable → `internal_error` plus an alert |

Never `403` for a foreign or cleaned-up job, so job ids cannot be probed across teams; that holds
only because lookup is strictly team-prefixed (§5.7, §7.3). `202` does not depend on `deadline_at` inside
the job's lifetime; at or after `deadline_at` the lifetime ceiling has passed and the gate denies `410`
(D39), while reconciliation finalizes the job in parallel. A missing published result *after* the gate
passed cannot come from cleanup (cleanup never starts before the guard): that is `500 internal_error`
plus an alert.

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

`state` and `timestamps.finalized_at`/`expires_at` are read from `final.json` when it exists, and are
`null` while the job is non-final; `accepted_at`/`deadline_at` come from `job.json`. No derived copy of
the final state is stored in `job.json` [D39].

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
- `expires_at = finalized_at + retention` (24 h), written once in `final.json` and nowhere else [D39].
  `null` until finalization; a job pending past `deadline_at` is finalized by reconciliation, so it is
  always eventually set. When `final.json` is absent at read time the deny instant is `deadline_at`
  (§3.3), which is never later than a granted `expires_at`.

**Exactly-once finalization** is create-if-absent on `final.json`: the first writer wins, every other
writer gets `412` and aborts. That — not the lease and not a fence — is what stops a stale worker from
replacing a final result, and it holds only while that object exists (§5.4). **Execution is
at-least-once**: the guarantee is one logical job per `(team_id, key)` and at most one published
result, never one execution per job.

### 3.3 One expiry rule

Evaluated **before any content object is read**, from two create-once records and nothing mutable [D39]:

1. `job.json` absent → `404 not_found` (also for a foreign team: lookup is team-prefixed).
2. `job.json` present, `final.json` present → `expires_at = final.json.expires_at` is authoritative:
   `now ≥ expires_at` → `410 job_expired`, no content read. A `final.json` that exists but cannot be
   read or fails validation is `500 internal_error` plus an alert; content is never served from an
   undecidable expiry.
3. `job.json` present, `final.json` absent → no expiry has been granted yet, so serve while
   `now < deadline_at` and deny `410` at or after `deadline_at`. That ceiling is the fail-closed deny
   instant, and it also covers the cleanup window in which `final.json` has been deleted but `job.json`
   has not, because `expires_at ≥ deadline_at` always holds (finalization never precedes acceptance).

`final.json` is the only authority for final *state* and for expiry; `job.json` is the authority for
*existence*, `accepted_at`, and `deadline_at`, all written once at creation and never rewritten. There is
no projection left to diverge, so there is no divergence to repair (§6.2). Ownership and expiry share one
source, which is why the code flips to `404` when cleanup finally deletes `job.json`. [CONFIRMED] The user
accepted `404` after cleanup, so no contentless stub is kept to make `410` permanent. What is guaranteed
is that access is denied at the applicable deny instant in both cases: a late cleanup cannot extend
access, and an early cleanup cannot grant it, because cleanup never starts before the guard (§6.3) and
content is deleted before records (D37).

### 3.4 Idempotency conflict rules

Key is `(team_id, Idempotency-Key)`; content identity is `(SHA-256(bytes), byte_length)` (D35).

Liveness is decided by the **same rule as the read gate (§3.3)**, so retention expires consistently on
both paths: a record is live iff its job record exists and the gate answers *serve* [D44].

| Existing record and its job record | Same content | Different content |
|---|---|---|
| Job exists, gate serves (non-final, or final with `now < expires_at`) | `200` with the existing job; no new work | `409 idempotency_conflict` |
| Job exists, gate denies (`now ≥ expires_at`, or no `final.json` and `now ≥ deadline_at`) | new job accepted (record replaced by CAS) | new job accepted |
| Job record absent, `now - claimed_at < materialization_grace` (5 min) | crash-window retry: materialize at the recorded `job_id` and return `200` with it — never a job that does not exist | `409` (the earlier claim still stands) |
| Job record absent, beyond the grace | stale record whose job was cleaned up: replace by CAS and accept a new job; **no resurrection** | new job accepted |
| A record or job read fails | `503 storage_unavailable`, `Retry-After: 5`: fail closed, no second job is created | same |

Both replacement and deletion are conditional on the generation read **and** on the record still naming
the `job_id` that was read; a mismatch means another actor has already moved the record, so the submitter
re-reads and re-decides. `materialization_grace` is the same 5-minute value reconciliation uses (§6.2)
[DEFAULT]; it is what distinguishes "the claim was created and the creator crashed before materializing"
from "cleanup removed a job that no longer exists".

The comparison uses the stored hash and the incoming hash, so it never reads the stored input object.
That matters because the input is deleted immediately on full success while the record lives until
`expires_at`: a retry after a successful job still gets `200` with the same job id. After the record
expires, deduplication is no longer guaranteed (spec).

---

## 4. Submission: claim, admission, materialization [D34]

Order: authenticate → validate → **idempotency claim** → **admission** → materialize `job.json` →
persist input → record dispatch intent → launch → respond.

1. **Claim** `idem/{team_id}/{key_hash}` with `ifGenerationMatch=0`, carrying a fresh `job_id`, the
   content identity, and `claimed_at`. Created → this caller owns the job (the `202` path).
   `412` → re-read (strong read-after-write) and decide by §3.4: same content and live → `200` path,
   adopt `existing.job_id` and ensure that job's admission before materialization;
   different content and live → `409`; denied or stale → CAS-replace at the generation read and only if
   the record still names the `job_id` that was read (on `412`, re-read and re-decide).
2. **Admission** (§6.4) on both new and nonfinal retry paths, before materialization: atomically ensure
   the set contains this `job_id`. An existing entry is an idempotent success, not another slot. If
   admission cannot be recorded, fail closed without materialization or dispatch. A final-job retry does
   not reacquire capacity. This covers a creator crash after the claim but before admission.
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
read at creation, and only objects it created itself; it also removes its own admission entry (§6.4),
and an entry it fails to remove self-expires. A retry that adopted another actor's `job_id`
must not delete that record — it belongs to a live job another actor may still be materializing, and
deleting it would lose deduplication and strand the job. A compensating actor that cannot finish does
nothing further; the repair falls to reconciliation's missing-input rule (§6.2).

Both the `202` and the `200` path run steps 3–5 for a job that §3.4 classifies as live or as a
crash-window retry, which makes the claim-to-materialize crash window harmless in one direction and
closed in the other: a same-key retry within `materialization_grace` materializes at the recorded
`job_id` (so a `200` never names a job that does not exist), and a retry after the grace replaces the
stale record instead of resurrecting a cleaned-up job [D44]. A `200` hit on an **already final** job
skips steps 3–5 entirely: nothing is materialized, nothing dispatched, and a deleted full-success input
is not re-uploaded (which would otherwise extend its retention).

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
read-modify-write against a known generation. `claim.json` carries the writer's `{owner, fence}` and is
written with the generation read from that same object; a page checkpoint carries
`job_id`/`page_number`/`engine_ref` and is create-if-absent (D41), so it needs no fence field at all.

**What it does not guarantee — exclusion between workers.** After a lease expires and another worker
takes over, the older worker may still run and can still CAS-write an object whose stored fence equals
its own, because the newer fence lives in the other worker's `claim.json`. No cross-object compare
closes that window. This document makes no "a stale worker cannot write" claim.

**What makes staleness harmless:** (1) `final.json` create-if-absent is the single arbiter; (2) the
published result has a fence-unique name, created once and never rewritten; (3) checkpoints are
create-if-absent and validated against the pin, so a stale worker cannot replace a completed page
(§5.5, D41); (4) `final.json` is read directly, so there is no derived state to repair. Residual cost:
duplicated OCR work, bounded by the lease TTL, the ≤2-attempt dispatch cap, `task_max_retries`, and
`deadline_at` — never a duplicated or mixed published result.

**At-least-once, one logical job.** The caller's unit of work is `(team_id, key)`: exactly one
`job.json` and at most one published result exist for it, and a duplicate submission never starts a
second logical job (§4, D1). What is not single is *execution* — after a lease takeover or a
re-dispatch, two executions of the same logical job may run concurrently, and both are idempotent
against the durable state: checkpoints are create-if-absent, `final.json` arbitrates the result, and the
capacity entry is a set keyed by `job_id` so a duplicate release is a no-op. The cost of an extra
execution is duplicated OCR work, bounded by the lease TTL, the `≤2`-attempt dispatch cap,
`task_max_retries`, and `deadline_at` — never a duplicated, mixed, or rewritten published result.

### 5.4 Finalization

1. Write `result.{fence}.json` with `ifGenerationMatch=0` — fence-unique, never rewritten.
2. Create `final.json` with `ifGenerationMatch=0` holding `{finalized_at, state, reason,
   result_object, fence, expires_at, schema_version}`. One writer succeeds; `412` means another already
   finalized. This is the **only** cross-writer arbiter, and `expires_at` is written here once as the
   sole expiry authority [D39].
3. Release the admission entry (§6.4) — only the winner of step 2 may. This write is **not** part of
   finalization's correctness: if it fails, or the winner crashes first, the entry self-expires and
   reconciliation may remove it once the job is final. Finalization writes no other object, and in
   particular it does **not** project the final state into `job.json` (D39 retracts D33).

Readers resolve a result **only** through `final.json.result_object`, and only when that name is exactly
this job's own `jobs/{team}/{job}/result.{fence}.json` — a name pointing at another job, outside the
job's directory, or at a different fence is corruption to fail closed on, not a path to follow. Final
state and expiry are read only from `final.json`; a `result.*.json` without a matching `final.json` is
unpublished and ignored (cleanup removes it by enumeration). Likewise a stored record whose embedded
identity is not the path it was read from (`job.json`'s `job_id`/`team_id`, a result's `job_id`) is
corruption, never this job's state. **Assurance limit:** the two steps that produce durable state are
independent single-object writes with no cross-object transaction, so a crash can leave a result without
a `final.json` (unpublished, ignored) or a capacity entry that was never released (over-reserved until
its own time bound). Nothing needs repairing, because no reader consults a derived projection. "Exactly
once" means **exactly one winning `final.json`**, not an atomic multi-object commit. The arbiter's
authority lasts as long as the object exists: once cleanup deletes `final.json`, a straggler could
create a new one at that name — closed by §6.3's guard and the worker rule below, not by any
cross-object property.

### 5.5 Worker contract

- Reads `job.json` **before writing any object**; if that read fails, or the job is already final
  (`final.json` exists), it writes **nothing** and exits non-zero [D30, D42]. After cleanup removed the
  records, a straggler finds no job to resume and creates nothing.
- **Never works at or after `deadline_at`** [D42]: the deadline, the non-final check, and the lease are
  re-checked before **every page**, and an attempt that starts at or after `deadline_at` writes nothing
  and exits non-zero. A finalize that happens at or after `deadline_at` can never be `succeeded`: the
  state is `partially_succeeded` when at least one page succeeded, otherwise `failed`, always with
  `reason: deadline_exceeded`.
- **Scoped page limits** [D42]: each page call is issued with a client-side timeout of
  `min(page_timeout, deadline_at - now)` and is abandoned at the deadline, so page work for a job ends by
  `deadline_at + page_timeout ≤ deadline_at + per_attempt_timeout` — one in-flight page is the last
  possible write, never a retry chain. A failed page is retried at most `page_retries` times inside the
  attempt (provisional 1).
- **Checkpoints** [D41]: one per **succeeded** page at `pages/{n:05d}.json`, written create-if-absent
  (`ifGenerationMatch=0`) and not rewritten as a rule, carrying `{job_id, page_number, status,
  text_source, text, warnings, engine_ref}`. A `412` means the page is already checkpointed: read it,
  validate it
  (`job_id` and `page_number` against the object name, `engine_ref` equal to the pin, `status ==
  succeeded`, text/warning shape), and use it. An invalid checkpoint is treated as absent and may be
  replaced **only** conditionally on the generation read from it — the sole rewrite of a page object, and
  it can only ever replace invalid content with valid content.
- **Failed pages are never checkpointed**, so the next attempt recomputes them: a transient engine
  failure is retried rather than frozen, and the finalizer reports whatever is still failed when it
  finalizes. Resumes from present checkpoints, and refuses to run if its resolved engine/config
  disagrees with `job.json` [D16].
- Exits `0` on success, non-zero on failure, and never listens on a port. Uses `CLOUD_RUN_JOB`,
  `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_*` for identity and log correlation.
- Runs under the **pinned** `task_timeout_s` and `task_max_retries` from `job.json` (D43); the dispatcher
  sends the timeout as an override and compares the template before launch (§6.1, §8). Application
  checkpointing is required alongside platform retries.

### 5.6 Crash recovery matrix

| Crash point | Recovery |
|---|---|
| After the claim, before `job.json` | the admission entry self-expires at `deadline_at + per_attempt_timeout`; a same-key retry inside `materialization_grace` materializes at the recorded `job_id` and after it the stale record is replaced (§3.4); with no retry nothing runs |
| After `job.json`, before the input | not usable: submitter compensates and returns `503`; else reconciliation finalizes `failed`/`internal_error` and conditionally deletes the `idem` record |
| After `job.json`, before `dispatch.json` | reconciliation dispatches it |
| After `dispatch.json`, before the launch returns | `acknowledged` stays `false`; reconciliation re-dispatches after ≥5 min when no live lease exists and attempts remain |
| After the lease, before any page | lease expires; the next attempt takes it over with `fence + 1` and recomputes |
| Holder dies holding the lease | same: takeover possible after ≤1 TTL |
| Between page checkpoints | the next attempt lists `pages/*` and resumes from present ones |
| After `result.{fence}.json`, before `final.json` | unpublished; the next writer or the reconciler finalizes and abandons the earlier object |
| After `final.json` | a second `final.json` gets `412` while the name exists and the published result is never rewritten; a missing admission release self-expires or is removed by reconciliation |
| Two writers reach the same page | the first create-if-absent write wins; the other validates the existing checkpoint and uses it, and can never replace a valid one (D41) |
| An attempt crosses `deadline_at` | page work stops at the deadline; the finalize is `deadline_exceeded` and can never be `succeeded` (D42) |
| Cleanup starts while a straggler still writes | no pass starts before `cleanup_guard`, which is at least `deadline_at + per_attempt_timeout` — the last instant any page write can land (§6.3, D42) |
| During cleanup, partially | idempotent and re-runnable; a failed precondition is skipped and re-decided next pass |

### 5.7 Bucket layout

One bucket, name prefixed `dev-ocr-service-*`; `team_id` is the first path segment, so listing,
audits, and application-scoped queries are team-scoped (IAM caveat in §7.3).

```
inputs/{team_id}/{job_id}
jobs/{team_id}/{job_id}/job.json            # create-if-absent: identity, pins, accepted/deadline;
                                            # later CAS only for state accepted->running
jobs/{team_id}/{job_id}/claim.json          # lease + heartbeat
jobs/{team_id}/{job_id}/dispatch.json       # create-if-absent intent; CAS-updated ack
jobs/{team_id}/{job_id}/pages/{n:05d}.json  # one checkpoint per succeeded page; create-if-absent
jobs/{team_id}/{job_id}/result.{fence}.json # published result; created once, never rewritten
jobs/{team_id}/{job_id}/final.json          # finalization arbiter + authoritative state and expiry
idem/{team_id}/{key_hash}
capacity/inflight.json                      # the admission set (§6.4)
```

Created `job.json` holds `{schema_version, job_id, team_id, accepted_at, deadline_at, content_hash,
byte_length, idempotency_key_hash, mime_type, engine_ref, worker_job_name, worker_revision_digest,
task_timeout_s, task_max_retries, worker_container_name, worker_args, worker_env, pages_total, state}`
[D43]: the pin of §8 (`engine_ref`, the worker job name and image digest, the container name, args,
env, and both the timeout and the retry count), identity and timestamps, the content identity with its
`mime_type`, the initial `state`, and `pages_total` — the source page count the admission byte scan
could see, `null` when it could not, written once and never rewritten, because a worker that refuses
the container accounts for exactly that many source pages (§2.6). The only later CAS update
changes `state` from `accepted` to `running` (plus `updated_at`). Identity, pins, and timestamps are
written once; a reader finding a changed identity field treats the record as corrupt, alerts, and fails
closed `internal_error`. Readers decide only from `accepted_at`/`deadline_at` here and from `final.json`
(D39). This layout — not a fixed deletion list — is the cleanup contract, because more than one
`result.{fence}.json` can exist and a crashed holder can leave `claim.json` behind.

---

## 6. Dispatch, reconciliation, cleanup, capacity

### 6.1 Dispatch [D6/D7/D14]

The API calls Cloud Run Admin `POST /v2/{name}:run` on the **version-pinned worker job** recorded in
`job.json`; the response is an `Operation`, recorded and not awaited. Overridable: `containerOverrides
[].{name,args,env,clearArgs}`, `taskCount`, `timeout` — there is **no `image` field**, and sending
`overrides` needs `run.jobs.runWithOverrides` [B2]. Dispatch sends `timeout` and `containerOverrides`
**from the job's pin**, never from the template (D43). Launch success is not processing progress; worker
completion is observed **only** from the GCS finalization record.

1. Create `dispatch.json` with `ifGenerationMatch=0` holding `{attempts: 1, last_attempt_at,
   acknowledged: false, execution_ref: null, last_error_code: null}`.
2. **Only the creator calls `jobs:run`**, and immediately before it re-reads the pinned job and compares
   its template against the pin: `containers[0].image == job.json.worker_revision_digest`, the pinned
   container name, `maxRetries == task_max_retries`, `timeoutSeconds == task_timeout_s`, and the pinned
   args/env are equal or absent (D43). Any mismatch fails closed (§8). A `412` on step 1 means another
   submitter already owns this attempt, so this caller does not dispatch — that is what stops a
   concurrent duplicate submission from launching two executions.
3. On a launch response, CAS-update `dispatch.json` at the generation read in step 1. Losing that CAS
   is harmless. `acknowledged`/`execution_ref` are diagnostic [D31]: an acknowledged operation can
   still fail or never start an execution.
4. A further attempt may be claimed only when **all** hold: non-final, `now < deadline_at`, ≥5 min since
   the last attempt, `attempts < 2`, no live lease. This is `may_dispatch()` in `app/ocr_contract.py`,
   asserted in §11.1; it deliberately takes no acknowledgment parameter.
5. A **non-retryable** launch error (`PERMISSION_DENIED`, `NOT_FOUND` for the pinned job, or any
   pin mismatch of step 2) does not retry: reconciliation finalizes `failed` with
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
| Non-final, last launch error non-retryable | finalize `failed`/`engine_unavailable` + alert (no second launch, §6.1 step 5) |
| Non-final, `now ≥ deadline_at` | finalize `deadline_exceeded` (§3) |
| Final job whose `job.json` still reads `accepted`/`running` | nothing to repair: readers take final state and expiry from `final.json` (D39), and the stale `state` field is never read for a decision |
| Non-final, input absent, `now - accepted_at ≥ 5 min`, no live lease | finalize `failed`/`internal_error`, alert (reconciliation is then the `final.json` winner, so it also releases the admission entry), delete the `idem` record only at the generation read and only if it still names this `job_id` |
| Expired live `idem` record whose job is final and past `expires_at` | delete, generation- and `job_id`-conditional (D44) |
| `capacity/inflight.json` entry whose job is final | remove by CAS: a final job starts no new page work, so removal cannot undercount (§6.4) |
| `capacity/inflight.json` entry past its own `expires_at` | pruned by the next writer inside its CAS write; reconciliation may prune it explicitly. There is no enumerating recount (D40) |
| Pinned worker job's observed template drifts from the pin (`image`, container name, `maxRetries`, `timeoutSeconds`, args/env) | alert and do not dispatch — fail closed (§8, D43) |
| Pinned worker job missing or its read denied | alert and do not dispatch; finalize the affected job `failed`/`engine_unavailable` when its dispatch path reaches that conclusion (§6.1 step 5) |

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
- **Guard.** No pass may start before `cleanup_guard = max(expiry_bound, deadline_at +
  per_attempt_timeout)`, where `expiry_bound = final.json.expires_at` when `final.json` exists and
  `deadline_at` when it does not (asserted in §11.1, D39). Derived, not guessed: no dispatch happens at
  or after `deadline_at`; an attempt is bounded by `task_timeout_s`; an attempt that starts at or after
  `deadline_at` writes nothing; and a page call is abandoned at the deadline (D42). So an attempt may
  start just before the deadline and land one in-flight page past it, and later attempts write nothing —
  the last possible write is `deadline_at + per_attempt_timeout`. Without the guard a straggler could
  re-create `pages/*`, an unpublished result, or a fresh `final.json` after the pass was reported
  complete.
- **Fail closed, never guess.** A `final.json` that is present but unreadable or invalid stops that job's
  cleanup for this pass — skipped, alerted, re-decided next pass — because deleting on an unknown expiry
  could destroy data still inside its retention window. A job whose `final.json` never appeared (a broken
  reconciliation) is still deleted once `now ≥ deadline_at + per_attempt_timeout`: access was already
  denied at `deadline_at` (§3.3), so nothing is owed to a reader.
- **1 h target.** `guard - expires_at = max(0, per_attempt_timeout - (finalized_at - accepted_at))`, so
  the guard exceeds `expires_at` only for jobs that finalize within one per-attempt timeout of
  acceptance. With the startup invariant
  `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` [D32], the first legal pass starts at
  ≤ `expires_at + 1 h` by construction. Residual: scheduler and execution latency, alerted, not
  guaranteed.
- **Order (D37).** Content, then records, `job.json` last among the job's objects: (1) `inputs/…`,
  every `pages/*.json`, every `result.*.json` — including results from workers that never won
  `final.json`, so a fixed list would leak; (2) `claim.json`, `dispatch.json`, `final.json`, then
  `job.json`; (3) `idem/{team_id}/{key_hash}`. The gate reads `job.json` for existence and
  `final.json` for expiry, so deleting content and `final.json` first cannot grant access: with
  `final.json` gone the deny instant is `deadline_at`, which is at or before every pass start. The
  `410`/`404` flip happens only after no content remains, and no reader is ever served content past its
  expiry.
- **Every delete is conditional, including the `idem` record.** A delete whose precondition fails is
  **skipped, not retried blind** — a mismatch means another actor replaced the object after this pass
  read it. This matters most for the record, whose name is reused: an expired record is CAS-replaced by
  a new submission with the same key and different content, so a name-addressed delete would remove a
  **live** record for a **new** job, silently losing deduplication and stranding that job. The record
  is deleted only when the generation still matches the one read **and** its `job_id` still names this
  job (D44). Cleanup is idempotent; a partial pass is completed by the next one.

### 6.4 Global capacity: one serialized admission set [D40]

One coordination object, `capacity/inflight.json`, holding the whole admission state:

```json
{"schema_version": "ocr.capacity.v1", "updated_at": "2026-11-08T04:31:07Z",
 "entries": {"6d1f9b2a-…": {"admitted_at": "…", "deadline_at": "…", "expires_at": "…"}}}
```

`entries` is keyed by `job_id`, and `expires_at = deadline_at + per_attempt_timeout` is the instant after
which the job can run no page work at all (§5.5), so every entry is a self-limiting reservation. Size is
bounded twice over: a startup invariant asserts `ceiling × max_entry_bytes ≤ capacity_object_budget`
(provisional 256 KiB) and pruning only ever shrinks the set.

- **Acquire** on the `202` path only, after the claim, before materialization. Loop ≤5: read
  `(generation, doc)`; **prune** every entry with `expires_at ≤ now`; if `job_id` is already present,
  this submission already holds its admission and the acquire is idempotent; if
  `len(entries) ≥ ceiling`, fail closed `429 capacity_exceeded` with no write; otherwise CAS-write the
  pruned set plus the new entry at the generation read. On `412`, re-read and re-decide; on exhaustion,
  `429` (fail closed). An unreadable, unparseable, or schema-mismatched object is `429` plus an alert
  (fail closed): there is no automatic rebuild, because any rebuild that could read low would reintroduce
  the defect this design removes.
- **Release** by the `final.json` winner only (§5.4 step 3), plus the claim creator on its own `503`
  compensation path (§4). Release is idempotent: read, prune, CAS-write the set without `job_id`. A
  missing entry means another actor already released it, which is success rather than an error. A release
  that cannot be written is **not** a job failure: the entry self-expires at its own `expires_at`, and
  reconciliation may remove an entry whose job is already final.
- **Crash recovery.** Every leak is time-bounded: a submitter that dies after acquiring and before
  materializing, and a winner that dies before releasing, both leave an entry that the next writer prunes
  at `expires_at`. There is no enumerating recount anywhere, so no repair path can read the set low.
- **Why no race can undercount.** The set replaces a counter: admission and removal are keyed by
  `job_id`, so a duplicated release is a no-op instead of a second decrement, and pruning is a pure
  function of time applied inside the same CAS write. No interleaving lets a reader observe fewer live
  admissions than exist. Over-reservation (over-reject) is the only drift the protocol itself can
  produce. One residual is stated rather than hidden: a job that finalizes while an older holder is still
  alive stops being counted, and that straggler's remaining page is not counted — bounded by one page per
  such job (`page_timeout`) and disclosed in B6.
- **Throughput:** the object is one serialization point rewritten by every submission, release, and
  prune; sustained contention surfaces as fail-closed `429`, so its throughput bounds the submission
  rate. The ceiling value is benchmark-gated [B3]. The API is additionally capped by `--max-instances`,
  where Cloud Run queues briefly then fails with `429`; that is not relied on for the document ceiling,
  but a client can see a `429` this design did not produce [D21].

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

**Mapping (§7.3).** A deployed allowlist maps a verified principal to an internal `team_id`, loaded from
configuration at startup. A key binds the **pair** of verified claims — the ID token's `email` and its
`sub`, written `<email>|<subject>` — so an allowlisted address presented with another subject is
unmapped, and a reused or forged address cannot borrow a team. A key without both claims is a startup
failure. Unmapped →
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

Recorded once in `job.json` and frozen for the job and all its retries [D43]: `engine_ref` (engine id,
model id, config version/hash, pre-/post-processing parameters), `worker_revision_digest` (the worker
image by immutable digest, never a tag), `worker_job_name`, `worker_container_name`, `worker_args`,
`worker_env`, `task_timeout_s`, `task_max_retries`, `dispatch_attempt_cap`, `schema_version`, and any
decode parameter that affects output bytes. `engine_ref` is operator-only [D19]. `task_timeout_s` is
pinned here rather than left to the template because §6.3's guard and §5.5's page bound are computed from
it, and `task_max_retries` is pinned because it sets the duplicated-work bound; the startup invariant
`task_timeout_s + cleanup_schedule_period ≤ 1 h` (D32) keeps the 1 h cleanup target reachable.

**A Cloud Run job is mutable state, and this design does not pretend otherwise.** It can be updated,
re-pointed, or deleted in place; its template is the launch surface, not an immutable artifact. The
design therefore pins the executable, the entrypoint, and the timeouts in `job.json`, and enforces them
at dispatch:

- the **image digest** is pinned and compared immediately before `jobs:run`;
- the pinned **timeout** travels as a dispatch override, so the attempt is bound by the job's pin even if
  the template changed;
- `maxRetries`, the container name, and the pinned args/env are compared against the pin in the same
  pre-launch read; any mismatch, missing job, or denied read is non-retryable
  `failed`/`engine_unavailable` with an alert — fail closed (§6.1 step 2);
- reconciliation alerts on observed template drift and does not dispatch a drifted job (§6.2).

**One job per revision** [D18]: each worker revision gets its own Cloud Run Job
`dev-ocr-service-worker-<slug>` created with that revision's image by digest. Dispatch runs *that* job
by name, with overrides carrying only the pinned `env`, `args`, and `timeout`; an image cannot be
selected at dispatch time, so the versioned job name *is* part of the pin. Re-pointing it is detected,
not prevented.

**Detection, not a lock.** Immediately before `jobs:run` the dispatcher re-reads the pinned job and
compares image, container name, `maxRetries`, `timeoutSeconds`, and args/env against the pin; a
mismatch, a missing job, or a denied read is the non-retryable `failed`/`engine_unavailable` case with
an alert (§6.1 step 2). Being read-then-act, a job re-pointed between the read and the call still runs
the wrong template: the wrong executable, or a wider platform-retry bound. The `jobs:run` request
accepts an `etag` documented to "detect modification conflict", but its run-time failure semantics are
not documented for this call and are not assumed here [B2]. The residual is bounded and stated: for the
image, a wrong-executable execution; for `maxRetries`, up to the new value's extra task attempts of
duplicated work, alerted by drift detection — neither affects the published result, access, or the
cleanup guard, because the attempt timeout travels as an override and the guard reads `task_timeout_s`
from the pin.

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
    exists, the published result is never rewritten, its page writes fail create-if-absent against the
    existing checkpoints, and its extra objects are removed by enumeration. Once cleanup deleted the
    records the `412` no longer protects the name; §6.3's guard closes that window, not the arbiter.
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
17. **Admission drift or contention** — an unrecordable, unparseable, or over-ceiling admission is
    rejected `429` (fail closed). The only drift is over-reservation; the only gap to actual concurrent
    page work is the disclosed one-page straggler tail (B6).
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
24. **Two workers reach the same page** — the first `pages/{n}.json` create wins; the second reads and
    validates it, uses it, and writes nothing. A loser holding a *failed* page result cannot replace a
    `succeeded` checkpoint.
25. **An attempt crosses the deadline** — page work stops at `deadline_at`, the in-flight page is
    abandoned at the deadline, and the finalize is `deadline_exceeded` with `partially_succeeded` when
    usable pages exist. A job finalized at or after `deadline_at` is never `succeeded`.
26. **A pinned job is re-pointed or its observed template drifts** — the pre-launch comparison refuses
    to dispatch and fails closed `failed`/`engine_unavailable`; a change landing between the read and
    the call is the disclosed read-then-act residual (§8) and cannot affect the published result or the
    cleanup guard.
27. **A submission finds an `idem` record whose job was cleaned up** — it never receives a job that
    does not exist: inside `materialization_grace` it materializes at the recorded `job_id`, beyond it
    the stale record is replaced and a new job is accepted (D44).
28. **The capacity object is corrupt** — submissions fail closed `429` with an alert; nothing is
    admitted and no automatic rebuild runs (D40).

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
  20 MiB upload, 500 pages/frames, 30 min per attempt (`task_timeout_s`), `task_max_retries` 0
  (application dispatch attempts own retry policy; `0` means Cloud Run tasks run once without
  uncontrolled platform retries),
  `page_timeout` 5 min, `page_retries` 1, `materialization_grace` 5 min, capacity object budget 256 KiB,
  worker parallelism 1, API `max-instances` 4, ceiling value [B3].
- **The page cap and the per-attempt timeout are one pair, not two numbers.** 500 pages at the
  provisional `page_timeout` cannot finish inside one 30-minute attempt, so a large document is expected
  to finalize `partially_succeeded` on the deadline path until the benchmark fixes a per-page cost and
  the pair is re-derived together (B3). No value here is approved.
- **Memory bound.** The body is buffered and hashed in full before the idempotency decision (the hash
  *is* the content identity), so worst-case memory per instance is roughly `max_upload_bytes` plus
  multipart overhead, times `max-instances`. The 20 MiB cap leaves headroom under the 32 MiB ceiling on
  a 512 MiB-class instance; raising it, or serving HTTP/2 to escape the ceiling, is a
  benchmark-and-configuration decision, not a contract change.
- **D32 startup invariant** `per_attempt_timeout + cleanup_schedule_period ≤ 1 h`, asserted at startup
  and in §11.1 (provisional 30 + 15 min).
- **Capacity invariants**, asserted at startup and in §11.1: `ceiling × max_entry_bytes ≤
  capacity_object_budget`, every entry satisfies `expires_at = deadline_at + task_timeout_s`, and the
  set never exceeds `ceiling` entries.

---

## 11. Seams and tests

**Approved seams [CONFIRMED S1–S3]** (§1.1), per the `tdd` rule that no test is written at an unconfirmed
seam: **S1** public lifecycle through the HTTP API; **S2** storage coordination (claim/materialize,
lease CAS, checkpoint and result publication, finalization arbiter, dispatch intent, the capacity
admission set, cleanup predicates); **S3** the application-owned recognition adapter, exercised with a fake
engine. A worker-entrypoint seam is deliberately *not* added: its behavior is observable through S1 and
S2.

### 11.1 Draft rules, executable locally, not committed (no cloud, no sleep)

**Not committed by the design commit, and not updated by rev 7.** In the working tree,
`app/ocr_contract.py` holds the decidable rules of §2–§6 and `tests/test_ocr_contract.py` asserts them;
both are left uncommitted deliberately, pending parent review, and rev 7 does **not** touch them: they
are a speculative draft rather than an approved seam, and the user directed that they stay unedited.
They assert pure decision functions directly rather than behaviour through the approved S1–S3 seams, so
they are not evidence for S1–S3, and the committed contract stands on §2–§6 alone.

**They also now encode rev 6 rules, so these assertions are stale under rev 7** and must not be read as
evidence for it: the expiry gate (which read a `job.json` projection instead of `final.json` with the
`deadline_at` fallback), the counter-based admission (single decrement, enumerating repair) instead of
the admission set, the fence-tagged checkpoint acceptance instead of create-if-absent plus validation,
and the deadline checked only at attempt start instead of before every page and before finalizing. Rev
7's rules have no executable form yet; they belong to the implementation ticket at the S1–S3 seams, with
the tests listed in §11.2. Local evidence from the design-commit session, **about the rev 6 draft
only**: `uv run pytest tests/test_ocr_contract.py -q` → 36 passed; `uv run pytest -q` → 66 passed (30
existing + 36); `uv run ruff check .` → clean. That draft covers, for rev 6: configuration invariants
(upload ceiling versus the platform limit, D32, heartbeat < lease); `deadline_at`/`expires_at`/
`cleanup_guard` including the early-finalization case and the proof that the guard stays within 1 h of
`expires_at` under D32; the expiry gate at its boundary (`now == expires_at` → expired, record absent →
gone) and that it takes no caller-identity parameter; idempotency `create`/`reuse`/`conflict`,
byte-length mismatch, and dedupe ending exactly at expiry; `may_dispatch` bounds (deadline, live lease,
5-minute spacing, 2-attempt cap) and that it has no `acknowledged` parameter; deadline finalization
preserving usable pages; admission fail-closed at the ceiling and on CAS exhaustion, the single-release
rule, exact repair; container sniffing, declared-type mismatch, gate order and codes,
`Idempotency-Key` format; the closed error vocabulary with no message leaking a path, bucket, or URL;
page rules (embedded-text usability, `text_source`, trim, combined-text order and separator, warning
union, completeness, 1-based contiguity, closed vocabularies).

### 11.2 Actionable tests for the implementation (not yet writable)

Each line is one test: setup → action → assertion. They reuse the existing injected-dependency and
`TestClient` approach, a fake recognition engine, a controllable dispatcher, an injected fake clock,
and an in-memory store that emulates generations and `412`. Fakes do **not** prove real GCS behavior
[B1].

Items (1)–(18) were written against rev 6; the lines touching the expiry gate, checkpoints, the capacity
counter, or the deadline are superseded by the rev 7 additions below.

S2 — storage coordination: (1) two concurrent submissions, same key and content → one `idem` create,
both return the same `job_id`, exactly one job directory; (2) same key, different bytes → one `409`,
winner untouched; (3) crash after the claim, before `job.json` → the next same-key request materializes
at the recorded `job_id` inside `materialization_grace`, never returns `200` for a nonexistent job, and
replaces the record beyond the grace instead of resurrecting a cleaned-up job; (4) input write returns `412`
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
failures on admission → `429` and no job object; (16) a second finalizer and a reconciler release cannot
double-release a `job_id` entry; (17) an abandoned admission self-expires and is pruned by the next
writer, with no recount; (18) crash at each row of §5.6 → restart recovers to the same observable state.

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

**rev 7 additions** (each line one test, at the same seams; none is written yet):

- gate authority: seed `final.json` with an expiry → `410` at `now == expires_at`; delete `final.json`
  and keep `job.json` → `410` at `now == deadline_at` and `200`/`202` just before it; a truncated
  `final.json` → `500` + alert, with no content served.
- no projection: finalize a job and assert `job.json` is unchanged apart from the `accepted`→`running`
  flip, so finalization wrote no second object.
- admission set: two concurrent submissions → two entries and no loss; a duplicated release is a no-op;
  a release racing a prune leaves a consistent set; over-ceiling → `429` with no write; the next acquire
  prunes expired entries; `ceiling × max_entry_bytes ≤ budget`.
- admission crash: a submitter that dies after acquiring leaves an entry that self-expires at
  `deadline_at + task_timeout_s`; reconciliation removes an entry whose job is final.
- corrupt capacity object → `429` + alert, and no admission is granted.
- checkpoints: two workers on one page → the first create wins, the second validates and reuses it, and
  the published text is the first writer's; a stale worker's failed page cannot replace a succeeded
  checkpoint; an invalid checkpoint is replaced only at its read generation.
- page retry: a transient engine failure on page 3 in attempt 1 (no checkpoint written) is recomputed in
  attempt 2 and the job is `succeeded`.
- deadline: with the clock at `deadline_at - ε` and a slow engine, the page call is abandoned at the
  deadline, no finalize produces `succeeded`, and `reason == "deadline_exceeded"`.
- pin: a template whose image, `maxRetries`, or `timeoutSeconds` differs from the pin → dispatch
  refuses, `failed`/`engine_unavailable` + alert; the dispatch request carries `task_timeout_s`.
- idem authority: a record whose `job.json` was deleted → materializes at the recorded `job_id` inside
  `materialization_grace`, and beyond it the record is replaced and a new job accepted; no `200` ever
  names an absent job.
- idem conditionality: a replace or delete whose generation or `job_id` no longer matches is skipped.

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
confirmations. D39–D44 were added by the rev 7 review pass and inherit the review direction in §1.1, not
individual user approval.

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
| D13 | ~~Admission ceiling via one CAS counter, **fail-closed**; release only by the `final.json` winner; exact enumeration repair.~~ Superseded in rev 7 by D40: the enumerating repair can read low. | [DEFAULT] |
| D14 | `task_max_retries = 0` pinned in `job.json` (D43; tasks run once per launched execution so application dispatch attempts rather than uncontrolled platform retries govern retry policy); ≤2 dispatch attempts, ≥5 min spacing, lease check, `now < deadline_at`; non-retryable launch error → `failed`/`engine_unavailable` + alert. | [DEFAULT] |
| D15 | Lease TTL 5 min, heartbeat 60 s; acquire = create-if-absent, expired lease taken over by CAS-replace with `fence + 1`, `412` stops the holder, release = conditional delete; checkpoints carry no fence (D41). | [DEFAULT] |
| D16 | Result published as fence-unique `result.{fence}.json`; `final.json` arbitrates; correctness does not depend on fence-tagged resumption. Checkpoint acceptance and rewriting are governed by D41. | [DEFAULT] |
| D17 | Bucket: soft-delete retention `0`, versioning disabled, no locks, no lifecycle rule as the deletion mechanism. | [DEFAULT] |
| D18 | One Cloud Run Job per worker revision, image by digest; dispatch re-reads and compares image, container name, `maxRetries`, `timeoutSeconds`, and args/env (detection, not a lock); mismatch/`NOT_FOUND`/`PERMISSION_DENIED` → `failed`/`engine_unavailable` + alert; drift is alerted. | [DEFAULT] |
| D19 | `engine_ref` is operator-only, never exposed to callers. | [DEFAULT] |
| D20 | Allowlisted log fields only; fixed per-code error messages; no content, keys, or tokens. | [DEFAULT] |
| D21 | Provisional limits: 500 pages, 20 MiB upload, `task_timeout_s` 30 min, `task_max_retries` 0, `page_timeout` 5 min, `page_retries` 1, `materialization_grace` 5 min, capacity object budget 256 KiB, worker parallelism 1, API `max-instances` 4, ceiling value (benchmark-gated). | [DEFAULT] |
| D22 | Non-success finalization carries `reason` ∈ {`deadline_exceeded`, `engine_unavailable`, `input_unreadable`, `internal_error`}; `null` on success. | [DEFAULT] |
| D23 | Text rules: per-page trim; combined = successful pages joined by `\n`; empty success `text: ""` + `no_text`; failed page `text: null` + `error_code`. | [DEFAULT] |
| D24 | One page entry per PDF page / TIFF frame; JPEG and PNG are single-page. | [DEFAULT] |
| D25 | A record is live while the job is non-final or `now < expires_at`; absent or expired → new job, record replaced by CAS. Liveness is decided by the read gate's authority and the absent-job case is bounded by `materialization_grace` (D44). | [DEFAULT] |
| D26 | Cleanup: enumerated prefixes, every delete generation-conditional (the `idem` record also matched by `job_id`), idempotent, refused before the guard, and refused for a job whose `final.json` exists but is unreadable; full-success input deleted immediately. | [DEFAULT] |
| D27 | `AGENTS.md`/README/five-template-contract alignment and retirement of `POST /v1/documents:process` happen inside the approved implementation, not in this ticket. | [DEFAULT] |
| D28 | Reconciliation also dispatches materialized non-final jobs with no `dispatch.json`. | [DEFAULT] |
| D29 | An input `412` means "already present" (size-verified) and never triggers compensation; compensation is limited to the claim creator's own objects and record. | [DEFAULT] |
| D30 | A worker reads `job.json` before writing anything and writes nothing if that read fails or the job is already final; it works only while `now < deadline_at` (D42); it releases its lease on completion or give-up; cleanup removes an unreleased lease. | [DEFAULT] |
| D31 | `dispatch.acknowledged` and `execution_ref` are diagnostic: no decision reads them. | [DEFAULT] |
| D32 | Startup invariant `per_attempt_timeout + cleanup_schedule_period ≤ 1 h` (provisional 30 + 15 min), asserted at startup. | [DEFAULT] |
| D33 | ~~`job.json` carries the final-state projection `{state, finalized_at, expires_at, reason, fence}`; it is the existence+expiry source for the read gate and is deleted last among the job's objects.~~ **Retracted in rev 7 by D39**: a derived projection read for a decision is exactly the defect it caused. `job.json` is still deleted last among the job's objects (D37). | [DEFAULT] |
| D34 | Submission order: authenticate → validate → claim → admission (new jobs only) → materialize → input → dispatch intent → launch. | [DEFAULT] |
| D35 | Content identity is `(SHA-256(bytes), byte_length)`; the declared media type is a validation gate, not part of dedupe. | [DEFAULT] |
| D36 | Every mutation is one conditional write with `ifGenerationMatch` on the same object; no metadata-only updates; renewal and lease takeover are the same conditional replace. | [DEFAULT] |
| D37 | Cleanup order: content → `claim.json` → `dispatch.json` → `final.json` → `job.json` → `idem` record. | [DEFAULT] |
| D38 | Upload cap ≤ the 32 MiB Cloud Run HTTP/1 request ceiling; 20 MiB provisional, or an explicit decision to serve HTTP/2. | [DEFAULT] |
| D39 | No final-state projection: `final.json.expires_at` (written once) is the sole expiry authority for the read gate, the dedupe decision, and the cleanup guard; when `final.json` is absent all three use `deadline_at` and deny there; a present-but-unreadable `final.json` denies (`500` + alert) instead of serving. | [DEFAULT] |
| D40 | Capacity is one single-object admission set keyed by `job_id`, entries self-limiting at `deadline_at + task_timeout_s` and pruned inside the same CAS write as the acquire or release; bounded by the ceiling and by an asserted byte budget; unreadable/corrupt, over-ceiling, or CAS exhaustion → `429` (fail closed); no enumerating recount. | [DEFAULT] |
| D41 | Page checkpoints are create-if-absent with pin, `job_id`, and `page_number` validation; only a checkpoint that fails validation may be replaced, conditionally on the generation read from it; failed pages are never checkpointed and are recomputed by the next attempt. | [DEFAULT] |
| D42 | The deadline is checked before every page and before finalizing; each page call is bounded by `min(page_timeout, deadline_at - now)`; an attempt starting at or after `deadline_at` writes nothing; no finalize at or after `deadline_at` can be `succeeded`. | [DEFAULT] |
| D43 | `task_timeout_s`, `task_max_retries`, container name, args, and env are pinned in `job.json`; the timeout travels as a dispatch override; the template is compared against the pin immediately before `jobs:run` and a mismatch or denied read fails closed; observed drift is alerted and not dispatched. | [DEFAULT] |
| D44 | Dedupe liveness uses the read gate's authority; an absent job record is never served as a `200`; materialization at the recorded `job_id` is bounded by `materialization_grace` (5 min), after which the stale record is replaced; delete and replace are generation- and `job_id`-conditional; an undecidable read is `503`. | [DEFAULT] |

Not reintroduced from earlier revisions: the `queued` state, the `meta.json`/`state.json` split, the
`dispatch_unavailable` and `job_not_finalized` codes, the `containerOverrides.image` claim, the
cross-object fence rule, "`410` is independent of cleanup", the 50 MiB default limit, and the per-team
GCS IAM implication. Retracted in rev 7: the `job.json` final-state projection (D33), the counter-based
admission with enumerating repair (D13), and fence-tagged checkpoint rewriting (D16's checkpoint half).

---

## 13. Remaining blockers

Nothing here can be retired by writing more design.

| ID | Blocker | What closes it |
|---|---|---|
| **B1** | **Real GCS protocol verification.** (a) Does `ifGenerationMatch=0` succeed when only a *soft-deleted* version of the name exists? (b) Do concurrent CAS writes to one object reliably yield `412`, including concurrent create-if-absent? (c) Does a `0`-precondition create fail against a *noncurrent* version (the documented reason versioning must be off)? (d) Does a conditional delete return `412` against an object replaced since the read, as cleanup and lease release assume? | An approved cloud test budget against a scratch bucket. §5 is a design, not a verified protocol, until then. |
| **B2** | **Credentials, IAM, deployment facts.** Does the API SA hold `run.jobs.runWithOverrides` and `run.jobs.get`? Can the worker SA write the bucket? What is the real n8n identity-token path? Do the bucket, prefix, versioned jobs, and Artifact Registry retention exist? Is the versioned job's template readable at dispatch (for the D43 pin comparison) and is such a job protected from in-place update or deletion? What are the run-time semantics of the `jobs.run` `etag` — can it close the read-then-act window? Is reading execution state wanted for earlier stranded-execution classification? | Scoped approval to provision and run a live IAM/token check. No deployment exists today. |
| **B3** | **CPU benchmark and every numeric limit** — engine selection, max bytes/pages, per-attempt timeout, worker CPU/memory, ceiling, retry counts, lease/heartbeat, and the D32 pair. If the benchmark needs `per_attempt_timeout + cleanup_schedule_period > 1 h`, the 1 h cleanup target and the stale-worker guard conflict and the user must choose; this document will not silently prefer one. | The approved CPU benchmark (spec "Release-blocking technical work" #1). |
| **B4** | **Retention, erasure, operational configuration.** Actual soft-delete and versioning state, absence of locks/replication/backups, project recovery windows, cleanup alerting mechanism, log retention. | Approval to inspect/provision the real bucket, then a read-back of each setting. |
| **B5** | **Cloud test budget and per-number evidence.** *Half retired:* the user approved the technical design and the testing seams and requested implementation (§1.1), so AC #7's approval requirement is met. Still open: a budget to run B1/B2/B4, and the benchmark that turns each provisional number in §10/§12 into evidence. No approval is claimed for any number, and no cloud behaviour is claimed at all. | Scoped cloud/budget approval, then the B1/B2/B4 read-backs and the CPU benchmark for B3. |
| **B6** | **Admission set residual and throughput.** The set removes the counter's undercount race, but three things remain: (a) the only over-subscription is the straggler tail — a job that finalizes while an older holder is still alive stops being counted while that straggler finishes one page (`≤ page_timeout`); (b) the object is one serialization point, so sustained contention surfaces as fail-closed `429`; (c) a corrupt object fails closed with no automatic rebuild, so an additive operator rebuild (from the enumerated in-flight set) is a runbook item. | Accept the bounded tail, the single-object throughput, and the manual-rebuild runbook, or approve a transactional store (Firestore/Spanner), which the GCS-only constraint excludes. |

Issue #1 has no issue-level blockers; it blocks #3. B5 is now a budget/evidence blocker only (its
approval half is retired, §1.1); B6 is a decision blocker; B1–B4 are evidence blockers. Rev 7's
corrections are design text only: they retire no blocker and add no evidence, and the second review pass
§1.1 asks for is the nearest thing to closing work that does not need a cloud call.

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
  preserved in the corrected arithmetic and the fail-closed admission rule — rev 7 replaces
  single-decrement with the admission set (D40); its rev 4 corrections are folded into §5 and §6.
- `app/ocr_contract.py` + `tests/test_ocr_contract.py` — a working-tree draft of the executable form of
  §2–§6, retained uncommitted for parent review (§11.1); not part of the design commit.
- The independent protocol review of rev 6 — six findings, all confirmed and corrected in place (§0.1).
  It is a review input, not evidence: no cloud behaviour was tested to produce it.
- Issue #1 body and its eight acceptance criteria, read this session (`gh issue view 1 --comments`;
  zero comments).

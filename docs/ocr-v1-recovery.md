# OCR v1 — page checkpoints, bounded page retries, partial results (issue #8)

Status: implemented and covered by offline tests at the approved seams; **no
cloud or real-engine behaviour is verified**. This slice implements the page
parts of `docs/ocr-v1-contracts.md` §2.5–§2.6, §5.5, §5.6, D41, and D42, using
the fake engine only. Nothing here is a claim about a deployed service.

## What changed

| Module | Role |
|---|---|
| `app/ocr_tracer.py` | per-page engine seam (`page_count` + `recognize_page`), bounded page retry, checkpoint-aware page loop, partial/complete/failed finalization |
| `app/ocr_store.py` | `pages/{n:05d}.json` checkpoints: create-if-absent, validated against the name and the pin |
| `app/ocr_worker.py` | the fake worker engine is now per-page; an attempt that publishes nothing exits non-zero |
| `app/ocr_reconcile.py` | names the deadline-page gap instead of leaving it implicit |

The synchronous expense app (`app.main`) is untouched.

## Page checkpoints (D41)

One checkpoint per **succeeded** page at
`jobs/{team_id}/{job_id}/pages/{n:05d}.json`, holding
`{schema_version, job_id, page_number, status, text_source, text, warnings,
engine_ref}`, written create-if-absent and never rewritten as a rule.

- A `412` means the page is already checkpointed: the existing object is
  validated (`job_id`, `page_number`, `status == succeeded`, text/warning shape,
  and `engine_ref` equal to the job's pin) and reused. The **first writer's page
  wins**, so a stale, duplicate, or recovering worker cannot replace a page that
  already succeeded.
- An object that fails validation is treated as absent, so the page is redone,
  and the object may be replaced **only** conditionally on the generation just
  read — the one rewrite the contract allows.
- **Failed pages are never checkpointed**, so the next attempt recomputes them.

## Engine seam (S3)

Recognition is per page: `page_count(content, mime_type)` decodes the container
for its source page count, and `recognize_page(content, mime_type, page_number)`
recognizes one page. That is what lets a restart re-issue **only** the pages
without a valid checkpoint instead of repeating successful recognition. The fake
engine is fault-injectable: one scripted outcome per page (an `EnginePage`, an
`EnginePageError` for a page failure, or any exception for a crash), with a
per-call list when a retry needs scripting.

`page_count` stands in for container decoding, which belongs with the real engine
(ticket #7). The per-page timing model (`page_timeout`, gave-up-at-deadline calls
issued with a client-side timeout) is implemented (ticket #11): `page_count` and
`recognize_page` take a `timeout_s` derived from the job deadline and the live lease
window, a page's retries and every command it issues draw from that one shrinking
allowance, and a call that returns past its window contributes no page and is not
checkpointed. The numeric `page_timeout` value itself stays benchmark-gated ([B3]).

## Reviewed page-error categories and bounded retry

The spec leaves error categories open, so this slice fixes the smallest decidable
set from the contract's closed vocabulary (§2.7):

| Category | Page code | Retried inside one attempt | Job `reason` when it is the cause |
|---|---|---|---|
| transient | `page_engine_error` | yes, at most `PAGE_RETRIES` (provisional 1, D21) | `engine_unavailable` |
| permanent | `malformed_document` | no | `input_unreadable` |

A permanent page failure is never retried, so an unreadable or undecodable page
cannot enter a retry loop. The app — never the engine — decides whether a code is
retryable, so an engine cannot widen the bound.

**Shortcut:** between-attempt backoff is not implemented. A backoff value is a
benchmark-gated number [B3] and this slice adds no new limit; the fake engine is
instantaneous, and real spacing would belong with the per-page timeout, which is
now implemented (#11) but still carries no approved value ([B3]). Cross-attempt
recovery is a new attempt (a crash or a platform retry), not a sleep.

## Finalization (complete / partial / failed)

- Every page is accounted for: the result holds exactly one entry per source page,
  in ascending order, and a failed page keeps its number, `status: "failed"`, an
  `error_code`, and no text (§2.6/D23).
- `succeeded` only when every page succeeded; otherwise `partially_succeeded` when
  at least one page did, else `failed` (§3). `completeness` is `partial` for any
  state that is not a fully successful job, and `accuracy` stays the constant
  `not_verified`.
- `reason` is `null` only on success, else the dominant page failure's reason
  from §2.5's closed set.

## Deadline behavior (D42)

- An attempt that reaches the job **at or after** `deadline_at` writes nothing and
  exits non-zero; reconciliation finalizes it (§5.5, §6.2). This changed the
  earlier behavior where the worker finalized such a job itself.
- An attempt that **crosses** the deadline stops page work: the in-flight page
  call is abandoned (it contributes no page and is not checkpointed), and pages
  not reached are reported `failed` with `error_code: deadline_exceeded`. The
  finalize is `partially_succeeded` when a page survived, else `failed`, always
  `reason: deadline_exceeded` — never `succeeded`.

## Verification (offline only)

- `tests/test_ocr_recovery.py` — the public lifecycle (S1): a partial result with
  the failed page's number and code; bounded transient retry and its recovery; a
  permanent failure never retried; a crash resumed from checkpoints with only the
  missing page recomputed; a stale/duplicate worker unable to replace a succeeded
  page; an at-deadline attempt that writes nothing; and a deadline crossing that
  finalizes partial with every page accounted for.
- `tests/test_ocr_store.py` — the checkpoint storage contract (S2): create-once
  first-writer-wins, a foreign-pin checkpoint treated as absent, and an invalid
  checkpoint replaced only at its read generation.
- `scripts/ocr_tracer_demo.py` — the same paths end to end in fake mode, and it
  fails if a cloud client module is imported.

These are fake-client tests. They are **not** evidence of real GCS concurrency,
the real engine, or its per-page timeout ([B1], [B3]).

## Not implemented in this slice

Named so they are not mistaken for done: real container decoding and embedded-PDF
text (#7); numeric limits such as `page_timeout` and any backoff ([B3]); cleanup
and expiry (#9, now in `docs/ocr-v1-lifecycle.md`: reconciliation credits durable
page checkpoints on its deadline path, the read gate denies at `expires_at` even
mid-cleanup, full-success inputs are deleted at once, and the physical deletion
pass is `app/ocr_cleanup.py`). Reconciliation still does not resume page work.
Engine and worker pin enforcement (#11) is now merged and stated in
`docs/ocr-v1-pinning.md`, and the deadline-scoped page timeout and the
lease-heartbeat renewal that also carry the #11 label are implemented
(`app/ocr_tracer.run_job`, `app/ocr_store.renew_lease`, and the adapter's
per-command bound); their single-object CAS read-then-act limit is stated in
`docs/ocr-v1-dispatch.md`.

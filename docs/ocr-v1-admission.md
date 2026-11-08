# OCR v1 — admission and input bounds (issue #10)

Ticket: GitHub issue #10 "OCR v1: Bound uploads and shared processing capacity"
(`TU-thanakrit/dev-service-google-doc-ai`). Delivery slice 10 of 12.
Worktree `/home/danny/service_google_doc_ai/.worktrees/ocr-v1-admission`, branch
`task/ocr-v1-admission`, based on `integration/ocr-service-v1`.

Executable form: `app/ocr_admission.py` (input boundary, request bound,
`AdmissionLimits`, startup invariants) and the capacity set in `app/ocr_store.py`
(`acquire_admission` / `release_admission`). Wired in `app/ocr_tracer.py`, the
deployed compositions `app/ocr_api.py` / `app/ocr_worker.py` / `app/ocr_reconcile.py`,
and configured by `app/ocr_config.py`.

Nothing here is cloud evidence. Every check in this slice runs against fakes; the
contract's blocks B1–B4 stay open and no paid or live OCR was called.

---

## 1. What was built

| Bound | Where | Behavior |
|---|---|---|
| Total request size | `RequestSizeLimitMiddleware` (pure ASGI, mounted on every route) | Declared `Content-Length` over the ceiling → `413 file_too_large` with no body read. No declared length (chunked / HTTP-2) → the body is read here in bounded chunks and replayed downstream, so no parser can spool more than the ceiling. |
| File size | `validate_document` | Bytes over the ceiling → `413 file_too_large`. |
| Container | `validate_document` by magic bytes, not the declaration | Not PDF/JPEG/PNG/TIFF, or a declared type that disagrees with the bytes → `415 unsupported_media_type`. |
| Page / frame count | `validate_document`, then the worker | Over the limit → `422 too_many_pages` at submission; a container whose page tree hid the count from the byte scan is refused by the worker before any page is recognized. |
| Encryption | `validate_document` | A PDF carrying `/Encrypt` → `422 encrypted_document`. |
| Structural damage | `validate_document` | Truncated or headerless PDF/PNG/JPEG/TIFF → `422 malformed_document`. |
| Image dimensions | `validate_document` | A raster whose single frame exceeds the pixel budget → `413 file_too_large` (see §4). |
| Global capacity | `DurableOcrStore` over `capacity/inflight.json` | One serialized admission set keyed by `job_id`; over-ceiling, unreadable/unparseable, or CAS-exhausted → `429 capacity_exceeded` with `Retry-After: 30`. |

Order of the checks follows contract §2.3, so an input that breaks two rules
reports the earlier one's code every time.

The endpoint's own `file.read()` is deliberately **not** the admission design: the
multipart parser (python-multipart, via a `SpooledTemporaryFile` that rolls over to
disk after 1 MiB) spools the body before any route code runs. Bounding the request
first is what makes the size ceiling real.

## 2. The numbers, and what each rests on

All of these are DEV-safe defaults, configurable through `OCR_*` environment
variables (`app/ocr_config.py`) or by injecting `AdmissionLimits`. **None of them is
a capacity or throughput claim**, and none is benchmark-approved [B3].

| Setting | Default | Basis |
|---|---|---|
| `OCR_MAX_UPLOAD_BYTES` | 20 MiB | The reviewed provisional value (contract §10/D38). Bounded above by the 32 MiB Cloud Run HTTP/1 request ceiling, which cannot be raised; the startup invariant refuses a configuration outside `1 … 32 MiB`. |
| `OCR_MAX_PAGES` | **5** | The longest document actually measured on this project — the 5-page `mixed_multipage` cell of the labelled benchmark corpus (`docs/ocr-v1-benchmark-report.md` §2). **Provisional**: the corpus is synthetic, nothing above 5 pages has been measured, and the representativeness gap (including a ≥20-page real document) is part of issue #2 B1. This default is not a claim that longer documents work or that 6 pages would fail to OCR. |
| `OCR_MAX_IMAGE_PIXELS` | 40,000,000 | **No measurement exists.** A4 at 600 dpi is ≈35 MP, so the ceiling admits a plausible high-resolution scan and refuses a decompression bomb. Benchmark-gated [B3] like the other numbers. |
| `OCR_CAPACITY_CEILING` | **1** | The contract's [B3]-gated placeholder (§6.4/D40); the design proves no other measured bound, so the smallest safe value is used. One job in flight globally is a DEV-safe admission bound, **not** a throughput target: raising it requires the benchmark that the ticket gates, plus `ceiling × max_entry_bytes ≤ capacity_object_budget`. |
| per-attempt timeout | `OCR_TASK_TIMEOUT_S` (1800 s) | The reviewed provisional value (§10). It is how long an admission entry may outlive its job before the next writer prunes it. |

Startup invariants asserted in `assert_limits` (and by the tests): the byte
ceiling is inside the platform request ceiling (D38), each limit is positive, and
`ceiling × max_entry_bytes ≤ capacity_object_budget` (256 KiB, contract §10).

## 3. Capacity: one serialized admission set

- One object, `capacity/inflight.json`, holds every admission keyed by `job_id`
  (`{admitted_at, deadline_at, expires_at}`, `expires_at = deadline_at +
  per_attempt_timeout`), as reviewed in §6.4/D40.
- **Acquire** happens on the paths that create or resume a job's work, before
  `job.json` exists: the `202` path and the crash-window materialization path. A
  job already in the set is an idempotent success, so a same-key retry takes no
  second slot. Exhaustion, a corrupt object, or five lost CAS races → `429`, no
  record, no dispatch.
- **Release** is performed only by the `final.json` winner (§5.4 step 3), which
  covers the worker, reconciliation, and the claim creator's own `503`
  compensation. A release that cannot be written is not a job failure: the entry
  self-expires, and reconciliation removes a final job's entry by CAS (§6.2).
- **No enumerating recount exists.** Pruning is a pure function of time applied
  inside the same CAS write as the acquire or release, so no path can read the set
  low; the only drift the protocol can produce is over-reservation. A corrupt object
  is never rebuilt automatically (such a rebuild could read low), so repairing it is
  an operator action.
- **Leak bound.** An entry lives at most until `deadline_at + per_attempt_timeout`.
  A submitter that dies after being admitted, or a winner that dies before
  releasing, therefore holds a slot for at most that long — and cleanup's own guard
  is `max(expiry, deadline_at + per_attempt_timeout)`, the same instant, so a purged
  job never leaves a live reservation behind.

## 4. Named limits and deviations

- **The closed error vocabulary has no dimension code.** An over-limit raster is
  reported as `413 file_too_large` (an over-limit input) instead of inventing a code
  that §2.7/§11.1 would reject. A contract revision could add one; this slice does
  not.
- **`422 too_many_pages` on the worker path publishes one failed page, not one per
  source page.** §2.6's per-source-page accounting cannot be produced for a document
  the application refuses to decode, and enumerating its pages is exactly what must
  not happen. The job is `failed`/`input_unreadable`, the page carries `too_many_pages`
  (or `malformed_document` when the container could not be decoded at all), and the
  published `pages_total` is `null` when the count is unknown.
- **The byte-level PDF page count misses object-stream page trees.** A PDF whose page
  objects live inside a compressed object stream counts 0 here (the `%%EOF`/trailer
  check keeps it from being called malformed). This is a deliberate accepted limit,
  not an oversight: the worker's engine count is the second gate and refuses the
  document before recognizing anything. Upgrading it means a real cross-reference
  parser, which this slice does not attempt (the ticket explicitly excludes one).
- **The pixel bound is checked on the first/declared frame metadata**, not by decoding
  the raster; a decoder-specific bomb (nested PNG layers, TIFF strips) is bounded by
  the byte ceiling and the page limit, and by the engine's own memory behaviour.
- **A rejected input creates no job, but the idempotency claim from §4 step 1 may
  remain** when admission fails. That is the reviewed order (claim → admission →
  materialize): a retry within `materialization_grace` materializes at the recorded
  `job_id` once capacity frees, which is why the failed acquire is not compensated.

## 5. Tests (no live OCR, no cloud call)

`tests/test_ocr_admission.py` — seam S1 (the public lifecycle) plus the ASGI edge:

- the request bound: an oversized `Content-Length`, a streamed request with no
  `Content-Length` cut off mid-stream (the endpoint never runs), and a within-ceiling
  chunked request replayed whole;
- structural rejection of malformed, truncated, and encrypted documents, unsupported
  and mismatched declared types, an over-limit page count, and an over-budget raster —
  each asserting that no job object exists and that the engine was never called;
- the worker's own refusal of an over-limit and of an undecodable container;
- capacity: a `429` with `Retry-After: 30` at the ceiling, two simultaneous
  submissions admitting exactly one, release on success and on failure, pruning of an
  abandoned entry, a corrupt object failing closed, and reconciliation removing a
  leaked entry for a final job.

`tests/ocr_documents.py` builds the smallest structurally valid PDF/PNG/JPEG/TIFF
documents for these tests (no third-party or sensitive content). The pre-existing
single-page fixtures in `tests/test_ocr_tracer.py` were header-only stubs and are now
whole documents, because the boundary refuses a header with no structure.

`tests/test_ocr_tracer.py` constructs its apps with an explicit permissive
`capacity_ceiling`, because that module drives several independent open jobs through
one store; the ceiling itself is exercised in `tests/test_ocr_admission.py`. The
lifecycle, recovery, and worker modules keep the configured default of one job and
pass unchanged, which is itself the evidence that a single open job still behaves as
before.

Fake-mode evidence only: `InMemoryObjectStore` emulates generations and `412`s.
Real-GCS admission behaviour, the deployed `--max-instances` interaction, and any
measured ceiling remain unverified (B1–B4).

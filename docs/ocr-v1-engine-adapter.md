# OCR v1 — real CPU engine adapter: Thai/English text and embedded PDF text (issue #7)

Status: **implemented and covered by offline tests plus one env-gated local run
against the real binaries**; **no cloud behaviour and no production accuracy are
claimed**. This slice implements the recognition half of
`docs/ocr-v1-contracts.md` §2.6 (`text_source`, warnings, per-page accounting),
§5.5/D41 (per-page recognition), §11.2 S3, and D11/D23/D24. It replaces the
deterministic placeholder engine in the worker entrypoint; it adds no
classification, Gemini call, or structured extraction.

## What changed

| Module | Role |
|---|---|
| `app/ocr_engine.py` | `CommandOcrEngine` — the real per-page recognition adapter, plus `SubprocessRunner` and the injected `CommandRunner` seam |
| `app/ocr_worker.py` | the deployed entrypoint now builds the real engine; the placeholder `FakePagesEngine` is deleted |
| `app/ocr_config.py` | five `OCR_ENGINE_*` settings (`lang`, `raster_dpi`, `tesseract`, `tessdata_prefix`) |
| `app/ocr_tracer.py` | one bounded guard: a container the engine cannot decode finalizes the job `failed`/`input_unreadable` instead of crashing the worker |
| `Dockerfile` | poppler-utils + Tesseract (`eng`, `tha`) + ImageMagick, and an unprivileged runtime user |
| `tests/test_ocr_engine.py` | 25 fake-runner tests at the S3 seam and the worker seam |
| `tests/test_ocr_engine_integration.py` | 7 env-gated tests against the real installed binaries and the tracked fixtures |

The synchronous expense app (`app.main`) is untouched.

## Engine (DEV, provisional)

Tesseract 4.1.1 with the distro `tha`+`eng` models — the measured DEV selection
of `docs/ocr-v1-benchmark-report.md` §6. **Final / production selection remains
blocked** (issue #2 B1/B2: the corpus is synthetic, no acceptance threshold is
approved), and per-job engine pinning is ticket #11. The adapter's engine
identity is the operator-only `engine_ref` of §8 and is never returned to a
caller (D19).

## Per-page recognition

`page_count` decodes the container without recognizing anything; `recognize_page`
recognizes exactly one page, so a restart re-issues only the pages without a valid
checkpoint (D41).

| Input | Page count | Per page |
|---|---|---|
| PDF | `pdfinfo` `Pages:` | embedded text layer if usable, else `pdftoppm` raster → `tesseract` |
| JPEG, PNG | 1 (D24) | `tesseract` on the file |
| TIFF | `identify -format '%n'` (D24) | `convert 'file[N-1]'` frame → `tesseract` |

Published fields follow §2.6/D11/D23 exactly: `text_source` is `embedded` only
when the input is a PDF and its extracted layer is usable, otherwise `ocr`; text
is trimmed by the application; a `succeeded` page with no text carries `text: ""`
plus the `no_text` warning. **No confidence value is invented**, so
`possible_low_quality` is deliberately never emitted — no calibrated quality
signal exists without the benchmark ([B3]).

### Usability rule (D11), and one caveat

A PDF text layer is usable when, after NFC normalisation, it has at least one
non-whitespace character and contains no `U+FFFD`. Anything else — including a
`pdftotext` failure or timeout — falls back to OCR, because the text layer is an
optimisation and never the only path to a result.

**Caveat, stated so it is not mistaken for a fix:** NFC does **not** recompose
SARA AM (`U+0E33` is a composition exclusion), so a decomposed Thai layer
(`จำกัด` → `จํากัด`) is published as it is. The benchmark's SARA AM fold is a
*metrics* normalisation applied to reference and hypothesis alike; it is not
applied to published text, because doing so would alter the document's own bytes.

## Bounded, injectable commands

Every external program and every ceiling is constructor-injected behind
`CommandRunner`, so tests drive the adapter without a binary. `SubprocessRunner`
runs an **argv list** (never a shell) with `capture_output`, a strict timeout, and
captured bytes; a timeout raises `CommandTimeout` and a spawn failure
`CommandError`, which the adapter maps into the closed page vocabulary.

| Step | Ceiling |
|---|---|
| container decode (`pdfinfo`, `identify`) | 30 s |
| page render (`pdftoppm`, `convert`) | 120 s |
| recognition (`tesseract`) | 120 s |

The deadline-scoped page timeout of §5.5/D42 (`min(page_timeout, deadline_at - now)`)
is **ticket #11's**; these ceilings only stop one hung binary from stalling an attempt.

## Failure mapping

| Outcome | Page code (§2.7) | Retried | Job `reason` |
|---|---|---|---|
| decode/render non-zero, unparsable count, no/extra page image, bad frame index, unsupported type, `page_number` out of range | `malformed_document` | no | `input_unreadable` |
| command timeout, spawn failure, `tesseract` non-zero | `page_engine_error` | yes, `PAGE_RETRIES` | `engine_unavailable` |

A container the engine cannot decode at all (`page_count` fails) has **no page to
number**, so the job is finalized `failed`/`input_unreadable` with `page_count: 0`
and `pages: []` — never a crash and never an invented page (§5.6, §9 scenario 14).

## Container

`python:3.12-slim` installs `poppler-utils`, `tesseract-ocr`,
`tesseract-ocr-eng`, `tesseract-ocr-tha`, and `imagemagick`, then runs as an
unprivileged user (`uid 10001`); the worker writes only to its per-call
`TemporaryDirectory` under `/tmp`.

One deliberate deviation from the four named binaries: **the poppler suite and
Tesseract cannot select a single frame of a multi-frame TIFF** — Tesseract
concatenates every frame into one output — so ImageMagick `identify`/`convert`
supply the frame count and the frame split. Everything else uses the named
binaries.

The image is **not built here** (no build/registry permissions, and no paid
cloud work is authorised): the package set, the Tesseract 5-vs-4.1.1 difference
(Rocky ships 4.1.1, Debian ships 5.x) and the container licence expression
([B7]) stay verified-on-build items.

## Verification

Offline (fakes only, no binary executed, no cloud client constructed):

- `tests/test_ocr_engine.py` — 25 tests: `pdfinfo`/`identify` parsing, single-page
  images with no command, embedded-text reuse, NFC normalisation, whitespace and
  `U+FFFD` fallback, both page-failure categories, TIFF frame indexing, strict
  timeouts and child env, allowlisted-only logging, plus the S1/S3 worker test that
  every source page is accounted for in order with its `text_source`, and the
  undecodable-container finalization.
- `uv run pytest` → **201 passed, 8 skipped**; `uv run ruff check .` → clean;
  `uv lock --check` → current.

Env-gated local run against the real binaries and tracked fixtures:

    OCR_ENGINE_INTEGRATION=1 \
    OCR_ENGINE_TESSERACT=<root>/usr/bin/tesseract \
    OCR_ENGINE_TESSDATA_PREFIX=<root>/usr/share/tesseract/tessdata \
    LD_LIBRARY_PATH=<root>/usr/lib64 \
    uv run pytest tests/test_ocr_engine_integration.py
    # 7 passed -- every tracked PDF yields one ordered page per source page, the
    # tracked JPEG is OCR'd, and a 2-frame TIFF reports two frames.

Measured on this host with Tesseract 4.1.1 (`tha`+`eng`), 300 dpi rasterization,
content-free output (counts and durations only, no recognized text):

| Input | Pages | Text source | Chars/page | Notes |
|---|---|---|---|---|
| `cash_bill.pdf` | 1 | embedded | 249 | 0.03 s |
| `receipt.pdf` | 1 | embedded | 246 | 0.03 s |
| `receipt_substitute.pdf` | 1 | embedded | 1247 | 0.03 s |
| `tax_invoice.pdf` | 1 | embedded | 1579 | 0.04 s |
| `tollway.pdf` | 1 | ocr | 759 | no usable layer; 3.5 s |
| `tollway-source.jpeg` | 1 | ocr | 1501 | 1.5 s |
| 2-frame TIFF built from `receipt.pdf` (150 dpi) | 2 | ocr | 2485 each | 5.7 s / 6.4 s |

Zero execution failures on every path. These are **execution** measurements, not
accuracy: no ground-truth transcript exists for these fixtures, so no CER/WER is
computed and none is implied.

### Multi-frame TIFF cost (measured, named not built)

The TIFF path re-decodes the whole container for each frame (`convert 'file[N]'`),
so a 500-frame TIFF costs 500 full decodes; a per-run frame cache would fix that
if a real multi-frame TIFF input ever appears. Named, not built: no such fixture
is tracked.

## Explicit non-claims

- No production engine was selected: the DEV selection is provisional and
  reversible (issue #2 B1/B2 stay open).
- No cloud call, upload, deployment, paid run, or IAM change was made, and no
  bucket was touched.
- Fake-runner success is not cloud evidence, and the env-gated integration test
  is **not** cloud or accuracy evidence.
- Issue #7's cloud-workload criterion and the B1/B2/B4 evidence blockers stay open.

## Not in this slice

Per-job engine pin enforcement and the template comparison (#11), the
deadline-scoped page timeout and lease-heartbeat renewal (#11), submission-time
structural/encrypted/size validation (#10), and the numeric limits gated by the
benchmark ([B3]).

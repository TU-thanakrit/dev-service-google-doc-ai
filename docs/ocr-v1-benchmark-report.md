# OCR v1 — CPU engine benchmark: measured results and DEV selection

Ticket: GitHub issue #2 "OCR v1: Benchmark and select the initial CPU OCR engine"
(`TU-thanakrit/dev-service-google-doc-ai`). Delivery slice 2 of 12.

Worktree: `/home/danny/service_google_doc_ai/.worktrees/ocr-v1-engine`
Branch: `task/ocr-v1-engine`, based on `integration/ocr-service-v1`.

Status:

- **Measured, on this host, on a labelled corpus.** Real CER/WER, durations and
  memory for Tesseract 4.1.1 (two model sets), RapidOCR 3.10.0 with the PaddleOCR
  PP-OCRv5 Thai model, and the embedded-PDF-text path.
- **Initial DEV engine selected: Tesseract 4.1.1 with `tha` + `eng`** — on
  measured aggregate-Thai accuracy and operational fit (see §6).
- **Final / production selection remains BLOCKED** (issue #2 B1–B2): the corpus
  here is synthetic, not representative real documents, and no acceptance
  threshold is user-approved. §7 lists the exact next evidence.

Companion machine-checkable artefacts (sanitised, no document text):

- `docs/ocr-v1-benchmark-corpus.json` — corpus cells, licences, sources, hashes.
- `docs/ocr-v1-benchmark-results.json` — the four engine reports plus a comparison table.

Nothing was uploaded, no cloud resource was touched, no paid call was made, and
no corpus binary or OCR text is committed.

---

## 1. What "ground truth" means here, and what it does not

The repository's public fixtures (`examples/input/*`) carry **no transcript** —
`examples/manifest.json` records a classification label and
`verification/summary.md` reports schema-key correctness, not text accuracy. They
support a smoke harness (one accounted-for result per page, no crash, duration)
but **cannot rank engines**. That remains true and unchanged.

To get independent ground truth without an unbounded or licence-encumbered
download, this slice builds a **synthetic corpus whose text is authored in this
repository**. Ground truth is the authored string; the rendered image is a
lossy-but-controlled view of it. This is a legitimate way to measure CER/WER, and
it is the only way to get **controlled clean/poor pairs of the same text**.

**It is not representative of real Thai business documents.** Real scans carry
noise, skew, stamping, handwriting, and layout that this corpus does not model.
Every absolute accuracy number below is therefore a *relative* signal for
choosing between engines, **not** a production accuracy claim. Issue #2's
"approved representative corpus" requirement (B1) stays open.

## 2. Corpus (synthetic, labelled, licence-clean)

Definition and ground truth: `scripts/ocr_bench_corpus.py` (authored text only).
Rendering: `pango-view` (Pango + HarfBuzz, so Thai combining marks shape
correctly) with the system `Waree` font; degradation and format conversion with
ImageMagick; multi-page assembly with `pdfunite`. Licence: **CC0-1.0** (text
authored for this repository); no third-party content is copied.

11 documents / 15 pages covering the cells issue #2 asks for:

| Cell | Doc | Lang | Condition | Format | DPI | Pages |
|---|---|---|---|---|---|---|
| printed Thai, clean | `th_clean` | th | clean | png | 300 | 1 |
| printed English, clean | `en_clean` | en | clean | png | 300 | 1 |
| mixed Thai/English, clean | `mixed_clean` | mixed | clean | png | 300 | 1 |
| poor scan (blur+noise+skew) | `th_degraded` | th | degraded | png | 300 | 1 |
| poor scan | `en_degraded` | en | degraded | png | 300 | 1 |
| poor scan | `mixed_degraded` | mixed | degraded | png | 300 | 1 |
| poor scan (low resolution) | `th_lowdpi` | th | low_dpi | png | 100 | 1 |
| common format JPEG | `en_jpeg` | en | clean | jpeg | 300 | 1 |
| common format TIFF | `en_tiff` | en | clean | tiff | 300 | 1 |
| native PDF (real text layer) | `th_bornpdf` | th | born_digital | pdf | 300 | 1 |
| longer document | `mixed_multipage` | th/en/mixed | clean | pdf | 300 | 5 |

The Thai text deliberately includes the SARA AM words (จำกัด, กำกับ, สำนักงาน,
ที่ตั้ง), the rare-consonant set (ฎ ฏ ฐ ฑ ฒ ณ ฬ), Thai digits (๙๙, ๒), a 13-digit
tax id, and a Latin "OCR" token that both engines struggle with.

Per-artefact SHA-256 values are in `docs/ocr-v1-benchmark-corpus.json`. Examples:
`th_clean.png a58dc0ce…e16510`, `mixed_multipage.pdf e59a0e2b…ffcdba`,
`th_bornpdf.pdf 724dcfea…af74a8`.

**Limitation:** the longest document is 5 pages. Issue #2 asks for "longer
documents"; a ≥20-page real document is not covered here and is part of the
representative-corpus gap.

## 3. Frozen metrics and normalisation

Frozen before any run (`scripts/ocr_bench.py`, ids `ocr-bench-norm-v1` /
`ocr-bench-metrics-v1`), applied identically to reference and hypothesis:

- **Primary: CER** (character error rate = Levenshtein edits / reference length),
  on NFC text with zero-width characters removed and horizontal whitespace
  collapsed.
- **`cer_nospace`**: same, with all whitespace removed. Reported because Thai
  does not space words and engines insert spaces freely; the spaced CER would
  otherwise punish a correct recognition.
- **SARA AM fold**: U+0E33 is folded to U+0E4D U+0E32. Tesseract emits the
  decomposed form (`จำกัด` → `จํากัด`); without this fold every SARA AM would
  count as two errors. Verified by test.
- **Secondary: WER** (English word tokens) and **page exact-match rate**.
- **Operational (kept separate from accuracy):** wall-clock seconds/page, peak
  RSS, engine/model version and hash.

### Provisional DEV gates (proposed, NOT approved)

Defined before ranking so the numbers are not post-hoc. These are a **development
sanity gate**, not production acceptance criteria; issue #2 B2 stays open.

| Gate | Threshold | Source |
|---|---|---|
| clean English micro-CER | ≤ 0.05 | proposal |
| clean Thai micro-CER | ≤ 0.15 | proposal |
| degraded micro-CER | ≤ 0.35 | proposal |
| clean page-exact rate | ≥ 0.80 | proposal |
| seconds per page | ≤ 15 | proposal |
| peak RSS | ≤ 1024 MB | proposal |

## 4. Environment

Intel Core i5-6300U @ 2.40 GHz, 4 threads, 15.4 GiB RAM, Rocky Linux 9.8,
kernel 5.14.0-687.64.1.el9_8.x86_64, Python 3.12.12. A laptop, **not** a Cloud
Run worker shape — see the caveat on worker sizing in §7.

- Tesseract 4.1.1 + Leptonica 1.80.0, unpacked from Rocky `appstream` RPMs into
  scratch (no root, no system install). Two model sets: distro langpacks and
  official `tessdata_best` (Apache-2.0).
- RapidOCR 3.10.0 + ONNX Runtime 1.31.1 in an isolated scratch venv
  (`rapidocr`, `onnxruntime`); recognition model PaddleOCR `th_PP-OCRv5_rec_mobile`
  (Apache-2.0), downloaded from ModelScope (7.55 MB). Install: 20 s, 342 MB —
  well inside the 15 min / 2 GB bound.
- Rasterisation: `pdftoppm` (poppler) 300 dpi.

Model hashes (recorded in `docs/ocr-v1-benchmark-results.json`):

    tessdata_best  tha.traineddata  ee8adab6dc69eb8df3d3c8307ae8295471b7bd7d86a06d9267aa8f479b064eac
    tessdata_best  eng.traineddata  8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba
    distro langpack tha.traineddata 294227cc2d1292b0acb28d61d4115c88252b96d466ca90b417cf4cf0c67bf07c
    distro langpack eng.traineddata 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2
    RapidOCR th_PP-OCRv5_rec_mobile.onnx  de541dd83161c241ff426f7ecfd602a0ba77d686cf3ab9a6c255ea82fd08006e

Licence check: Tesseract engine + `tessdata_best` are Apache-2.0; RapidOCR code
and the PaddleOCR weights are Apache-2.0. All four engines/adapters run fully
on-premises and transmit no document content. The distro Tesseract package's
combined licence expression (plan B7) is still open for the container path.

## 5. Measured results

15 pages, one engine process per page, Thai+English, 300 dpi, single laptop run.
Full per-page detail is in `docs/ocr-v1-benchmark-results.json`.

| Engine | micro CER | page-exact | s/page | peak RSS | clean en | clean th | clean mixed | degraded | low-dpi | born-digital |
|---|---|---|---|---|---|---|---|---|---|---|
| Tesseract 4.1.1 · distro langpack | **0.076** | 0.40 | **0.84** | **53 MB** | 0.000 | **0.082** | 0.212 | 0.108 | **0.042** | 0.051 |
| Tesseract 4.1.1 · tessdata_best | 0.078 | 0.40 | 2.79 | 77 MB | 0.000 | 0.080 | 0.223 | 0.113 | 0.037 | 0.051 |
| RapidOCR 3.10.0 · PP-OCRv5 th | 0.124 | 0.40 | 2.23 | 807 MB | 0.000 | 0.261 | **0.009** | 0.127 | 0.296 | 0.301 |
| embedded PDF text layer (6 PDF pages) | 0.040 | 0.33 | **0.011** | 48 MB | 0.000 | 0.069 | 0.018 | n/a | n/a | 0.069 |

Zero execution failures in every run.

What the numbers say:

- **English is solved** by both engines: CER 0.000 on clean, degraded, JPEG and
  TIFF English pages. English does not discriminate between candidates.
- **Thai-only accuracy favours Tesseract**, roughly 3× better than RapidOCR
  (0.08 vs 0.26 clean) and ~7× better at 100 dpi (0.04 vs 0.30).
- **Clean mixed Thai/Latin favours RapidOCR** (0.009 vs 0.21) — it reads the
  Latin runs on a Thai line correctly where Tesseract's `tha+eng` confuses them.
  Real invoices are mixed, so this is a genuine, not cosmetic, difference.
- **Memory is the largest operational gap**: RapidOCR peaks at **807 MB** versus
  Tesseract's **53 MB**, for a 4-model ONNX stack. RapidOCR is ~2.7× slower per
  page and needs a 342 MB Python environment; Tesseract is a native binary with
  no Python ML runtime.
- **No OCR engine reaches the 0.80 clean page-exact gate** (both 0.40 overall /
  0.50 on clean pages): every Thai page has at least one character error. Under
  the provisional gates Tesseract passes 5/6, RapidOCR 4/6.
- **The embedded PDF text layer beats OCR on born-digital PDFs** in aggregate
  (0.040 vs 0.076) at ~100× the speed, but *not* on the pure-Thai page (0.069 vs
  0.051) because of the known Thai PDF text-layer defect (`บริษัท` → `บริษท ั`).
  This supports reusing embedded text only behind the spec's usability check, not
  unconditionally.

Reproduce (all commands run from the worktree; scratch is outside the repo):

    SCR=/home/danny/.hermes/cache/scratch/ocr-bench
    uv run python -m scripts.ocr_bench_corpus --outdir "$SCR/corpus" \
        --manifest "$SCR/corpus.json" --include-text
    uv run python -m scripts.ocr_bench --corpus-dir "$SCR/corpus" \
        --corpus-manifest "$SCR/corpus.json" --engine tesseract \
        --tesseract <tesseract> --tessdata-dir <tessdata> --lang tha+eng \
        --out "$SCR/report-tess.json" --raw-dir "$SCR/raw/tess" --workdir "$SCR/work"
    <venv-rapidocr>/bin/python -m scripts.ocr_bench_rapidocr \
        --corpus-dir "$SCR/corpus" --corpus-manifest "$SCR/corpus.json" \
        --text-dir "$SCR/raw/rapidocr" --workdir "$SCR/work-rapid" --meta "$SCR/rapidocr-meta.json"
    uv run python -m scripts.ocr_bench --corpus-dir "$SCR/corpus" \
        --corpus-manifest "$SCR/corpus.json" --engine dir --text-dir "$SCR/raw/rapidocr" \
        --engine-name rapidocr --adapter-meta "$SCR/rapidocr-meta.json" \
        --out "$SCR/report-rapidocr.json" --workdir "$SCR/work-rapid-h"
    uv run python -m scripts.ocr_bench --corpus-dir "$SCR/corpus" \
        --corpus-manifest "$SCR/corpus.json" --engine embedded \
        --out "$SCR/report-embedded.json" --workdir "$SCR/work-emb"

## 6. Selection

**Initial DEV engine: Tesseract 4.1.1 with `tha` + `eng` (distro langpack or
`tessdata_best`).** Chosen on measured evidence and operational fit:

- Best measured Thai-only CER (0.08) and by far the best low-resolution result
  (0.04), with English already perfect.
- Lowest cost and risk: 53–77 MB RSS, 0.84–2.79 s/page, no Python ML runtime, no
  model download at run time, license-clean, installable offline from distro
  RPMs.
- It is the natural first engine behind the application-owned recognition seam
  (spec: "Begin with CPU-only evaluation"; engine replacement must not change the
  caller contract).

`tessdata_best` is ~3× slower than the distro langpack for no measured accuracy
gain on this corpus, so the **distro `tha`+`eng` langpack is the DEV default**.

**RapidOCR is not rejected — it is deferred.** Its PP-OCRv5 Thai model is clearly
better on clean mixed Thai/Latin lines, which is what real invoices look like,
but it is worse on Thai-only and low-resolution input and 15× heavier in memory.
The synthetic corpus cannot settle that trade-off.

**Final / production selection is BLOCKED** until the representative corpus (B1)
and approved thresholds (B2) exist, because a ranking over synthetic, cleanly
rendered text is not a safe basis for a production commitment.

## 7. Blockers and exact next evidence

- **B1 approved representative corpus — BLOCKED.** Need real Thai/English
  business documents (scanned invoices/forms, mixed Thai/English, poor scans,
  ≥20 pages) with native-Thai transcription on selected pages, source identity,
  hash, licence/permission, and scan class recorded. This is the single most
  important missing input.
- **B2 metrics / normalisation / thresholds — BLOCKED.** The harness's
  `ocr-bench-norm-v1` and the provisional DEV gates in §3 are the concrete
  proposal awaiting approval.
- **Re-run both engines on the representative corpus** once B1/B2 exist; decide
  the final engine from that run, not from this one.
- **Worker CPU/memory (B6) — still needs worker-shape evidence.** This run is a
  4-thread laptop. The relevant input now exists: RapidOCR needs ~807 MB while
  Tesseract needs ~53 MB, so a Thai model on ONNX Runtime constrains Cloud Run
  worker memory far more than Tesseract does.
- **B7 Tesseract install-licence expression** — confirm for the container base
  image (distro package vs source build) before shipping.
- **Tesseract 5 vs 4.1.1** — Rocky 9.8 / EPEL 9 ship 4.1.1 only; a Tesseract 5
  comparison needs a pinned source build or a different base image.

## 8. Explicit non-claims

- No engine was selected for production; the DEV selection in §6 is provisional
  and reversible.
- No accuracy number here is a production accuracy claim: the corpus is
  synthetic and does not model real scan degradation or layout.
- No document was uploaded, no paid call was made, no cloud resource was touched,
  and no issue was modified.
- No corpus binary, ground-truth-in-report, or OCR text is committed; raw output
  lives only under `/home/danny/.hermes/cache/scratch/ocr-bench/`.

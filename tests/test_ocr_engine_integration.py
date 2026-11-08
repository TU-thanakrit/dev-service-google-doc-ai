"""Env-gated local integration test for the real engine adapter (issue #7).

Runs only when `OCR_ENGINE_INTEGRATION=1` is exported, and only ever uses the
**tracked public example fixtures** under `examples/` with the **actually
installed system binaries** (`pdfinfo`, `pdftotext`, `pdftoppm`, `tesseract`,
plus ImageMagick `identify`/`convert` for multi-frame TIFF). No cloud client is
constructed, nothing is uploaded, and no recognised text is written to disk or
committed: scratch files go to a per-call temp directory that is removed when the
call returns.

The DEV engine here is the measured, provisional Tesseract `tha`+`eng` selection
(`docs/ocr-v1-benchmark-report.md` §6). Production engine selection stays blocked
on a representative corpus and approved thresholds; this test is an execution
smoke test, **not** an accuracy measurement.

    OCR_ENGINE_INTEGRATION=1 uv run pytest tests/test_ocr_engine_integration.py

On a host where Tesseract is not on `PATH`, point the adapter at the unpacked
binary/tessdata (and put its shared libraries on `LD_LIBRARY_PATH`):

    OCR_ENGINE_INTEGRATION=1 \
    OCR_ENGINE_TESSERACT=/path/to/root/usr/bin/tesseract \
    OCR_ENGINE_TESSDATA_PREFIX=/path/to/root/usr/share/tesseract/tessdata \
    LD_LIBRARY_PATH=/path/to/root/usr/lib64 \
    uv run pytest tests/test_ocr_engine_integration.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.ocr_engine import EMBEDDED, OCR, CommandOcrEngine, SubprocessRunner

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ENABLED = os.environ.get("OCR_ENGINE_INTEGRATION") == "1"
TESSERACT = os.environ.get("OCR_ENGINE_TESSERACT", "tesseract")

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="set OCR_ENGINE_INTEGRATION=1 to run against the real installed binaries",
)


def _engine() -> CommandOcrEngine:
    if shutil.which(TESSERACT) is None and not Path(TESSERACT).is_file():
        pytest.skip(f"tesseract not available at {TESSERACT!r}")
    return CommandOcrEngine(
        SubprocessRunner(),
        tesseract=TESSERACT,
        tessdata_prefix=os.environ.get("OCR_ENGINE_TESSDATA_PREFIX") or None,
    )


def _pages(engine, content: bytes, mime: str):
    total = engine.page_count(content, mime)
    return [engine.recognize_page(content, mime, number) for number in range(1, total + 1)]


@pytest.mark.parametrize(
    "name",
    ["cash_bill.pdf", "receipt.pdf", "receipt_substitute.pdf", "tax_invoice.pdf", "tollway.pdf"],
)
def test_every_tracked_pdf_fixture_yields_one_ordered_page_per_source_page(name):
    engine = _engine()
    content = (EXAMPLES / "input" / name).read_bytes()

    pages = _pages(engine, content, "application/pdf")

    assert len(pages) >= 1
    assert all(page.text_source in {EMBEDDED, OCR} for page in pages)
    # A real printed Thai/English document must produce some text on this path;
    # the text itself is never stored or printed.
    assert any(page.text.strip() for page in pages), f"{name}: unrecognized document"


def test_the_tracked_jpeg_fixture_is_recognized_directly():
    engine = _engine()
    content = (EXAMPLES / "source" / "tollway-source.jpeg").read_bytes()

    assert engine.page_count(content, "image/jpeg") == 1
    page = engine.recognize_page(content, "image/jpeg", 1)

    assert page.text_source == OCR
    assert page.text.strip()


def test_a_tiff_derived_from_a_tracked_fixture_reports_one_page_per_frame():
    engine = _engine()
    with tempfile.TemporaryDirectory(prefix="ocr-engine-int-") as scratch:
        work = Path(scratch)
        pdf = EXAMPLES / "input" / "receipt.pdf"
        subprocess.run(
            ["pdftoppm", "-r", "150", "-png", str(pdf), str(work / "page")],
            check=True,
            capture_output=True,
            timeout=120,
        )
        pngs = sorted(work.glob("page*.png"))
        assert pngs
        # A two-frame TIFF built from tracked input, outside the repository.
        subprocess.run(
            ["convert", str(pngs[0]), str(pngs[0]), str(work / "frames.tiff")],
            check=True,
            capture_output=True,
            timeout=120,
        )
        content = (work / "frames.tiff").read_bytes()

    assert engine.page_count(content, "image/tiff") == 2
    frames = _pages(engine, content, "image/tiff")
    assert len(frames) == 2
    assert all(page.text_source == OCR for page in frames)
    assert all(page.text.strip() for page in frames)

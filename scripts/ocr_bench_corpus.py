"""Synthetic, clearly-labelled Thai/English OCR benchmark corpus (GitHub issue #2).

Ground truth is text **authored in this repository** (no third-party content), so
licences are unambiguous and the corpus is reproducible from source without
committing a single binary. Rendered artefacts are written OUTSIDE the repository
(scratch); only this definition and the hashes it produces are version controlled.

This corpus is SYNTHETIC. It is not a representative real-document corpus and
cannot substitute for the approved representative corpus required by issue #2
(blocker B1). It exists to measure character/word error rates honestly on
controlled clean/degraded pairs, which the public example fixtures cannot do
(they carry no ground-truth transcript).

Rendering uses pango-view (Pango + HarfBuzz) so Thai combining marks are shaped
correctly, and ImageMagick for degradation/format conversion. Both are optional
at test time: ``plan()`` is pure and is what the unit tests exercise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CORPUS_ID = "ocr-bench-synthetic-v1"
CORPUS_LICENSE = "CC0-1.0 (text authored for this repository)"
CORPUS_SOURCE = "synthetic-authored"
FONT = "Waree"  # /usr/share/fonts/thai-scalable/Waree.otf (TLWG); system-provided, not shipped
FONT_SIZE_PT = 14

# --- ground truth text (authored here; the only source of truth for scoring) ---

TH_CLEAN = """\
แบบทดสอบการอ่านตัวอักษรไทย
บริษัท ไทยยูเนี่ยน จำกัด (มหาชน)
ที่ตั้ง ๙๙ หมู่ ๒ ถนนสุขุมวิท กรุงเทพมหานคร
เลขประจำตัวผู้เสียภาษี 0107536000315
กำกับ จำกัด สำนักงาน ที่ตั้ง ฎ ฏ ฐ ฑ ฒ ณ ฬ
ยอดรวมทั้งสิ้น 1,234.50 บาท"""

EN_CLEAN = """\
OCR Accuracy Test Document
Invoice Number: INV-2026-00123
Customer: Thai Union Public Company Limited
Total Amount: 1,234.50 THB
Payment Terms: Net 30 days
Thank you for your business."""

MIXED_CLEAN = """\
ใบกำกับภาษี / Tax Invoice
ผู้ขาย: บริษัท ไทยยูเนี่ยน จำกัด (มหาชน)
Invoice No. INV-2026-00123
วันที่: 08/11/2026
รายการ สินค้าแช่แข็ง Frozen Seafood
จำนวน 12 กล่อง Unit Price 102.88 บาท
ยอดรวมทั้งสิ้น Total 1,234.50 THB"""


@dataclass(frozen=True)
class Page:
    text: str
    lang: str  # th | en | mixed


@dataclass(frozen=True)
class Doc:
    id: str
    lang: str
    condition: str  # clean | degraded | low_dpi | born_digital
    fmt: str  # png | jpeg | tiff | pdf
    dpi: int
    degradations: tuple[str, ...]
    pages: tuple[Page, ...]


def _p(text: str, lang: str) -> Page:
    return Page(text=text, lang=lang)


DOCS: tuple[Doc, ...] = (
    Doc("th_clean", "th", "clean", "png", 300, (), (_p(TH_CLEAN, "th"),)),
    Doc("en_clean", "en", "clean", "png", 300, (), (_p(EN_CLEAN, "en"),)),
    Doc("mixed_clean", "mixed", "clean", "png", 300, (), (_p(MIXED_CLEAN, "mixed"),)),
    Doc(
        "th_degraded",
        "th",
        "degraded",
        "png",
        300,
        ("blur", "noise", "skew"),
        (_p(TH_CLEAN, "th"),),
    ),
    Doc(
        "en_degraded",
        "en",
        "degraded",
        "png",
        300,
        ("blur", "noise", "skew"),
        (_p(EN_CLEAN, "en"),),
    ),
    Doc(
        "mixed_degraded",
        "mixed",
        "degraded",
        "png",
        300,
        ("blur", "noise", "skew"),
        (_p(MIXED_CLEAN, "mixed"),),
    ),
    Doc("th_lowdpi", "th", "low_dpi", "png", 100, ("low_dpi",), (_p(TH_CLEAN, "th"),)),
    Doc("en_jpeg", "en", "clean", "jpeg", 300, ("jpeg_q75",), (_p(EN_CLEAN, "en"),)),
    Doc("en_tiff", "en", "clean", "tiff", 300, (), (_p(EN_CLEAN, "en"),)),
    Doc(
        "th_bornpdf",
        "th",
        "born_digital",
        "pdf",
        300,
        (),
        (_p(TH_CLEAN, "th"),),
    ),
    Doc(
        "mixed_multipage",
        "mixed",
        "clean",
        "pdf",
        300,
        (),
        (
            _p(TH_CLEAN, "th"),
            _p(EN_CLEAN, "en"),
            _p(MIXED_CLEAN, "mixed"),
            _p(TH_CLEAN, "th"),
            _p(EN_CLEAN, "en"),
        ),
    ),
)

DOCS_BY_ID = {doc.id: doc for doc in DOCS}


@dataclass(frozen=True)
class Step:
    doc_id: str
    argv: tuple[str, ...]
    output: Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _render_argv(text_path: Path, out: Path, dpi: int) -> tuple[str, ...]:
    return (
        "pango-view",
        "--no-display",
        "--font",
        f"{FONT} {FONT_SIZE_PT}",
        "--dpi",
        str(dpi),
        "--margin",
        str(max(12, dpi // 10)),
        "--background",
        "white",
        "--output",
        str(out),
        str(text_path),
    )


def _degrade_argv(src: Path, out: Path) -> tuple[str, ...]:
    # visual-only degradation: the intended text stays the ground truth
    return (
        "convert",
        str(src),
        "-blur",
        "0x0.8",
        "-attenuate",
        "0.5",
        "+noise",
        "Gaussian",
        "-rotate",
        "1.5",
        "-background",
        "white",
        "-flatten",
        str(out),
    )


def _convert_argv(src: Path, out: Path, fmt: str) -> tuple[str, ...]:
    argv = ["convert", str(src)]
    if fmt == "jpeg":
        argv += ["-quality", "75"]
    argv.append(str(out))
    return tuple(argv)


def plan(outdir: Path) -> list[Step]:
    """Pure planning: the exact subprocess argv list for building the corpus.

    No file is touched and no tool is invoked, so this is safe to unit-test.
    """
    outdir = Path(outdir)
    work = outdir / "work"
    steps: list[Step] = []
    for doc in DOCS:
        # born-digital / multi-page PDFs are rendered as vector PDFs (real text layer)
        if doc.fmt == "pdf":
            page_pdfs = []
            for page_no, _ in enumerate(doc.pages, start=1):
                text_path = work / f"{doc.id}.p{page_no}.txt"
                page_pdf = work / f"{doc.id}.p{page_no}.pdf"
                steps.append(
                    Step(doc.id, _render_argv(text_path, page_pdf, doc.dpi), text_path)
                )
                page_pdfs.append(page_pdf)
            target = outdir / f"{doc.id}.pdf"
            if len(page_pdfs) == 1:
                steps.append(Step(doc.id, ("cp", str(page_pdfs[0]), str(target)), target))
            else:
                steps.append(
                    Step(
                        doc.id,
                        ("pdfunite", *[str(p) for p in page_pdfs], str(target)),
                        target,
                    )
                )
            continue

        # raster documents: one PNG per page, then degrade/convert as configured
        page_pngs = []
        for page_no, _ in enumerate(doc.pages, start=1):
            text_path = work / f"{doc.id}.p{page_no}.txt"
            page_png = work / f"{doc.id}.p{page_no}.png"
            steps.append(Step(doc.id, _render_argv(text_path, page_png, doc.dpi), text_path))
            page_pngs.append(page_png)

        for page_no, page_png in enumerate(page_pngs, start=1):
            source = page_png
            if doc.condition == "degraded":
                degraded = work / f"{doc.id}.p{page_no}.deg.png"
                steps.append(Step(doc.id, _degrade_argv(page_png, degraded), degraded))
                source = degraded
            target = outdir / f"{doc.id}.{doc.fmt}"
            if doc.fmt != "png":
                steps.append(Step(doc.id, _convert_argv(source, target, doc.fmt), target))
            else:
                steps.append(Step(doc.id, ("cp", str(source), str(target)), target))
    return steps


def text_paths(outdir: Path) -> dict[str, Path]:
    """Map ``<doc_id>.p<n>`` to the ground-truth text file the builder writes."""
    work = Path(outdir) / "work"
    return {
        f"{doc.id}.p{page_no}": work / f"{doc.id}.p{page_no}.txt"
        for doc in DOCS
        for page_no in range(1, len(doc.pages) + 1)
    }


def build(outdir: Path, *, only: set[str] | None = None) -> dict:
    outdir = Path(outdir).resolve()
    (outdir / "work").mkdir(parents=True, exist_ok=True)
    # write ground-truth text files first; render steps read them
    for key, path in text_paths(outdir).items():
        doc_id, page_no = key.rsplit(".p", 1)
        doc = DOCS_BY_ID[doc_id]
        path.write_text(doc.pages[int(page_no) - 1].text + "\n", encoding="utf-8")

    for step in plan(outdir):
        if only and step.doc_id not in only:
            continue
        subprocess.run(list(step.argv), check=True, capture_output=True, text=True, timeout=300)

    return manifest(outdir)


def manifest(outdir: Path, *, include_text: bool = False) -> dict:
    outdir = Path(outdir).resolve()
    docs = []
    for doc in DOCS:
        artifact = outdir / f"{doc.id}.{doc.fmt}"
        pages = []
        for page_no, page in enumerate(doc.pages, start=1):
            entry = {"page": page_no, "lang": page.lang, "text_sha256": sha256_text(page.text)}
            if include_text:
                entry["text"] = page.text
            pages.append(entry)
        docs.append(
            {
                "id": doc.id,
                "lang": doc.lang,
                "condition": doc.condition,
                "format": doc.fmt,
                "dpi": doc.dpi,
                "degradations": list(doc.degradations),
                "pages": pages,
                "artifact": artifact.name,
                "artifact_sha256": sha256_file(artifact) if artifact.exists() else None,
            }
        )
    return {
        "corpus": CORPUS_ID,
        "synthetic": True,
        "license": CORPUS_LICENSE,
        "source": CORPUS_SOURCE,
        "font": FONT,
        "docs": docs,
        "totals": {
            "documents": len(docs),
            "pages": sum(len(d["pages"]) for d in docs),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outdir", type=Path, required=True, help="scratch dir; never inside the repo"
    )
    parser.add_argument("--manifest", type=Path, default=None, help="write manifest JSON here")
    parser.add_argument("--include-text", action="store_true", help="include ground-truth text")
    parser.add_argument("--only", nargs="*", default=None, help="build only these doc ids")
    parser.add_argument("--plan", action="store_true", help="print the plan and exit")
    args = parser.parse_args(argv)

    if args.plan:
        for step in plan(args.outdir):
            print(f"{step.doc_id}\t{' '.join(step.argv)}")
        return 0

    built = build(args.outdir, only=set(args.only) if args.only else None)
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = manifest(args.outdir, include_text=args.include_text)
        payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        args.manifest.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(built["totals"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Repeatable CPU-only OCR accuracy benchmark harness (GitHub issue #2).

Scores OCR output against **independent** ground truth produced by
``scripts/ocr_bench_corpus.py`` (a clearly-labelled synthetic Thai/English
corpus). Unlike the public example fixtures, that corpus carries a known
transcript, so CER/WER are measurable rather than assumed.

The report contains metrics, counts, hashes and durations only -- never
recognised or reference text. Raw text, when requested, goes to ``--raw-dir``
which must stay outside the repository.

Thai normalisation notes (frozen, applied identically to reference and
hypothesis):
  * NFC, zero-width characters removed, horizontal whitespace collapsed.
  * SARA AM (U+0E33) is folded to NIPPON (U+0E4D) + SARA AA (U+0E32). Tesseract
    emits the decomposed form, so without this fold every SARA AM counts as two
    errors. See ``fold_sara_am``.
  * ``cer`` keeps single spaces; ``cer_nospace`` removes all whitespace, because
    Thai does not space words and engines insert spaces freely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

HARNESS_ID = "ocr-bench-v1"
NORMALISATION_ID = "ocr-bench-norm-v1"
METRICS_ID = "ocr-bench-metrics-v1"
ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
SARA_AM = "\u0e33"
SARA_AM_FOLD = "\u0e4d\u0e32"
HORIZONTAL = re.compile(r"[^\S\n]+")
WHITESPACE = re.compile(r"\s+")

# Provisional DEV gates, proposed for approval BEFORE any ranking. They are a
# development sanity gate for a synthetic corpus, NOT the production acceptance
# criteria (issue #2 B2 stays open until a representative corpus exists).
DEV_GATES = {
    "clean_en_micro_cer_max": 0.05,
    "clean_th_micro_cer_max": 0.15,
    "degraded_micro_cer_max": 0.35,
    "clean_page_exact_rate_min": 0.80,
    "seconds_per_page_max": 15.0,
    "peak_rss_mb_max": 1024.0,
}


def fold_sara_am(text: str) -> str:
    return text.replace(SARA_AM, SARA_AM_FOLD)


def normalize_lines(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(ch for ch in text if ch not in ZERO_WIDTH)
    text = fold_sara_am(text)
    lines = [HORIZONTAL.sub(" ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def normalize_flat(text: str) -> str:
    return WHITESPACE.sub(" ", normalize_lines(text).replace("\n", " ")).strip()


def normalize_nospace(text: str) -> str:
    return WHITESPACE.sub("", normalize_lines(text))


def levenshtein(a: Sequence[object], b: Sequence[object]) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class PageRef:
    doc_id: str
    page: int
    lang: str  # th | en | mixed
    condition: str  # clean | degraded | low_dpi | born_digital
    text: str


@dataclass(frozen=True)
class Item:
    ref: PageRef
    image: Path | None = None
    pdf: Path | None = None
    pdf_page: int | None = None
    key: str = ""


def score_page(ref_text: str, hyp_text: str) -> dict:
    ref_flat = normalize_flat(ref_text)
    hyp_flat = normalize_flat(hyp_text)
    ref_ns = normalize_nospace(ref_text)
    hyp_ns = normalize_nospace(hyp_text)
    ref_words = ref_flat.split()
    hyp_words = hyp_flat.split()
    edits = levenshtein(ref_flat, hyp_flat)
    return {
        "cer": edits / max(len(ref_flat), 1),
        "cer_nospace": levenshtein(ref_ns, hyp_ns) / max(len(ref_ns), 1),
        "wer": levenshtein(ref_words, hyp_words) / max(len(ref_words), 1),
        "page_exact": ref_flat == hyp_flat,
        "edits": edits,
        "ref_chars": len(ref_flat),
        "hyp_chars": len(hyp_flat),
        "ref_words": len(ref_words),
        "hyp_sha256": sha256_text(hyp_flat),
    }


def _empty_score() -> dict:
    return {
        "cer": None,
        "cer_nospace": None,
        "wer": None,
        "page_exact": False,
        "edits": 0,
        "ref_chars": 0,
        "hyp_chars": 0,
        "ref_words": 0,
        "hyp_sha256": None,
    }


def _aggregate(pages: list[dict]) -> dict:
    scored = [p for p in pages if not p["error"]]
    ref_chars = sum(p["ref_chars"] for p in scored)
    edits = sum(p["edits"] for p in scored)
    return {
        "pages": len(pages),
        "scored_pages": len(scored),
        "failures": len(pages) - len(scored),
        "micro_cer": (edits / ref_chars) if ref_chars else None,
        "page_exact_rate": (
            sum(1 for p in scored if p["page_exact"]) / len(scored) if scored else None
        ),
        "seconds_total": round(sum(p["seconds"] for p in pages), 3),
    }


def evaluate(
    items: list[Item],
    recognize: Callable[[Item], str],
    *,
    clock=time.perf_counter,
    timings: dict[str, float] | None = None,
) -> dict:
    """Run ``recognize(item) -> str`` over items; return a content-free report.

    ``timings`` maps ``item.key`` to an externally measured duration; it is used
    for the ``dir`` engine, where recognition happened in another process.
    """
    page_timings: dict[str, float] = timings or {}
    per_doc: dict[str, dict] = {}
    for item in items:
        doc = per_doc.setdefault(item.ref.doc_id, {"id": item.ref.doc_id, "pages": []})
        started = clock()
        try:
            hyp = recognize(item)
            elapsed = round(page_timings.get(item.key, clock() - started), 3)
            score = score_page(item.ref.text, hyp)
            score["error"] = None
            score["seconds"] = elapsed
        except Exception as exc:  # an accounted-for page outcome, never dropped
            elapsed = round(page_timings.get(item.key, clock() - started), 3)
            score = _empty_score()
            score["error"] = f"{type(exc).__name__}: {exc}"
            score["seconds"] = elapsed
        score["page"] = item.ref.page
        score["lang"] = item.ref.lang
        score["condition"] = item.ref.condition
        doc["pages"].append(score)

    for doc in per_doc.values():
        langs = sorted({p["lang"] for p in doc["pages"]})
        doc["langs"] = langs
        doc["condition"] = doc["pages"][0]["condition"] if doc["pages"] else None

    all_pages = [p for d in per_doc.values() for p in d["pages"]]
    by_lang = {
        lang: _aggregate([p for p in all_pages if p["lang"] == lang])
        for lang in sorted({p["lang"] for p in all_pages})
    }
    by_condition = {
        cond: _aggregate([p for p in all_pages if p["condition"] == cond])
        for cond in sorted({p["condition"] for p in all_pages})
    }
    by_condition_lang = {
        f"{cond}|{lang}": _aggregate(
            [p for p in all_pages if p["condition"] == cond and p["lang"] == lang]
        )
        for cond in sorted({p["condition"] for p in all_pages})
        for lang in sorted({p["lang"] for p in all_pages})
        if any(p["condition"] == cond and p["lang"] == lang for p in all_pages)
    }
    overall = _aggregate(all_pages)
    overall["seconds_per_page"] = (
        round(overall["seconds_total"] / overall["pages"], 3) if overall["pages"] else None
    )
    overall["peak_rss_kb"] = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )

    return {
        "harness": HARNESS_ID,
        "normalisation": NORMALISATION_ID,
        "metrics": METRICS_ID,
        "documents": sorted(per_doc.values(), key=lambda d: d["id"]),
        "by_lang": by_lang,
        "by_condition": by_condition,
        "by_condition_lang": by_condition_lang,
        "aggregate": overall,
    }


def evaluate_gates(report: dict) -> list[dict]:
    """Apply the provisional DEV gates; each check is machine-checkable."""
    agg = report["aggregate"]
    by_cl = report["by_condition_lang"]
    clean_en = by_cl.get("clean|en", {}).get("micro_cer")
    clean_th = by_cl.get("clean|th", {}).get("micro_cer")
    degraded = report["by_condition"].get("degraded", {}).get("micro_cer")
    clean_exact = report["by_condition"].get("clean", {}).get("page_exact_rate")
    return [
        {
            "gate": "clean_en_micro_cer_max",
            "value": clean_en,
            "limit": DEV_GATES["clean_en_micro_cer_max"],
            "passed": clean_en is not None and clean_en <= DEV_GATES["clean_en_micro_cer_max"],
        },
        {
            "gate": "clean_th_micro_cer_max",
            "value": clean_th,
            "limit": DEV_GATES["clean_th_micro_cer_max"],
            "passed": clean_th is not None and clean_th <= DEV_GATES["clean_th_micro_cer_max"],
        },
        {
            "gate": "degraded_micro_cer_max",
            "value": degraded,
            "limit": DEV_GATES["degraded_micro_cer_max"],
            "passed": degraded is not None and degraded <= DEV_GATES["degraded_micro_cer_max"],
        },
        {
            "gate": "clean_page_exact_rate_min",
            "value": clean_exact,
            "limit": DEV_GATES["clean_page_exact_rate_min"],
            "passed": clean_exact is not None
            and clean_exact >= DEV_GATES["clean_page_exact_rate_min"],
        },
        {
            "gate": "seconds_per_page_max",
            "value": agg["seconds_per_page"],
            "limit": DEV_GATES["seconds_per_page_max"],
            "passed": agg["seconds_per_page"] is not None
            and agg["seconds_per_page"] <= DEV_GATES["seconds_per_page_max"],
        },
        {
            "gate": "peak_rss_mb_max",
            "value": round(agg["peak_rss_kb"] / 1024, 1),
            "limit": DEV_GATES["peak_rss_mb_max"],
            "passed": agg["peak_rss_kb"] / 1024 <= DEV_GATES["peak_rss_mb_max"],
        },
    ]


# --- runners ---------------------------------------------------------------


def tesseract_runner(binary: str, tessdata: Path | None, lang: str, psm: int = 3):
    env = dict(os.environ)
    if tessdata:
        env["TESSDATA_PREFIX"] = str(tessdata)

    def recognize(item: Item) -> str:
        proc = subprocess.run(
            [binary, str(item.image), "stdout", "-l", lang, "--psm", str(psm)],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract exited {proc.returncode}")
        return proc.stdout

    return recognize


def embedded_pdf_runner(pdftotext: str = "pdftotext"):
    """Score the PDF's own text layer (spec user story 15 / plan section 5.6)."""

    def recognize(item: Item) -> str:
        if item.pdf is None or item.pdf_page is None:
            raise ValueError("embedded runner needs a pdf and page number")
        proc = subprocess.run(
            [
                pdftotext,
                "-layout",
                "-f",
                str(item.pdf_page),
                "-l",
                str(item.pdf_page),
                str(item.pdf),
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pdftotext exited {proc.returncode}")
        return proc.stdout

    return recognize


def dir_runner(text_dir: Path):
    """Read pre-computed ``<key>.txt`` files (used by external engines)."""

    def recognize(item: Item) -> str:
        path = Path(text_dir) / f"{item.key}.txt"
        if not path.exists():
            raise FileNotFoundError(f"missing pre-computed text: {path.name}")
        return path.read_text(encoding="utf-8")

    return recognize


def with_raw_logging(recognize: Callable[[Item], str], raw_dir: Path) -> Callable[[Item], str]:
    """Wrap a runner so every recognised page is also written to ``raw_dir``."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    def wrapped(item: Item) -> str:
        text = recognize(item)
        (raw_dir / f"{item.key}.txt").write_text(text, encoding="utf-8")
        return text

    return wrapped


def engine_version(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        text = out.stdout or out.stderr
        return text.strip().splitlines()[0] if text.strip() else ""
    except OSError:
        return ""


def hardware_info() -> dict:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal"):
                mem_kb = int(line.split()[1])
                break
    except OSError:
        pass
    return {
        "cpu": cpu,
        "logical_cores": os.cpu_count(),
        "mem_total_mb": round(mem_kb / 1024, 1),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }


def rasterise_pdf(pdf: Path, dest: Path, dpi: int) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(dest / "page")],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return sorted(dest.glob("page-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def items_from_corpus(corpus_dir: Path, corpus: dict, workdir: Path, dpi: int) -> list[Item]:
    corpus_dir = Path(corpus_dir)
    items: list[Item] = []
    for doc in corpus["docs"]:
        artifact = corpus_dir / doc["artifact"]
        if doc["format"] == "pdf":
            images = rasterise_pdf(artifact, workdir / doc["id"], dpi)
        else:
            images = [artifact] * len(doc["pages"])
        for page_no, page in enumerate(doc["pages"], start=1):
            ref = PageRef(doc["id"], page_no, page["lang"], doc["condition"], page["text"])
            items.append(
                Item(
                    ref=ref,
                    image=images[page_no - 1] if page_no - 1 < len(images) else None,
                    pdf=artifact if doc["format"] == "pdf" else None,
                    pdf_page=page_no if doc["format"] == "pdf" else None,
                    key=f"{doc['id']}.p{page_no}",
                )
            )
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="sanitised JSON report")
    parser.add_argument("--engine", choices=["tesseract", "embedded", "dir"], required=True)
    parser.add_argument("--engine-name", default=None)
    parser.add_argument("--engine-version", default=None)
    parser.add_argument(
        "--adapter-meta",
        type=Path,
        default=None,
        help="JSON file from an external adapter (models, version, per-page timings)",
    )
    parser.add_argument("--lang", default="tha+eng")
    parser.add_argument("--psm", type=int, default=3)
    parser.add_argument("--tesseract", default=os.environ.get("TESSERACT_BIN", "tesseract"))
    parser.add_argument("--tessdata-dir", type=Path, default=None)
    parser.add_argument("--text-dir", type=Path, default=None)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args(argv)

    corpus_dir = args.corpus_dir.resolve()
    workdir = args.workdir or Path(os.environ.get("TMPDIR", "/tmp")) / "ocr-bench-work"
    if workdir.resolve().is_relative_to(Path.cwd().resolve()):
        print("refusing to write rasterised pages inside the repository", file=sys.stderr)
        return 2

    corpus = json.loads(args.corpus_manifest.read_text(encoding="utf-8"))
    if not corpus.get("synthetic") or not corpus["docs"][0]["pages"][0].get("text"):
        print("corpus manifest must be synthetic and include ground-truth text", file=sys.stderr)
        return 2

    items = items_from_corpus(corpus_dir, corpus, workdir, args.dpi)
    adapter_meta = (
        json.loads(args.adapter_meta.read_text(encoding="utf-8")) if args.adapter_meta else None
    )
    models: dict[str, str] = {}

    if args.engine == "tesseract":
        recognize = tesseract_runner(args.tesseract, args.tessdata_dir, args.lang, args.psm)
        name = args.engine_name or "tesseract"
        version = args.engine_version or engine_version([args.tesseract, "--version"])
        if args.tessdata_dir:
            for code in args.lang.split("+"):
                model = Path(args.tessdata_dir) / f"{code}.traineddata"
                if model.exists():
                    models[model.name] = sha256_file(model)
    elif args.engine == "embedded":
        # the embedded-text track only applies to PDF inputs
        items = [item for item in items if item.pdf is not None]
        recognize = embedded_pdf_runner()
        name = args.engine_name or "embedded-pdf-text"
        version = args.engine_version or engine_version(["pdftotext", "-v"])
    else:
        if not args.text_dir:
            print("--text-dir is required for the dir engine", file=sys.stderr)
            return 2
        recognize = dir_runner(args.text_dir)
        name = args.engine_name or "external"
        version = args.engine_version or ""

    if adapter_meta:
        models.update(adapter_meta.get("models", {}))
        version = version or adapter_meta.get("version", "")

    if args.raw_dir:
        recognize = with_raw_logging(recognize, Path(args.raw_dir))

    report = evaluate(items, recognize, timings=(adapter_meta or {}).get("timings"))
    report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    report["engine"] = {"name": name, "version": version, "lang": args.lang, "models": models}
    if adapter_meta:
        report["engine"]["adapter"] = {
            key: adapter_meta[key]
            for key in ("rec_config", "init_seconds", "total_seconds", "peak_rss_kb")
            if key in adapter_meta
        }
    report["corpus"] = {
        "id": corpus["corpus"],
        "synthetic": True,
        "license": corpus["license"],
        "documents": corpus["totals"]["documents"],
        "pages": corpus["totals"]["pages"],
    }
    report["rasterisation"] = {"dpi": args.dpi, "pdf_renderer": "pdftoppm (poppler)"}
    report["hardware"] = hardware_info()
    report["dev_gates"] = {
        "status": "provisional (proposed, not user-approved; issue #2 B2 open)",
        "thresholds": DEV_GATES,
        "checks": evaluate_gates(report),
    }
    report["limitations"] = [
        "synthetic authored corpus, NOT representative real documents (issue #2 B1 open)",
        "peak_rss_kb is a running maximum over this process and its children",
        "no document was uploaded and no paid call was made",
    ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"aggregate": report["aggregate"], "by_condition": report["by_condition"]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""RapidOCR adapter for the OCR benchmark harness (GitHub issue #2).

Runs RapidOCR (ONNX Runtime CPU) with the PaddleOCR PP-OCRv5 Thai recognition
model over the synthetic corpus and writes one ``<key>.txt`` per page, which the
harness consumes via ``--engine dir``. This keeps the heavy ONNX dependency out
of the repository's runtime/dev dependencies and out of ``uv run pytest``.

Requires an environment with ``rapidocr`` and ``onnxruntime`` installed, e.g.:

    uv venv --python 3.12 <scratch>/venv-rapidocr
    uv pip install --python <scratch>/venv-rapidocr/bin/python rapidocr onnxruntime
    <scratch>/venv-rapidocr/bin/python -m scripts.ocr_bench_rapidocr \
        --corpus-dir <scratch>/corpus --corpus-manifest <scratch>/corpus.json \
        --text-dir <scratch>/raw/rapidocr --workdir <scratch>/work-rapid \
        --meta <scratch>/rapidocr-meta.json

``--meta`` records the engine version, model hashes, per-page durations and peak
RSS so the harness can report them alongside the accuracy figures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path

from scripts.ocr_bench import error_category, items_from_corpus


def build_engine():
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    return RapidOCR(
        params={
            "Rec.lang_type": LangRec.TH,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
        }
    )


def model_hashes() -> dict[str, str]:
    import rapidocr

    models_dir = Path(rapidocr.__file__).resolve().parent / "models"
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(models_dir.glob("*.onnx"))
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--text-dir", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--meta", type=Path, default=None)
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args(argv)

    try:
        engine_version = package_version("rapidocr")
    except Exception:  # pragma: no cover - metadata should exist in an installed env
        engine_version = ""

    started = time.perf_counter()
    engine = build_engine()
    init_seconds = round(time.perf_counter() - started, 3)

    text_dir = Path(args.text_dir)
    text_dir.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(args.corpus_manifest.read_text(encoding="utf-8"))
    items = items_from_corpus(args.corpus_dir, corpus, args.workdir, args.dpi)

    timings: dict[str, float] = {}
    failures = 0
    for item in items:
        page_started = time.perf_counter()
        try:
            result = engine(str(item.image))
            lines = list(result.txts or ())
        except Exception as exc:  # recorded, not hidden; the harness shows the failure
            lines = []
            failures += 1
            # Fixed category only: an exception's text can carry a corpus path.
            print(f"FAILED {item.key}: {error_category(exc)}", file=sys.stderr)
        timings[item.key] = round(time.perf_counter() - page_started, 3)
        (text_dir / f"{item.key}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.meta:
        payload = {
            "engine": "rapidocr",
            "version": engine_version,
            "rec_config": "LangRec.TH / PP-OCRv5 / mobile",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "init_seconds": init_seconds,
            "total_seconds": round(sum(timings.values()), 3),
            "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "models": model_hashes(),
            "timings": timings,
        }
        args.meta.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps({"pages": len(items), "failures": failures}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

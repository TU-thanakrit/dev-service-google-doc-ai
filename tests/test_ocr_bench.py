"""Tests for the OCR benchmark harness and synthetic corpus (issue #2)."""

from __future__ import annotations

import json

import pytest

from scripts import ocr_bench as ob
from scripts import ocr_bench_corpus as corpus


def _item(doc_id="d1", page=1, lang="th", condition="clean", text="สวัสดี", key=None):
    ref = ob.PageRef(doc_id, page, lang, condition, text)
    return ob.Item(ref=ref, key=key or f"{doc_id}.p{page}")


# --- normalisation ---------------------------------------------------------


def test_normalize_collapses_whitespace_and_zero_width():
    raw = "  a\u200b  b\t\tc  \n\n  d  "
    assert ob.normalize_lines(raw) == "a b c\n\nd"
    assert ob.normalize_flat(raw) == "a b c d"


def test_sara_am_fold_makes_composed_and_decomposed_equal():
    # Tesseract emits decomposed SARA AM; NFC keeps it composed
    composed = "จำกัด"
    decomposed = "จํากัด"
    assert composed != decomposed
    assert ob.normalize_flat(composed) == ob.normalize_flat(decomposed)
    assert ob.score_page(composed, decomposed)["cer"] == 0.0


# --- metrics ---------------------------------------------------------------


def test_levenshtein_distance():
    assert ob.levenshtein("kitten", "sitting") == 3
    assert ob.levenshtein("abc", "abc") == 0
    assert ob.levenshtein("", "abc") == 3
    assert ob.levenshtein(["a", "b"], ["a", "c"]) == 1


def test_score_page_identical_is_perfect():
    score = ob.score_page("hello world", "hello world")
    assert score["cer"] == 0.0
    assert score["wer"] == 0.0
    assert score["page_exact"] is True


def test_score_page_known_cer_and_wer():
    score = ob.score_page("hello world", "hello wurld")
    assert score["cer"] == 1 / len("hello world")
    assert score["wer"] == 1 / 2
    assert score["page_exact"] is False


def test_cer_nospace_ignores_inserted_spaces():
    # Thai does not space words; an engine that inserts spaces must not be punished
    spaced = ob.score_page("สวัสดีครับ", "สวัสดี ครับ")
    assert spaced["cer"] > 0
    assert spaced["cer_nospace"] == 0.0


def test_score_page_empty_reference_is_safe():
    score = ob.score_page("", "")
    assert score["cer"] == 0.0
    assert score["page_exact"] is True


# --- evaluation ------------------------------------------------------------


def test_evaluate_records_failure_without_dropping_the_page():
    items = [_item(page=1, key="d1.p1"), _item(page=2, key="d1.p2")]

    def recognize(item):
        if item.ref.page == 2:
            raise RuntimeError("boom")
        return "สวัสดี"

    report = ob.evaluate(items, recognize)
    pages = report["documents"][0]["pages"]
    assert [p["page"] for p in pages] == [1, 2]
    assert pages[1]["error"] == "engine_failure"
    assert report["aggregate"]["pages"] == 2
    assert report["aggregate"]["scored_pages"] == 1
    assert report["aggregate"]["failures"] == 1


def test_a_recorded_failure_never_carries_the_exception_text_or_a_path():
    items = [_item(key="d1.p1")]

    def recognize(item):
        raise FileNotFoundError("/home/danny/private/corpus/d1.png is missing")

    report = ob.evaluate(items, recognize)

    page = report["documents"][0]["pages"][0]
    assert page["error"] == "input_missing"
    blob = json.dumps(report, ensure_ascii=False)
    assert "private/corpus" not in blob
    assert "is missing" not in blob


@pytest.mark.parametrize(
    "error, category",
    [
        (TimeoutError("slow"), "engine_timeout"),
        (FileNotFoundError("path"), "input_missing"),
        (ValueError("bad"), "invalid_input"),
        (RuntimeError("exit 1"), "engine_failure"),
        (KeyError("other"), "internal_error"),
    ],
)
def test_benchmark_error_categories_are_a_fixed_vocabulary(error, category):
    assert ob.error_category(error) == category


def test_evaluate_report_contains_no_recognised_or_reference_text():
    secret = "ลับสุดยอด CONFIDENTIAL-TOKEN"
    items = [_item(text=secret, key="d1.p1")]
    report = ob.evaluate(items, lambda item: secret)
    blob = json.dumps(report, ensure_ascii=False)
    assert secret not in blob
    assert "CONFIDENTIAL-TOKEN" not in blob
    assert not any("\u0e00" <= ch <= "\u0e7f" for ch in blob)
    # a hash is still recorded so runs are comparable
    assert report["documents"][0]["pages"][0]["hyp_sha256"]


def test_evaluate_aggregates_by_language_and_condition():
    items = [
        _item("a", 1, "th", "clean", "หนึ่ง", "a.p1"),
        _item("a", 2, "en", "clean", "two", "a.p2"),
        _item("b", 1, "th", "degraded", "สาม", "b.p1"),
    ]
    report = ob.evaluate(items, lambda item: item.ref.text)
    assert set(report["by_lang"]) == {"th", "en"}
    assert set(report["by_condition"]) == {"clean", "degraded"}
    assert set(report["by_condition_lang"]) == {"clean|th", "clean|en", "degraded|th"}
    assert report["aggregate"]["micro_cer"] == 0.0


def test_evaluate_uses_external_timings_for_dir_engine():
    items = [_item(key="a.p1")]
    report = ob.evaluate(items, lambda item: "สวัสดี", timings={"a.p1": 1.25})
    assert report["documents"][0]["pages"][0]["seconds"] == 1.25


def test_evaluate_gates_mark_pass_and_fail():
    items = [_item("a", 1, "en", "clean", "hello world", "a.p1")]
    report = ob.evaluate(items, lambda item: item.ref.text)
    checks = {c["gate"]: c["passed"] for c in ob.evaluate_gates(report)}
    assert checks["clean_en_micro_cer_max"] is True
    # the single page is exact, so the page-exact-rate floor is met
    assert checks["clean_page_exact_rate_min"] is True


# --- corpus ----------------------------------------------------------------


def test_corpus_ids_unique():
    ids = [doc.id for doc in corpus.DOCS]
    assert len(ids) == len(set(ids))


def test_corpus_covers_the_required_issue_2_cells():
    langs = {doc.lang for doc in corpus.DOCS}
    conditions = {doc.condition for doc in corpus.DOCS}
    formats = {doc.fmt for doc in corpus.DOCS}
    assert {"th", "en", "mixed"} <= langs
    assert {"clean", "degraded", "low_dpi", "born_digital"} <= conditions
    assert {"png", "jpeg", "tiff", "pdf"} <= formats
    assert any(len(doc.pages) > 1 for doc in corpus.DOCS), "need a multi-page document"


def test_corpus_plan_uses_pango_for_rendering_and_convert_for_derivations():
    from pathlib import Path

    steps = corpus.plan(Path("/tmp/corpus"))
    assert steps, "plan must not be empty"
    assert all(step.argv[0] in {"pango-view", "convert", "cp", "pdfunite"} for step in steps)
    assert any(step.argv[0] == "pango-view" for step in steps)
    degraded = [s for s in steps if s.doc_id == "th_degraded"]
    assert any(s.argv[0] == "convert" and "-blur" in s.argv for s in degraded)
    jpeg = [s for s in steps if s.doc_id == "en_jpeg"]
    assert any(s.argv[0] == "convert" and "-quality" in s.argv for s in jpeg)


def test_corpus_plan_is_pure():
    from pathlib import Path

    before = sorted(Path("/tmp/corpus").glob("**/*")) if Path("/tmp/corpus").exists() else []
    corpus.plan(Path("/tmp/corpus"))
    after = sorted(Path("/tmp/corpus").glob("**/*")) if Path("/tmp/corpus").exists() else []
    assert before == after


def test_corpus_manifest_ground_truth_hashes_match_text():
    from pathlib import Path

    manifest = corpus.manifest(Path("/tmp/does-not-exist"))
    assert manifest["synthetic"] is True
    assert manifest["totals"]["documents"] == len(corpus.DOCS)
    for doc, spec in zip(manifest["docs"], corpus.DOCS, strict=True):
        for page_entry, page in zip(doc["pages"], spec.pages, strict=True):
            assert page_entry["text_sha256"] == corpus.sha256_text(page.text)
        assert doc["artifact_sha256"] is None  # nothing built in this tmp path


def test_corpus_manifest_can_include_ground_truth_text():
    from pathlib import Path

    manifest = corpus.manifest(Path("/tmp/does-not-exist"), include_text=True)
    first = manifest["docs"][0]["pages"][0]
    assert first["text"] == corpus.DOCS[0].pages[0].text

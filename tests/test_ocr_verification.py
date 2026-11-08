"""Verification of the OCR v1 result body (`app/ocr_verification.py`).

The expected bodies below are written from the contract (`docs/ocr-v1-contracts.md`
§2.5, §2.6) rather than produced by the code under test, so a change in the
verifier or in the service has to disagree with this file to pass. Fakes only:
this says nothing about a deployed service.
"""

import json

from app.ocr_verification import summarize_result_body, verify_result_body

# A contract-conformant success: two pages, one of them from an embedded PDF text
# layer, warnings deduplicated in page order.
SUCCEEDED = {
    "schema_version": "ocr.v1",
    "job_id": "6d1f9b2a-0000-4000-8000-000000000001",
    "state": "succeeded",
    "reason": None,
    "completeness": "complete",
    "accuracy": "not_verified",
    "page_count": 2,
    "text": "ใบเสร็จรับเงิน\nreceipt page two",
    "pages": [
        {
            "page_number": 1,
            "status": "succeeded",
            "text_source": "embedded",
            "text": "ใบเสร็จรับเงิน",
            "warnings": ["no_text"],
            "error_code": None,
        },
        {
            "page_number": 2,
            "status": "succeeded",
            "text_source": "ocr",
            "text": "receipt page two",
            "warnings": ["no_text", "possible_low_quality"],
            "error_code": None,
        },
    ],
    "warnings": ["no_text", "possible_low_quality"],
    "timestamps": {
        "accepted_at": "2026-11-08T04:31:07Z",
        "finalized_at": "2026-11-08T04:33:07Z",
        "expires_at": "2026-11-09T04:33:07Z",
    },
}

# A partial job: the failed page keeps its place, carries an `error_code` and a
# null text, and contributes nothing to the combined text.
PARTIAL = {
    **SUCCEEDED,
    "state": "partially_succeeded",
    "reason": "deadline_exceeded",
    "completeness": "partial",
    "page_count": 2,
    "text": "first page only",
    "pages": [
        {
            "page_number": 1,
            "status": "succeeded",
            "text_source": "ocr",
            "text": "first page only",
            "warnings": [],
            "error_code": None,
        },
        {
            "page_number": 2,
            "status": "failed",
            "text_source": "ocr",
            "text": None,
            "warnings": [],
            "error_code": "page_engine_error",
        },
    ],
    "warnings": [],
    "timestamps": {
        "accepted_at": "2026-11-08T04:31:07Z",
        "finalized_at": "2026-11-09T04:31:07Z",
        "expires_at": "2026-11-10T04:31:07Z",
    },
}


def mutated(**changes):
    body = json.loads(json.dumps(SUCCEEDED))
    body.update(changes)
    return body


def test_a_conformant_result_verifies_and_a_partial_one_does_too():
    assert verify_result_body(SUCCEEDED) == []
    assert verify_result_body(PARTIAL) == []


def test_the_summary_carries_no_document_text():
    summary = summarize_result_body(SUCCEEDED)
    rendered = json.dumps(summary, ensure_ascii=False)

    assert summary["state"] == "succeeded"
    assert summary["page_count"] == 2
    assert [page["page_number"] for page in summary["pages"]] == [1, 2]
    assert summary["pages"][0]["text_source"] == "embedded"
    assert "ใบเสร็จรับเงิน" not in rendered
    assert "receipt page two" not in rendered


def test_a_missing_or_unexpected_key_is_reported():
    dropped = {key: value for key, value in SUCCEEDED.items() if key != "state"}

    assert "missing" in " ".join(verify_result_body(dropped))
    assert verify_result_body(dropped)
    assert verify_result_body(mutated(attempts=2))
    assert verify_result_body(mutated(engine_ref="tesseract:tha+eng"))
    assert verify_result_body(mutated(**{"สิ่งที่อ่าน": {"foo": None}}))


def test_the_retired_expense_shape_is_rejected():
    errors = verify_result_body(mutated(**{"Type of Expense": "อื่น ๆ"}))

    assert errors
    assert any("expense" in error.lower() for error in errors)


def test_a_version_other_than_the_contract_is_rejected():
    assert verify_result_body(mutated(schema_version="ocr.v2"))


def test_state_reason_and_accuracy_are_checked():
    assert verify_result_body(mutated(accuracy="verified"))
    assert verify_result_body(mutated(state="running", completeness="partial"))
    assert verify_result_body(mutated(reason="deadline_exceeded"))
    assert verify_result_body(mutated(state="failed", reason=None, completeness="partial", text=""))


def test_pages_are_numbered_and_accounted_for_one_per_source_page():
    unnumbered = mutated(page_count=1)
    unnumbered["pages"] = [dict(unnumbered["pages"][0], page_number=2)]

    errors = verify_result_body(unnumbered)

    assert any("page_number" in error for error in errors)
    assert verify_result_body(mutated(page_count=3))


def test_a_failed_page_is_visible_with_an_error_code_and_no_text():
    body = json.loads(json.dumps(PARTIAL))

    assert verify_result_body(body) == []
    body["pages"][1]["text"] = "should not be here"
    body["pages"][1]["error_code"] = None
    errors = verify_result_body(body)

    assert any("failed page" in error for error in errors)
    assert any("error_code" in error for error in errors)


def test_the_combined_text_is_the_successful_pages_joined_in_order():
    assert verify_result_body(mutated(text="receipt page two\nใบเสร็จรับเงิน"))
    assert verify_result_body(mutated(text=""))
    # A job with no successful page carries no `text` key at all.
    failed = json.loads(json.dumps(PARTIAL))
    failed["state"] = "failed"
    failed["completeness"] = "partial"
    failed["page_count"] = 1
    failed["pages"] = [failed["pages"][1]]
    failed["pages"][0]["page_number"] = 1
    failed.pop("text")

    assert verify_result_body(failed) == []


def test_completeness_and_warning_union_are_checked():
    assert verify_result_body(mutated(completeness="partial"))
    assert verify_result_body(mutated(warnings=["possible_low_quality", "no_text"]))
    assert verify_result_body(mutated(warnings=["no_text", "possible_low_quality", "no_text"]))

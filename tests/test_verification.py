import pytest

from app.templates import DOCUMENT_TYPES
from app.verification import summarize_api_response, verify_api_result


@pytest.mark.parametrize("document_type", DOCUMENT_TYPES)
def test_verify_api_result_accepts_exact_template_keys(document_type: str):
    template = DOCUMENT_TYPES[document_type]
    payload = {
        "document_type": document_type,
        "needs_review": False,
        "สิ่งที่อ่าน": {field: None for field in template.fields},
        "Analysis": (
            {"Type of Expense": None} if template.analysis_source else {}
        ),
    }

    assert verify_api_result(document_type, payload) == []


def test_verify_api_result_reports_wrong_type_and_keys():
    payload = {
        "document_type": "ทางด่วน",
        "สิ่งที่อ่าน": {"wrong": None},
        "Analysis": {"wrong": None},
    }

    errors = verify_api_result("ใบกำกับภาษี", payload)

    assert any("detected type" in error for error in errors)
    assert any("extraction keys" in error for error in errors)
    assert any("analysis keys" in error for error in errors)


def test_summarize_api_response_excludes_extracted_values():
    payload = {
        "document_type": "ทางด่วน",
        "needs_review": False,
        "classification_reason": "private diagnostic text",
        "สิ่งที่อ่าน": {
            "เลขที่เอกสาร": "SECRET-123",
            "วันที่": "2026-10-25",
            "Amount": "115.00",
        },
        "Analysis": {},
        "metadata": {"filename": "toll.pdf", "page_count": 1},
    }

    summary = summarize_api_response(payload)

    assert summary == {
        "document_type": "ทางด่วน",
        "needs_review": False,
        "extraction_keys": ["เลขที่เอกสาร", "วันที่", "Amount"],
        "analysis_keys": [],
        "metadata": {"filename": "toll.pdf", "page_count": 1},
    }
    assert "SECRET-123" not in str(summary)


def test_summarize_api_response_handles_non_object_json():
    assert summarize_api_response(["unexpected"]) == {
        "document_type": None,
        "needs_review": None,
        "extraction_keys": [],
        "analysis_keys": [],
        "metadata": {},
    }

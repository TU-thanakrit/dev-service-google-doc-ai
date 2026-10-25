from collections.abc import Iterator

import pytest

from app.service import DocumentProcessingService, ExtractionContractError, OcrResult
from app.templates import DOCUMENT_TYPES


class FakeOcr:
    def process(self, content: bytes, mime_type: str) -> OcrResult:
        assert content
        assert mime_type == "application/pdf"
        return OcrResult(text="ตัวอย่างเอกสารภาษาไทย", page_count=1)


class FakeModel:
    def __init__(self, outputs: list[dict]):
        self.outputs: Iterator[dict] = iter(outputs)
        self.calls: list[tuple[str, dict]] = []

    def generate_json(self, prompt: str, schema: dict) -> dict:
        self.calls.append((prompt, schema))
        return next(self.outputs)


@pytest.mark.parametrize("document_type", DOCUMENT_TYPES)
def test_process_returns_exact_keys_for_detected_template(document_type: str):
    template = DOCUMENT_TYPES[document_type]
    extracted = {field: f"value:{field}" for field in template.fields}
    analysis = {"Type of Expense": "ค่าใช้จ่ายทั่วไป"} if template.analysis_source else {}
    model = FakeModel(
        [
            {
                "document_type": document_type,
                "needs_review": False,
                "reason": "matched template",
            },
            {"สิ่งที่อ่าน": extracted, "Analysis": analysis},
        ]
    )
    service = DocumentProcessingService(ocr=FakeOcr(), model=model)

    result = service.process(b"pdf", "application/pdf", "sample.pdf")

    assert result["document_type"] == document_type
    assert result["needs_review"] is False
    assert tuple(result["สิ่งที่อ่าน"]) == template.fields
    assert result["Analysis"] == analysis
    assert result["metadata"]["filename"] == "sample.pdf"
    assert result["metadata"]["page_count"] == 1
    extraction_schema = model.calls[1][1]
    assert tuple(extraction_schema["properties"]["สิ่งที่อ่าน"]["properties"]) == template.fields


def test_unknown_document_stops_before_extraction():
    model = FakeModel(
        [
            {
                "document_type": "unknown",
                "needs_review": True,
                "reason": "not one of the five templates",
            }
        ]
    )
    service = DocumentProcessingService(ocr=FakeOcr(), model=model)

    result = service.process(b"pdf", "application/pdf", "unknown.pdf")

    assert result["document_type"] == "unknown"
    assert result["needs_review"] is True
    assert result["สิ่งที่อ่าน"] == {}
    assert result["Analysis"] == {}
    assert len(model.calls) == 1


def test_rejects_extraction_with_missing_or_extra_keys():
    document_type = "ทางด่วน"
    model = FakeModel(
        [
            {
                "document_type": document_type,
                "needs_review": False,
                "reason": "toll receipt",
            },
            {
                "สิ่งที่อ่าน": {
                    "เลขที่เอกสาร": "1",
                    "วันที่": "2026-01-01",
                    "unexpected": "bad",
                },
                "Analysis": {},
            },
        ]
    )
    service = DocumentProcessingService(ocr=FakeOcr(), model=model)

    with pytest.raises(ExtractionContractError):
        service.process(b"pdf", "application/pdf", "bad.pdf")


def test_rejects_non_boolean_classification_review_flag():
    model = FakeModel(
        [
            {
                "document_type": "ทางด่วน",
                "needs_review": "false",
                "reason": "toll receipt",
            }
        ]
    )
    service = DocumentProcessingService(ocr=FakeOcr(), model=model)

    with pytest.raises(ExtractionContractError, match="needs_review"):
        service.process(b"pdf", "application/pdf", "bad-classification.pdf")


def test_rejects_non_string_extraction_values():
    model = FakeModel(
        [
            {
                "document_type": "ทางด่วน",
                "needs_review": False,
                "reason": "toll receipt",
            },
            {
                "สิ่งที่อ่าน": {
                    "เลขที่เอกสาร": 123,
                    "วันที่": "2026-01-01",
                    "Amount": "50.00",
                },
                "Analysis": {},
            },
        ]
    )
    service = DocumentProcessingService(ocr=FakeOcr(), model=model)

    with pytest.raises(ExtractionContractError, match="string or null"):
        service.process(b"pdf", "application/pdf", "bad-value.pdf")

from fastapi.testclient import TestClient

from app.main import create_app

EXPECTED = {
    "document_type": "ทางด่วน",
    "needs_review": False,
    "classification_reason": "toll receipt",
    "สิ่งที่อ่าน": {
        "เลขที่เอกสาร": "T-1",
        "วันที่": "2026-10-25",
        "Amount": "50.00",
    },
    "Analysis": {},
    "metadata": {
        "filename": "toll.pdf",
        "mime_type": "application/pdf",
        "page_count": 1,
    },
}


class FakeProcessor:
    def process(self, content: bytes, mime_type: str, filename: str) -> dict:
        assert content == b"pdf-bytes"
        assert mime_type == "application/pdf"
        assert filename == "toll.pdf"
        return EXPECTED


class FailingProcessor:
    def process(self, content: bytes, mime_type: str, filename: str) -> dict:
        raise RuntimeError("secret processor path projects/123/processors/private")


def test_health_endpoint():
    client = TestClient(create_app(processor=FakeProcessor()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_process_pdf_returns_service_output():
    client = TestClient(create_app(processor=FakeProcessor()))

    response = client.post(
        "/v1/documents:process",
        files={"file": ("toll.pdf", b"pdf-bytes", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json() == EXPECTED


def test_rejects_unsupported_content_type():
    client = TestClient(create_app(processor=FakeProcessor()))

    response = client.post(
        "/v1/documents:process",
        files={"file": ("notes.txt", b"text", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported content type: text/plain"


def test_rejects_empty_upload():
    client = TestClient(create_app(processor=FakeProcessor()))

    response = client.post(
        "/v1/documents:process",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Empty file"


def test_internal_error_does_not_disclose_exception_text():
    client = TestClient(create_app(processor=FailingProcessor()))

    response = client.post(
        "/v1/documents:process",
        files={"file": ("bad.pdf", b"pdf-bytes", "application/pdf")},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Document processing failed"
    assert "projects/123" not in response.text

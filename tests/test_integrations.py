import json
from types import SimpleNamespace

from app.integrations import DocumentAiOcr, VertexJsonModel


class FakeDocumentAiClient:
    def __init__(self):
        self.request = None

    def processor_path(self, project: str, location: str, processor_id: str) -> str:
        assert project == "thaiunion-ai-platform"
        assert location == "asia-southeast1"
        assert processor_id == "62bd4850f12a7d55"
        return "processor-path"

    def process_document(self, request):
        self.request = request
        return SimpleNamespace(
            document=SimpleNamespace(
                text="OCR output",
                pages=[SimpleNamespace(), SimpleNamespace()],
            )
        )


class FakeModels:
    def __init__(self):
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(text=json.dumps({"document_type": "ทางด่วน"}))


class FakeGenAiClient:
    def __init__(self):
        self.models = FakeModels()


def test_document_ai_ocr_returns_text_and_page_count():
    client = FakeDocumentAiClient()
    ocr = DocumentAiOcr(
        project_id="thaiunion-ai-platform",
        location="asia-southeast1",
        processor_id="62bd4850f12a7d55",
        client=client,
    )

    result = ocr.process(b"pdf", "application/pdf")

    assert result.text == "OCR output"
    assert result.page_count == 2
    assert client.request.name == "processor-path"
    assert client.request.raw_document.content == b"pdf"
    assert client.request.raw_document.mime_type == "application/pdf"
    assert client.request.process_options.ocr_config.enable_native_pdf_parsing is True


def test_vertex_model_requests_json_schema_and_parses_response():
    client = FakeGenAiClient()
    model = VertexJsonModel(client=client, model_name="gemini-2.5-flash")
    schema = {
        "type": "object",
        "properties": {"document_type": {"type": "string"}},
        "required": ["document_type"],
    }

    result = model.generate_json("classify", schema)

    assert result == {"document_type": "ทางด่วน"}
    assert client.models.kwargs["model"] == "gemini-2.5-flash"
    assert client.models.kwargs["contents"] == "classify"
    config = client.models.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.temperature == 0

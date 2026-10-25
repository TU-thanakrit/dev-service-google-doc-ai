from app.config import Settings
from app.service import DocumentProcessingService


def test_settings_read_expected_gcp_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "thaiunion-ai-platform")
    monkeypatch.setenv("DOCAI_PROCESSOR_ID", "62bd4850f12a7d55")
    monkeypatch.setenv("DOCAI_LOCATION", "asia-southeast1")
    monkeypatch.setenv("VERTEX_LOCATION", "asia-southeast1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    settings = Settings()

    assert settings.project_id == "thaiunion-ai-platform"
    assert settings.docai_processor_id == "62bd4850f12a7d55"
    assert settings.docai_location == "asia-southeast1"
    assert settings.vertex_location == "asia-southeast1"
    assert settings.gemini_model == "gemini-2.5-flash"


def test_factory_builds_document_processing_service(monkeypatch):
    from app import factory

    settings = Settings(
        GOOGLE_CLOUD_PROJECT="thaiunion-ai-platform",
        DOCAI_PROCESSOR_ID="62bd4850f12a7d55",
    )
    sentinel_ocr = object()
    sentinel_model = object()
    monkeypatch.setattr(factory, "DocumentAiOcr", lambda **kwargs: sentinel_ocr)
    monkeypatch.setattr(factory, "VertexJsonModel", lambda **kwargs: sentinel_model)
    monkeypatch.setattr(factory.genai, "Client", lambda **kwargs: object())

    service = factory.build_service(settings)

    assert isinstance(service, DocumentProcessingService)
    assert service.ocr is sentinel_ocr
    assert service.model is sentinel_model

from google import genai
from google.genai import types

from app.config import Settings
from app.integrations import DocumentAiOcr, VertexJsonModel
from app.service import DocumentProcessingService


def build_service(settings: Settings | None = None) -> DocumentProcessingService:
    config = settings or Settings()
    genai_client = genai.Client(
        vertexai=True,
        project=config.project_id,
        location=config.vertex_location,
        http_options=types.HttpOptions(api_version="v1"),
    )
    return DocumentProcessingService(
        ocr=DocumentAiOcr(
            project_id=config.project_id,
            location=config.docai_location,
            processor_id=config.docai_processor_id,
        ),
        model=VertexJsonModel(
            client=genai_client,
            model_name=config.gemini_model,
        ),
    )

#!/usr/bin/env python3
"""Historical: runs the retired expense API locally with the active gcloud token.

This builds Document AI and Gemini clients, so it is not part of the OCR v1 service
and no documented procedure uses it; see `docs/ocr-v1-cutover.md`.
"""

import uvicorn
from google import genai
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from google.genai import types

from app.config import Settings
from app.gcloud_credentials import GcloudCliCredentials
from app.integrations import DocumentAiOcr, VertexJsonModel
from app.main import create_app
from app.service import DocumentProcessingService


def main() -> None:
    settings = Settings()
    credentials = GcloudCliCredentials()
    credentials.refresh(request=None)
    docai_client = documentai.DocumentProcessorServiceClient(
        credentials=credentials,
        client_options=ClientOptions(
            api_endpoint=f"{settings.docai_location}-documentai.googleapis.com"
        ),
    )
    genai_client = genai.Client(
        vertexai=True,
        project=settings.project_id,
        location=settings.vertex_location,
        credentials=credentials,
        http_options=types.HttpOptions(api_version="v1"),
    )
    processor = DocumentProcessingService(
        ocr=DocumentAiOcr(
            project_id=settings.project_id,
            location=settings.docai_location,
            processor_id=settings.docai_processor_id,
            client=docai_client,
        ),
        model=VertexJsonModel(
            model_name=settings.gemini_model,
            client=genai_client,
        ),
    )
    uvicorn.run(create_app(processor=processor), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()

import json
from typing import Any

from google import genai
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from google.genai import types

from app.service import OcrResult


class DocumentAiOcr:
    def __init__(
        self,
        project_id: str,
        location: str,
        processor_id: str,
        client: Any | None = None,
    ):
        self.project_id = project_id
        self.location = location
        self.processor_id = processor_id
        self.client = client or documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(
                api_endpoint=f"{location}-documentai.googleapis.com"
            )
        )

    def process(self, content: bytes, mime_type: str) -> OcrResult:
        processor_name = self.client.processor_path(
            self.project_id,
            self.location,
            self.processor_id,
        )
        request = documentai.ProcessRequest(
            name=processor_name,
            raw_document=documentai.RawDocument(
                content=content,
                mime_type=mime_type,
            ),
            process_options=documentai.ProcessOptions(
                ocr_config=documentai.OcrConfig(
                    enable_native_pdf_parsing=True,
                )
            ),
        )
        result = self.client.process_document(request=request)
        return OcrResult(
            text=result.document.text,
            page_count=len(result.document.pages),
        )


class VertexJsonModel:
    def __init__(
        self,
        model_name: str,
        client: genai.Client | Any,
    ):
        self.model_name = model_name
        self.client = client

    def generate_json(
        self,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        if not response.text:
            raise ValueError("Gemini returned an empty response")
        payload = json.loads(response.text)
        if not isinstance(payload, dict):
            raise ValueError("Gemini JSON response must be an object")
        return payload

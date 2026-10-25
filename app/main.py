import logging
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

SUPPORTED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


class Processor(Protocol):
    def process(self, content: bytes, mime_type: str, filename: str) -> dict[str, Any]: ...


def create_app(processor: Processor | None = None) -> FastAPI:
    app = FastAPI(
        title="Google Document AI Expense POC",
        version="0.1.0",
    )
    app.state.processor = processor

    def get_processor() -> Processor:
        if app.state.processor is None:
            from app.factory import build_service

            app.state.processor = build_service()
        return app.state.processor

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/documents:process")
    async def process_document(
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        mime_type = file.content_type or ""
        if mime_type not in SUPPORTED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported content type: {mime_type}",
            )

        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 20 MiB limit")

        filename = file.filename or "upload"
        try:
            return await run_in_threadpool(
                get_processor().process,
                content,
                mime_type,
                filename,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Document processing failed")
            raise HTTPException(
                status_code=500,
                detail="Document processing failed",
            ) from exc

    return app


app = create_app()

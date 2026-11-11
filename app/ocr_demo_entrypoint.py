"""Explicit deployable entrypoint for the public OCR demo service.

`app = build_app_from_env()` so missing or invalid demo configuration fails
process startup rather than serving a silent unconfigured application.

Deployment:
    uvicorn app.ocr_demo_entrypoint:app --host 0.0.0.0 --port ${PORT}

Keep Cloud Run request timeout above `OCR_DEMO_POLL_TIMEOUT_S` and run with
concurrency=1 for this demo.
"""

from app.ocr_demo import build_app_from_env

app = build_app_from_env()

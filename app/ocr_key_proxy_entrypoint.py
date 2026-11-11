"""ASGI entrypoint for the dev-only OCR API-key facade.

Environment (required):
- `OCR_KEY_PROXY_BACKEND_URL`: exact production backend origin
  `https://dev-ocr-service-api-1022222817897.asia-southeast1.run.app`.
- `OCR_KEY_PROXY_KEY`: strong shared demo key (>=32 non-whitespace ASCII chars).
- `OCR_KEY_PROXY_BACKEND_AUDIENCE`: must equal the exact backend origin.

The container launches this module via `uvicorn app.ocr_key_proxy_entrypoint:app`
under `exec` so PID 1 receives SIGTERM directly. No fallback startup path exists.
"""

from __future__ import annotations

import os

from app.ocr_key_proxy import create_app, validate_demo_key, validate_production_backend_url


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def build_app_from_env():
    backend_origin = validate_production_backend_url(
        _required("OCR_KEY_PROXY_BACKEND_URL")
    )
    demo_key = _required("OCR_KEY_PROXY_KEY")
    validate_demo_key(demo_key)
    audience = _required("OCR_KEY_PROXY_BACKEND_AUDIENCE")
    if audience != backend_origin:
        raise RuntimeError(
            "OCR_KEY_PROXY_BACKEND_AUDIENCE must equal the exact backend origin"
        )
    return create_app(
        backend_url=backend_origin,
        demo_key=demo_key,
        backend_audience=audience,
        require_strong_key=True,
    )


app = build_app_from_env()

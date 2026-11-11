"""Cloud Run API composition for the OCR tracer (container command
`uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}`).

This is the deployed API entrypoint: the container runs `app.ocr_api:app`, and
`app.main` (`POST /v1/documents:process`) is retained only as unreachable
historical code (`docs/ocr-v1-cutover.md`).

Settings, the worker pin, the team allowlist, and the admission bounds are
validated when the app is built, so a misconfigured deployment fails at startup.
The GCS store and the authenticated `jobs:run` session are constructed lazily on
the first non-health request (`_LazyDurableStore`, `_LazyAuthorizedSession`), so
importing `app.ocr_api` and serving `GET /health` construct neither ADC nor a GCS
client (contract §2.1, §11.2 #28).

Mutable live resource and acceptance facts are recorded in
`deploy/ocr-v1/sandbox.toml`. A private development deployment exists; n8n
end-to-end identity/token acceptance and paid/live OCR accuracy remain open.
"""

from typing import Any

import google.auth
from fastapi import FastAPI
from google.auth.transport.requests import AuthorizedSession

from app.gcs_objects import create_gcs_ocr_store
from app.ocr_auth import GoogleIdTokenVerifier
from app.ocr_config import OcrServiceSettings
from app.ocr_dispatch import CloudRunJobsDispatcher, CloudRunJobTemplateGuard
from app.ocr_store import DurableOcrStore
from app.ocr_tracer import MappingTeamDirectory, SystemClock, create_ocr_tracer_app

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class _LazyDurableStore:
    """Open the GCS store on the first store operation, never on `/health` (§2.1)."""

    def __init__(self, settings: OcrServiceSettings) -> None:
        self._settings = settings
        self._store: DurableOcrStore | None = None

    def _opened(self) -> DurableOcrStore:
        if self._store is None:
            self._store = create_gcs_ocr_store(
                self._settings.bucket, project=self._settings.bucket_project
            )
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._opened(), name)


class _LazyAuthorizedSession:
    """Acquire ADC and build the session on the first `jobs:run` / template read."""

    def __init__(self) -> None:
        self._session: Any = None

    def request(self, method: str, url: str, *, timeout: float, json: Any = None) -> Any:
        if self._session is None:
            credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            self._session = AuthorizedSession(credentials)
        return self._session.request(method, url, timeout=timeout, json=json)


def build_app(
    settings: OcrServiceSettings,
    *,
    session: Any = None,
    store: DurableOcrStore | None = None,
) -> FastAPI:
    """Compose the tracer app from the environment's settings and one session."""
    if store is None:
        store = create_gcs_ocr_store(settings.bucket, project=settings.bucket_project)
    if session is None:
        session = _LazyAuthorizedSession()
    dispatcher = CloudRunJobsDispatcher(
        session=session,
        project=settings.project,
        region=settings.region,
        # The operator's pin for new jobs; `settings.pin()` fails closed at startup
        # rather than accepting jobs it cannot pin (§8).
        pin=settings.pin(),
        guard=CloudRunJobTemplateGuard(
            session=session,
            project=settings.project,
            region=settings.region,
            request_timeout_s=settings.request_timeout_s,
        ),
        request_timeout_s=settings.request_timeout_s,
    )
    return create_ocr_tracer_app(
        store=store,
        dispatcher=dispatcher,
        clock=SystemClock(),
        verifier=GoogleIdTokenVerifier(audience=settings.api_audience),
        directory=MappingTeamDirectory(settings.teams),
        limits=settings.admission_limits(),
    )


def _build_from_environment() -> FastAPI:
    settings = OcrServiceSettings()
    return build_app(
        settings,
        session=_LazyAuthorizedSession(),
        store=_LazyDurableStore(settings),  # type: ignore[arg-type]
    )


app = _build_from_environment()

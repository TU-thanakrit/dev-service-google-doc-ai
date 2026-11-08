"""Cloud Run API composition for the OCR tracer (container command
`uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}`).

This is a separate composition, not `app.main`: the synchronous expense POC stays
mounted on `/v1/documents:process` until its own cutover ticket. Building this
app constructs a GCS client and an authenticated session, so it must only be
imported by the served role - never by a test, the fake-mode demos, or the worker.

Nothing here is verified cloud behaviour: deployment is blocked
(docs/ocr-v1-dispatch.md), and the ID-token path and IAM remain evidence gaps
(docs/ocr-v1-contracts.md, B2).
"""

import google.auth
from fastapi import FastAPI
from google.auth.transport.requests import AuthorizedSession

from app.gcs_objects import create_gcs_ocr_store
from app.ocr_auth import GoogleIdTokenVerifier
from app.ocr_config import OcrServiceSettings
from app.ocr_dispatch import CloudRunJobsDispatcher, CloudRunJobTemplateGuard
from app.ocr_tracer import MappingTeamDirectory, SystemClock, create_ocr_tracer_app

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def build_app(settings: OcrServiceSettings, *, session) -> FastAPI:
    """Compose the tracer app from the environment's settings and one session."""
    store = create_gcs_ocr_store(settings.bucket, project=settings.bucket_project)
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
    credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return build_app(OcrServiceSettings(), session=AuthorizedSession(credentials))


app = _build_from_environment()

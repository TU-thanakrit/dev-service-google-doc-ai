"""Cross-project bucket configuration: settings and the four role compositions.

The service runs in `thaiunion-ai-platform` while the durable bucket lives in
another approved project, so `create_gcs_ocr_store` is opened with the bucket's
own project, not the service project. `OCR_BUCKET_PROJECT` selects it and defaults
to the service project, so a single-project deployment is unchanged.

Fakes only: this proves the wiring and the default, not any real bucket, project,
or IAM fact. The real bucket behaviour is `tests/test_ocr_gcs_integration.py`.
"""

import importlib
import sys

import google.auth
import pytest

from app import gcs_objects
from app.ocr_cleanup import main as cleanup_main
from app.ocr_config import OcrServiceSettings
from app.ocr_pinning import PinError
from app.ocr_reconcile import main as reconcile_main
from app.ocr_worker import main as worker_main

SERVICE_PROJECT = "thaiunion-ai-platform"
BUCKET = "dev-ocr-service-data-asse1-1022222817897"
BUCKET_PROJECT = "thaiunion-shared-sandbox"
# A pinned worker revision: the API and reconcile roles refuse to start without
# one, because a deployment that cannot name its worker cannot pin a job (§8).
# A fake digest: nothing here is cloud evidence.
WORKER_DIGEST = "sha256:" + "3" * 64


class RecordingStore:
    """Minimal store for the compositions: no job exists, so a pass does nothing."""

    def get_job(self, team_id: str, job_id: str):
        return None

    def list_jobs(self) -> list:
        return []

    def list_job_ids(self) -> list:
        return []


@pytest.fixture()
def recorded(monkeypatch):
    """Set the cross-project environment and record the store arguments used."""
    seen: dict[str, object] = {}
    monkeypatch.setenv("OCR_PROJECT", SERVICE_PROJECT)
    monkeypatch.setenv("OCR_BUCKET", BUCKET)
    monkeypatch.setenv("OCR_BUCKET_PROJECT", BUCKET_PROJECT)
    monkeypatch.setenv("OCR_JOB_TEAM_ID", "team-a")
    monkeypatch.setenv("OCR_JOB_ID", "job-0001")
    monkeypatch.setenv("OCR_API_AUDIENCE", "https://ocr.example")
    monkeypatch.setenv("OCR_WORKER_REVISION_DIGEST", WORKER_DIGEST)

    def fake_create_gcs_ocr_store(
        bucket_name: str, *, project: str | None = None, client: object | None = None
    ):
        seen["bucket"] = bucket_name
        seen["project"] = project
        return RecordingStore()

    monkeypatch.setattr(gcs_objects, "create_gcs_ocr_store", fake_create_gcs_ocr_store)
    return seen


def test_settings_default_the_bucket_project_to_the_service_project(monkeypatch):
    monkeypatch.delenv("OCR_BUCKET_PROJECT", raising=False)
    monkeypatch.setenv("OCR_PROJECT", SERVICE_PROJECT)
    monkeypatch.setenv("OCR_BUCKET", BUCKET)

    settings = OcrServiceSettings()

    assert settings.bucket_project == SERVICE_PROJECT


def test_settings_read_the_bucket_project_from_the_environment(monkeypatch):
    monkeypatch.setenv("OCR_PROJECT", SERVICE_PROJECT)
    monkeypatch.setenv("OCR_BUCKET", BUCKET)
    monkeypatch.setenv("OCR_BUCKET_PROJECT", BUCKET_PROJECT)

    settings = OcrServiceSettings()

    assert settings.bucket_project == BUCKET_PROJECT


def test_worker_composition_opens_the_store_in_the_bucket_project(recorded):
    assert worker_main([]) == 1

    assert recorded == {"bucket": BUCKET, "project": BUCKET_PROJECT}


def test_reconcile_composition_opens_the_store_in_the_bucket_project(recorded, monkeypatch):
    monkeypatch.setattr("app.ocr_reconcile._authorized_session", lambda: object())

    assert reconcile_main([]) == 0

    assert recorded == {"bucket": BUCKET, "project": BUCKET_PROJECT}


def test_cleanup_composition_opens_the_store_in_the_bucket_project(recorded):
    assert cleanup_main([]) == 0

    assert recorded == {"bucket": BUCKET, "project": BUCKET_PROJECT}


def test_api_composition_opens_the_store_in_the_bucket_project(recorded, monkeypatch):
    # `app.ocr_api` builds its app at import time, so the ambient clients it would
    # construct are stubbed before the import and the module is dropped again.
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (object(), BUCKET_PROJECT))
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession", lambda credentials: object()
    )

    module = importlib.import_module("app.ocr_api")
    monkeypatch.delitem(sys.modules, "app.ocr_api", raising=False)
    module.build_app(OcrServiceSettings(), session=object())

    assert recorded == {"bucket": BUCKET, "project": BUCKET_PROJECT}


def test_the_reconcile_composition_refuses_to_start_without_a_usable_pin(recorded, monkeypatch):
    # Fail closed at startup: a role that cannot pin (or re-dispatch from) a usable
    # worker revision must not run at all (§8, D43).
    monkeypatch.delenv("OCR_WORKER_REVISION_DIGEST")
    monkeypatch.setattr("app.ocr_reconcile._authorized_session", lambda: object())

    with pytest.raises(PinError):
        reconcile_main([])

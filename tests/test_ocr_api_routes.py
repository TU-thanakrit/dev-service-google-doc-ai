"""The production container's app: which routes it serves, and what it is started as.

Seam: the module the container runs, `app.ocr_api`. The route table below is read
from the very app object `uvicorn app.ocr_api:app` serves, composed with injected
dependencies (a stub store factory, a stub credential factory, a stub session), so
this asserts the deployed surface without constructing a GCS client or a
credential, and without contacting GCP. Nothing here is cloud evidence: it is the
offline proof that the cutover's entrypoint is the OCR API and that the retired
`POST /v1/documents:process` is not reachable from it (issue #12).

The retire-path assertion is deliberately exact rather than a membership check:
the served app may expose the OCR routes and the framework's own documentation
routes, and nothing else under `/v1`.
"""

import importlib
import json
import sys
from fnmatch import fnmatch
from pathlib import Path

import google.auth
import pytest
from fastapi.testclient import TestClient

from app import gcs_objects

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_PROJECT = "thaiunion-ai-platform"
BUCKET = "dev-ocr-service-data-asse1-1022222817897"
BUCKET_PROJECT = "thaiunion-shared-sandbox"
API_AUDIENCE = "https://dev-ocr-service-api.example"
# A fake worker revision: the API composition refuses to start without a usable
# pin, and this one is never launched.
WORKER_DIGEST = "sha256:" + "4" * 64

# The contract's public surface (docs/ocr-v1-contracts.md §2.1).
OCR_ROUTES = {
    ("GET", "/health"),
    ("POST", "/v1/ocr/jobs"),
    ("GET", "/v1/ocr/jobs/{job_id}"),
    ("GET", "/v1/ocr/jobs/{job_id}/result"),
}
# The route the cutover retires, and the only method it ever had.
RETIRED_ROUTE = "/v1/documents:process"


class RecordingStore:
    """A store with no jobs: enough for the compositions, never contacted."""

    def get_job(self, team_id: str, job_id: str):
        return None

    def list_jobs(self) -> list:
        return []

    def list_job_ids(self) -> list:
        return []


@pytest.fixture()
def served_app(monkeypatch):
    """The imported `app.ocr_api` module, built with injected dependencies only."""
    monkeypatch.setenv("OCR_PROJECT", SERVICE_PROJECT)
    monkeypatch.setenv("OCR_BUCKET", BUCKET)
    monkeypatch.setenv("OCR_BUCKET_PROJECT", BUCKET_PROJECT)
    monkeypatch.setenv("OCR_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("OCR_WORKER_REVISION_DIGEST", WORKER_DIGEST)
    monkeypatch.setattr(gcs_objects, "create_gcs_ocr_store", lambda *a, **k: RecordingStore())
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (object(), BUCKET_PROJECT))
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession", lambda credentials: object()
    )
    # `app.ocr_api` builds its app at import time from these names, so it must be
    # imported after the stubs and removed again afterwards: a module cached here
    # would hand the next importer a stale, stubbed composition. Pop rather than
    # `monkeypatch.delitem`, whose undo would put the stubbed module back.
    sys.modules.pop("app.ocr_api", None)
    try:
        yield importlib.import_module("app.ocr_api")
    finally:
        sys.modules.pop("app.ocr_api", None)


def route_table(app) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }


def test_the_served_app_exposes_the_ocr_routes_and_no_other_v1_route(served_app):
    routes = route_table(served_app.app)

    served = {
        (method, path)
        for method, path in routes
        if path == "/health" or path.startswith("/v1")
    }
    assert served == OCR_ROUTES
    assert served_app.app.state is not None  # a real FastAPI app, not a stub


def test_the_retired_expense_route_is_absent_from_the_served_app(served_app):
    routes = route_table(served_app.app)
    assert RETIRED_ROUTE not in {path for _, path in routes}

    client = TestClient(served_app.app, raise_server_exceptions=False)
    # No route registration and no handler either: both verbs 404, so the retired
    # expense path cannot reach Gemini or the Document AI pipeline from this app.
    assert client.get(RETIRED_ROUTE).status_code == 404
    assert client.post(RETIRED_ROUTE).status_code == 404


def test_liveness_answers_without_a_cloud_client(served_app):
    client = TestClient(served_app.app)

    assert client.get("/health").json() == {"status": "ok"}


def test_importing_and_serving_health_when_adc_and_gcs_fail(monkeypatch):
    # Contract §2.1, §11.2 #28: `/health` starts and responds without ADC or GCS
    # construction, even when both are forced to fail if called.
    monkeypatch.setenv("OCR_PROJECT", SERVICE_PROJECT)
    monkeypatch.setenv("OCR_BUCKET", BUCKET)
    monkeypatch.setenv("OCR_BUCKET_PROJECT", BUCKET_PROJECT)
    monkeypatch.setenv("OCR_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("OCR_WORKER_REVISION_DIGEST", WORKER_DIGEST)

    def forbidden_cloud_call(*args, **kwargs):
        raise AssertionError("cloud client constructed on import or /health")

    monkeypatch.setattr(google.auth, "default", forbidden_cloud_call)
    monkeypatch.setattr(gcs_objects, "create_gcs_ocr_store", forbidden_cloud_call)
    monkeypatch.setattr(gcs_objects.storage, "Client", forbidden_cloud_call)
    sys.modules.pop("app.ocr_api", None)
    try:
        module = importlib.import_module("app.ocr_api")
        response = TestClient(module.app).get("/health")
    finally:
        sys.modules.pop("app.ocr_api", None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_container_command_serves_the_ocr_api_and_never_the_legacy_app():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = [line for line in dockerfile.splitlines() if line.startswith("CMD")]

    assert len(command) == 1, dockerfile
    assert "app.ocr_api:app" in command[0]
    assert "app.main" not in dockerfile


def test_the_documented_deploy_command_serves_the_ocr_api():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    # The only served entrypoint a reader can copy must be the OCR API: a
    # documented `app.main` command would redeploy the Gemini/expense path.
    assert "app.main" not in readme
    assert "app.ocr_api:app" in readme


def test_the_container_command_is_a_valid_exec_form():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = [line for line in dockerfile.splitlines() if line.startswith("CMD")][0]

    assert json.loads(command.removeprefix("CMD ")) == [
        "sh",
        "-c",
        "uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}",
    ]


def test_the_build_context_contains_what_the_image_copies():
    """The offline stand-in for `docker build --check`, which needs the registry."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignored = [
        line.strip().rstrip("/")
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    sources = [
        line.split()[-1] if line.split()[1].startswith("--") else line.split()[1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY")
    ]

    assert sources, dockerfile
    for source in sources:
        assert (REPO_ROOT / source).exists(), source
        assert not any(
            fnmatch(source, pattern) or source.startswith(f"{pattern}/") for pattern in ignored
        ), source
    # The OCR modules must actually be in the image: one image serves all four roles.
    assert "app" in sources

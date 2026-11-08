"""Public HTTP lifecycle tests for the local asynchronous OCR tracer (seam S1).

Dev/test only: the tracer is assembled from injected fakes, no GCP client is
created, and nothing here is evidence about real GCS behaviour.
"""

import logging
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.ocr_tracer import (
    JOB_LIFETIME,
    SCHEMA_VERSION,
    EnginePage,
    EngineResult,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
)
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    FakeTokenVerifier,
    InMemoryOcrStore,
)

TEAM_A = "team-a"
TEAM_B = "team-b"
TEAM_ID = TEAM_A
# Fake credentials: the fake verifier accepts these exact tokens and nothing
# else, so an invalid or expired identity is one it cannot verify (401).
PRINCIPALS = {
    "tok-team-a": Principal(email="analyst-a@example.com", subject="sub-team-a"),
    "tok-team-b": Principal(email="analyst-b@example.com", subject="sub-team-b"),
    # Verified by Google but absent from the team allowlist (403).
    "tok-unmapped": Principal(email="outsider@example.com", subject="sub-outsider"),
}
TEAM_DIRECTORY = {"analyst-a@example.com": TEAM_A, "analyst-b@example.com": TEAM_B}
REPO_ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF"
TIFF_BYTES = b"II*\x00\x08\x00\x00\x00"
EXPENSE_FIELDS = (
    "document_type",
    "needs_review",
    "classification_reason",
    "สิ่งที่อ่าน",
    "Analysis",
    "metadata",
)
DEFAULT_PAGES = (EnginePage(text="หน้าแรก"), EnginePage(text="page two"))


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class BlockingEngine:
    """Fake engine that holds recognition until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[bytes, str]] = []

    def recognize(self, content: bytes, mime_type: str) -> EngineResult:
        self.calls.append((content, mime_type))
        self.entered.set()
        assert self.release.wait(timeout=5)
        return EngineResult(pages=(EnginePage(text="blocked page"),))


class ExplodingStore(InMemoryOcrStore):
    """Fake store whose reads fail, to exercise the safe error path."""

    def get_job(self, team_id: str, job_id: str):
        raise RuntimeError("gs://private-bucket/path not readable")


def build_tracer(
    pages=DEFAULT_PAGES,
    *,
    token: str | None = "tok-team-a",
    directory=None,
    engine=None,
    store=None,
    acknowledge=True,
):
    store = store or InMemoryOcrStore()
    engine = engine or FakeOcrEngine(pages)
    clock = FakeClock(START)
    dispatcher = ControllableDispatcher(
        store=store, engine=engine, clock=clock, acknowledge=acknowledge
    )
    app = create_ocr_tracer_app(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        verifier=FakeTokenVerifier(PRINCIPALS),
        directory=MappingTeamDirectory(
            TEAM_DIRECTORY if directory is None else directory
        ),
    )
    return SimpleNamespace(
        client=TestClient(
            app,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            raise_server_exceptions=False,
        ),
        # Unauthenticated client: identity comes only from a request credential.
        anonymous=TestClient(app, raise_server_exceptions=False),
        store=store,
        engine=engine,
        clock=clock,
        dispatcher=dispatcher,
    )


def submit(
    tracer,
    *,
    content=PDF_BYTES,
    filename="doc.pdf",
    content_type="application/pdf",
    key="retry-key-0001",
):
    return tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": (filename, content, content_type)},
    )


def assert_error(response, status_code, code, request_id=None):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"schema_version", "error"}
    assert body["schema_version"] == SCHEMA_VERSION
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == request_id
    # Fixed allowlisted message: nothing about paths, buckets, jobs or tokens.
    assert isinstance(body["error"]["message"], str)
    assert "/" not in body["error"]["message"]


def test_health_returns_ok():
    response = build_tracer().client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_submission_returns_identity_and_dispatch_metadata_before_recognition_runs():
    tracer = build_tracer()

    response = submit(tracer)

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "schema_version": SCHEMA_VERSION,
        "job_id": body["job_id"],
        "state": "accepted",
        "timestamps": {
            "accepted_at": "2026-11-08T04:31:07Z",
            "deadline_at": "2026-11-09T04:31:07Z",
            "finalized_at": None,
            "expires_at": None,
        },
        # AC #2 engine-processing metadata: the recorded launch intent is what the
        # caller sees, and the execution reference is stable for the job.
        "dispatch": {
            "acknowledged": True,
            "execution_ref": f"fake-execution-{body['job_id']}",
        },
    }
    assert uuid.UUID(body["job_id"]).version == 4
    assert not any(field in body for field in EXPENSE_FIELDS)
    assert tracer.engine.calls == []


def test_status_polls_accepted_then_succeeded_after_manual_dispatch():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]

    before = tracer.client.get(f"/v1/ocr/jobs/{job_id}")
    assert before.status_code == 200
    assert before.json()["state"] == "accepted"
    assert "progress" not in before.json()

    tracer.dispatcher.run_pending()

    after = tracer.client.get(f"/v1/ocr/jobs/{job_id}")
    assert after.status_code == 200
    assert after.json()["state"] == "succeeded"
    assert after.json()["progress"] == {"pages_total": 2, "pages_done": 2}
    assert tracer.engine.calls == [(PDF_BYTES, "application/pdf")]


def test_status_reports_running_while_recognition_is_still_controlled():
    engine = BlockingEngine()
    tracer = build_tracer(engine=engine)
    job_id = submit(tracer).json()["job_id"]

    worker = threading.Thread(target=tracer.dispatcher.run_pending)
    worker.start()
    try:
        assert engine.entered.wait(timeout=5)
        body = tracer.client.get(f"/v1/ocr/jobs/{job_id}").json()
        assert body["state"] == "running"
        assert body["timestamps"]["finalized_at"] is None
    finally:
        engine.release.set()
        worker.join(timeout=5)


def test_result_is_not_ready_before_the_job_finalizes():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]

    response = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")

    assert response.status_code == 202
    assert response.headers["Retry-After"] == "10"
    assert response.json()["state"] == "accepted"


def test_result_exposes_ordered_pages_and_combined_text():
    tracer = build_tracer(
        pages=(
            EnginePage(text="  หน้าแรก  "),
            EnginePage(text="page two", text_source="embedded"),
            EnginePage(text="last page"),
        )
    )
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()

    response = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "state": "succeeded",
        "reason": None,
        "completeness": "complete",
        "accuracy": "not_verified",
        "page_count": 3,
        "text": "หน้าแรก\npage two\nlast page",
        "pages": [
            {
                "page_number": 1,
                "status": "succeeded",
                "text_source": "ocr",
                "text": "หน้าแรก",
                "warnings": [],
                "error_code": None,
            },
            {
                "page_number": 2,
                "status": "succeeded",
                "text_source": "embedded",
                "text": "page two",
                "warnings": [],
                "error_code": None,
            },
            {
                "page_number": 3,
                "status": "succeeded",
                "text_source": "ocr",
                "text": "last page",
                "warnings": [],
                "error_code": None,
            },
        ],
        "warnings": [],
        "timestamps": {
            "accepted_at": "2026-11-08T04:31:07Z",
            "finalized_at": "2026-11-08T04:31:07Z",
            "expires_at": "2026-11-09T04:31:07Z",
        },
    }
    assert not any(
        field in body for field in (*EXPENSE_FIELDS, "engine_ref", "worker_revision_digest")
    )


def test_result_reports_no_text_as_a_warning_not_a_failure():
    tracer = build_tracer(pages=(EnginePage(text="", warnings=("no_text",)),))
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()

    assert body["state"] == "succeeded"
    assert body["completeness"] == "complete"
    assert body["text"] == ""
    assert body["warnings"] == ["no_text"]
    assert body["pages"][0]["warnings"] == ["no_text"]
    assert body["pages"][0]["error_code"] is None


def test_retention_clock_starts_at_finalization():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]
    tracer.clock.advance(timedelta(hours=3))
    tracer.dispatcher.run_pending()

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()

    assert body["timestamps"]["accepted_at"] == "2026-11-08T04:31:07Z"
    assert body["timestamps"]["finalized_at"] == "2026-11-08T07:31:07Z"
    assert body["timestamps"]["expires_at"] == "2026-11-09T07:31:07Z"


def test_duplicate_submission_reuses_the_job_and_recognizes_once():
    tracer = build_tracer()

    first = submit(tracer)
    duplicate = submit(tracer)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()

    tracer.dispatcher.run_pending()

    assert len(tracer.engine.calls) == 1
    job_id = first.json()["job_id"]
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200


def test_duplicate_after_finalization_is_served_without_dispatching_again():
    tracer = build_tracer()
    submit(tracer)
    tracer.dispatcher.run_pending()

    duplicate = submit(tracer)

    assert duplicate.status_code == 200
    assert duplicate.json()["state"] == "succeeded"
    assert tracer.dispatcher.pending == []
    tracer.dispatcher.run_pending()
    assert len(tracer.engine.calls) == 1


def test_same_key_with_different_content_conflicts():
    tracer = build_tracer()
    accepted = submit(tracer)

    conflict = submit(tracer, content=PDF_BYTES + b"% different document\n")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    job_id = accepted.json()["job_id"]
    still_there = tracer.client.get(f"/v1/ocr/jobs/{job_id}")
    assert still_there.status_code == 200
    assert still_there.json()["state"] == "accepted"


def test_supported_containers_are_accepted_by_content_not_by_declaration():
    tracer = build_tracer()

    responses = [
        submit(tracer, content=PDF_BYTES, content_type="application/pdf", key="key-pdf-0001"),
        submit(tracer, content=PNG_BYTES, content_type="image/png", key="key-png-0001"),
        submit(tracer, content=JPEG_BYTES, content_type="image/jpeg", key="key-jpg-0001"),
        submit(tracer, content=TIFF_BYTES, content_type="image/tiff", key="key-tif-0001"),
    ]

    assert [response.status_code for response in responses] == [202, 202, 202, 202]


def test_missing_or_malformed_idempotency_key_is_rejected():
    tracer = build_tracer()

    without_header = tracer.client.post(
        "/v1/ocr/jobs",
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert_error(without_header, 400, "invalid_idempotency_key")

    for key in ("short", "bad key with spaces", "x" * 129):
        assert_error(submit(tracer, key=key), 400, "invalid_idempotency_key")


def test_request_id_is_echoed_in_error_bodies():
    response = build_tracer().client.post(
        "/v1/ocr/jobs",
        headers={"X-Request-Id": "req-42"},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )

    assert_error(response, 400, "invalid_idempotency_key", request_id="req-42")


def test_empty_document_is_rejected():
    response = submit(build_tracer(), content=b"", content_type="application/pdf")

    assert_error(response, 422, "empty_document")


def test_unsupported_or_mismatched_media_type_is_rejected():
    tracer = build_tracer()

    text_upload = submit(tracer, content=b"plain notes", content_type="text/plain")
    assert_error(text_upload, 415, "unsupported_media_type")

    declared_pdf_but_png = submit(tracer, content=PNG_BYTES, content_type="application/pdf")
    assert_error(declared_pdf_but_png, 415, "unsupported_media_type")

    zip_archive = submit(tracer, content=b"PK\x03\x04archive", content_type="application/pdf")
    assert_error(zip_archive, 415, "unsupported_media_type")


UNKNOWN_JOB = "6d1f9b2a-0000-4000-8000-000000000000"


def test_missing_or_invalid_credential_is_unauthorized_on_every_route():
    tracer = build_tracer()

    missing = tracer.anonymous.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": "key-no-credential"},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert_error(missing, 401, "unauthorized_caller")

    # An expired or forged token is simply one the verifier cannot verify.
    invalid = tracer.anonymous.post(
        "/v1/ocr/jobs",
        headers={
            "Idempotency-Key": "key-bad-credential",
            "Authorization": "Bearer expired-or-forged-token",
        },
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert_error(invalid, 401, "unauthorized_caller")
    assert "expired-or-forged-token" not in invalid.text

    assert_error(tracer.anonymous.get(f"/v1/ocr/jobs/{UNKNOWN_JOB}"), 401, "unauthorized_caller")
    assert_error(
        tracer.anonymous.get(f"/v1/ocr/jobs/{UNKNOWN_JOB}/result"),
        401,
        "unauthorized_caller",
    )
    # A rejected credential creates nothing.
    assert tracer.store.jobs == {}


def test_verified_caller_without_a_team_mapping_is_forbidden():
    tracer = build_tracer(token="tok-unmapped")

    for response in (
        submit(tracer),
        tracer.client.get(f"/v1/ocr/jobs/{UNKNOWN_JOB}"),
        tracer.client.get(f"/v1/ocr/jobs/{UNKNOWN_JOB}/result"),
    ):
        assert_error(response, 403, "team_not_authorized")
        assert "outsider@example.com" not in response.text
    assert tracer.store.jobs == {}


def test_forged_identity_headers_neither_authenticate_nor_pick_a_team():
    tracer = build_tracer()
    forged = {
        "X-Goog-Authenticated-User-Email": "accounts.google.com:analyst-b@example.com",
        "X-Goog-Authenticated-User-Id": "accounts.google.com:sub-team-b",
        "X-Forwarded-User": "analyst-b@example.com",
        "X-Serverless-Authorization": "Bearer tok-team-b",
        "X-Team-Id": TEAM_B,
    }

    unauthenticated = tracer.anonymous.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": "key-forged-0001", **forged},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert_error(unauthenticated, 401, "unauthorized_caller")
    assert tracer.store.jobs == {}

    # The real credential decides ownership; forged team headers change nothing.
    accepted = tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": "key-forged-0002", **forged},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    assert tracer.store.get_job(TEAM_A, job_id) is not None
    assert tracer.store.get_job(TEAM_B, job_id) is None
    foreign = build_tracer(store=tracer.store, token="tok-team-b")
    assert_error(foreign.client.get(f"/v1/ocr/jobs/{job_id}"), 404, "not_found")


def test_caller_supplied_team_identifiers_are_ignored():
    tracer = build_tracer()

    response = tracer.client.post(
        f"/v1/ocr/jobs?team_id={TEAM_B}",
        headers={"Idempotency-Key": "key-spoof-team"},
        data={"team_id": TEAM_B},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = tracer.store.get_job(TEAM_A, job_id)
    assert job is not None
    assert job.team_id == TEAM_A
    assert tracer.store.get_job(TEAM_B, job_id) is None


def test_unknown_job_id_is_not_found_on_both_read_routes():
    tracer = build_tracer()
    unknown = "6d1f9b2a-0000-4000-8000-000000000000"

    assert_error(tracer.client.get(f"/v1/ocr/jobs/{unknown}"), 404, "not_found")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{unknown}/result"), 404, "not_found")


def test_another_team_cannot_read_a_guessed_job_identifier():
    store = InMemoryOcrStore()
    tracer = build_tracer(store=store)
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()

    # Same team, authorized: both read routes serve the whole lifecycle.
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}").status_code == 200
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200

    foreign = build_tracer(store=store, token="tok-team-b")
    for response in (
        foreign.client.get(f"/v1/ocr/jobs/{job_id}"),
        foreign.client.get(f"/v1/ocr/jobs/{job_id}/result"),
    ):
        assert_error(response, 404, "not_found")
        # No existence oracle, document text, or result on the refusal.
        assert "หน้าแรก" not in response.text


def test_idempotency_keys_are_scoped_to_the_verified_team():
    store = InMemoryOcrStore()

    first = submit(build_tracer(store=store))
    second = submit(build_tracer(store=store, token="tok-team-b"))

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["job_id"] != second.json()["job_id"]


def test_finalized_result_serves_until_its_expiry_and_is_gone_after():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()

    tracer.clock.advance(timedelta(hours=24) - timedelta(seconds=1))
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200

    tracer.clock.advance(timedelta(seconds=1))
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}"), 410, "job_expired")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}/result"), 410, "job_expired")


def test_unfinished_job_stops_serving_at_its_deadline():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]

    tracer.clock.advance(timedelta(hours=24) - timedelta(seconds=1))
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}").status_code == 200

    tracer.clock.advance(timedelta(seconds=1))
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}"), 410, "job_expired")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}/result"), 410, "job_expired")


def test_storage_failure_is_reported_without_leaking_details():
    tracer = build_tracer(store=ExplodingStore())

    response = tracer.client.get("/v1/ocr/jobs/6d1f9b2a-0000-4000-8000-000000000000")

    assert_error(response, 500, "internal_error")
    assert "private-bucket" not in response.text


def test_internal_failure_logs_a_fixed_category_not_the_external_error(caplog):
    tracer = build_tracer(store=ExplodingStore())

    with caplog.at_level(logging.ERROR, logger="app.ocr_tracer"):
        response = tracer.client.get(
            "/v1/ocr/jobs/6d1f9b2a-0000-4000-8000-000000000000",
            headers={"X-Request-Id": "req-500"},
        )

    assert_error(response, 500, "internal_error", request_id="req-500")
    logged = caplog.text
    # Allowlisted fields only: a fixed category plus safe identifiers.
    assert "internal_error" in logged
    assert "req-500" in logged
    assert "6d1f9b2a-0000-4000-8000-000000000000" in logged
    # Never the external exception text, its class, or a traceback (D20).
    assert "private-bucket" not in logged
    assert "RuntimeError" not in logged
    assert "Traceback" not in logged


def test_engine_processing_metadata_is_stable_and_visible_to_callers():
    tracer = build_tracer()
    submitted = submit(tracer)
    job_id = submitted.json()["job_id"]

    polled = tracer.client.get(f"/v1/ocr/jobs/{job_id}").json()

    assert submitted.json()["dispatch"]["acknowledged"] is True
    assert submitted.json()["dispatch"]["execution_ref"]
    assert polled["dispatch"] == submitted.json()["dispatch"]


def test_jobs_pin_the_engine_and_worker_revision_without_exposing_them():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()

    job = tracer.store.get_job(TEAM_ID, job_id)
    assert (job.engine_ref, job.worker_revision_digest) == (
        tracer.dispatcher.engine_ref,
        tracer.dispatcher.worker_revision_digest,
    )

    status = tracer.client.get(f"/v1/ocr/jobs/{job_id}").json()
    result = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert not {"engine_ref", "worker_revision_digest"} & (status.keys() | result.keys())


def test_a_launch_that_is_not_acknowledged_is_still_accepted():
    tracer = build_tracer(acknowledge=False)

    response = submit(tracer)

    assert response.status_code == 202
    assert response.json()["state"] == "accepted"
    assert response.json()["dispatch"] == {"acknowledged": False, "execution_ref": None}


def test_live_retry_returns_the_existing_status_without_dispatching_again():
    tracer = build_tracer()
    first = submit(tracer)

    duplicate = submit(tracer)

    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()
    # One recorded launch intent for the one logical job: the retry starts no
    # second dispatch and rewrites no dispatch metadata.
    assert len(tracer.dispatcher.pending) == 1
    tracer.dispatcher.run_pending()
    assert len(tracer.engine.calls) == 1


def test_worker_that_reaches_the_deadline_finalizes_it_without_processing():
    tracer = build_tracer()
    job_id = submit(tracer).json()["job_id"]
    tracer.clock.advance(JOB_LIFETIME)

    tracer.dispatcher.run_pending()

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert body["state"] == "failed"
    assert body["reason"] == "deadline_exceeded"
    assert body["page_count"] == 0
    assert tracer.engine.calls == []


def test_worker_crossing_the_deadline_cannot_finalize_succeeded():
    engine = BlockingEngine()
    tracer = build_tracer(engine=engine)
    job_id = submit(tracer).json()["job_id"]

    worker = threading.Thread(target=tracer.dispatcher.run_pending)
    worker.start()
    try:
        assert engine.entered.wait(timeout=5)
        tracer.clock.advance(JOB_LIFETIME + timedelta(seconds=1))
    finally:
        engine.release.set()
        worker.join(timeout=5)

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert body["state"] == "failed"
    assert body["reason"] == "deadline_exceeded"
    assert body["completeness"] == "partial"


def test_fake_mode_command_runs_the_lifecycle_without_cloud_clients():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ocr_tracer_demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "lifecycle ok" in result.stdout

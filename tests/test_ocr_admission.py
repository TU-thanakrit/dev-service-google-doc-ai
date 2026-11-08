"""Admission and input-boundary tests for the OCR v1 API (issue #10).

Seams: S1 (the public HTTP lifecycle through `TestClient`) for every rejection
the caller can see, plus the ASGI edge itself for the one bound a test client
cannot produce - a request that arrives without `Content-Length`, where the only
protection is a bounded streamed read before multipart parsing.

Dev/test only: injected fakes, no GCP client, no live OCR, and no cloud call of
any kind. Fake-client success is not cloud evidence (docs/ocr-v1-contracts.md,
B1-B4 stay open).
"""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from ocr_documents import jpeg_bytes, pdf_bytes, png_bytes, tiff_bytes

from app.ocr_admission import (
    CLOUD_RUN_HTTP1_MAX_REQUEST_BYTES,
    DEFAULT_CAPACITY_CEILING,
    MAX_IMAGE_PIXELS,
    MAX_PAGES,
    MAX_UPLOAD_BYTES,
    PER_ATTEMPT_TIMEOUT,
    AdmissionLimits,
    RequestSizeLimitMiddleware,
    assert_limits,
)
from app.ocr_config import OcrServiceSettings
from app.ocr_reconcile import reconcile
from app.ocr_store import CAPACITY_NAME, CAPACITY_OBJECT_BUDGET
from app.ocr_tracer import (
    JOB_LIFETIME,
    SCHEMA_VERSION,
    EnginePage,
    EnginePageError,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
)
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    FakeTokenVerifier,
    InMemoryObjectStore,
    InMemoryOcrStore,
)

TEAM_ID = "team-a"
TEAM_DIRECTORY = {"analyst-a@example.com": TEAM_ID}
PRINCIPALS = {
    "tok-team-a": Principal(email="analyst-a@example.com", subject="sub-team-a"),
}
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
DEFAULT_LIMITS = AdmissionLimits(capacity_ceiling=1)
ONE_PAGE_PDF = pdf_bytes()


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class CountingEngine(FakeOcrEngine):
    """Engine that reports a page count the container bytes do not carry."""

    def __init__(self, total: int) -> None:
        super().__init__([EnginePage(text="page") for _ in range(total)])
        self._total = total

    def page_count(self, content: bytes, mime_type: str) -> int:
        return self._total


class UndecodableEngine(FakeOcrEngine):
    """Engine whose decoder rejects the container the API accepted."""

    def __init__(self) -> None:
        super().__init__([EnginePage(text="page")])

    def page_count(self, content: bytes, mime_type: str) -> int:
        raise RuntimeError("decoder rejected the container")


def build(*, limits=None, store=None, objects=None, engine=None, token="tok-team-a"):
    store = store or InMemoryOcrStore(objects)
    engine = engine or FakeOcrEngine([EnginePage(text="หน้าแรก")])
    clock = FakeClock(START)
    dispatcher = ControllableDispatcher(store=store, engine=engine, clock=clock)
    app = create_ocr_tracer_app(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        verifier=FakeTokenVerifier(PRINCIPALS),
        directory=MappingTeamDirectory(TEAM_DIRECTORY),
        limits=DEFAULT_LIMITS if limits is None else limits,
    )
    return SimpleNamespace(
        client=TestClient(
            app,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            raise_server_exceptions=False,
        ),
        store=store,
        engine=engine,
        clock=clock,
        dispatcher=dispatcher,
    )


def submit(tracer, *, content=ONE_PAGE_PDF, content_type="application/pdf", key="admission-key-1"):
    return tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("doc", content, content_type)},
    )


def assert_error(response, status_code, code):
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"schema_version", "error"}
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["error"]["code"] == code
    assert "/" not in body["error"]["message"]


def job_objects(store):
    return [name for name in store.objects.names() if ".json" in name and "/jobs/" in name]


# --- the request-size bound, before multipart parsing (contract §10/D38) ---------


def test_input_over_the_byte_ceiling_is_rejected_before_it_is_parsed():
    limits = AdmissionLimits(max_upload_bytes=1024, capacity_ceiling=1)
    tracer = build(limits=limits)
    oversized = pdf_bytes() + b"%" + b"x" * 4096

    response = submit(tracer, content=oversized)

    assert_error(response, 413, "file_too_large")
    assert tracer.store.objects.names() == []
    assert tracer.engine.calls == []


def _middleware_case(max_bytes, messages):
    """Drive the middleware directly, so no test-client transport can hide it."""
    sent: list[dict] = []
    received: list[bytes] = []
    called = False
    pending = list(messages)

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        while True:
            message = await receive()
            received.append(message.get("body", b""))
            if not message.get("more_body"):
                break

    async def receive():
        return pending.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/ocr/jobs",
        "headers": [(b"x-request-id", b"req-42")],
        "query_string": b"",
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1234),
    }
    middleware = RequestSizeLimitMiddleware(downstream, max_bytes=max_bytes)
    asyncio.run(middleware(scope, receive, send))
    return called, received, sent


def _body(chunks):
    return [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]


def test_a_streamed_request_without_content_length_is_cut_off_at_the_ceiling():
    called, _, sent = _middleware_case(1024, _body([b"x" * 512] * 8))

    # The endpoint never ran, so nothing was parsed or spooled downstream.
    assert called is False
    start = sent[0]
    assert (start["type"], start["status"]) == ("http.response.start", 413)
    assert (b"content-type", b"application/json") in start["headers"]
    body = sent[1]["body"].decode()
    assert '"code": "file_too_large"' in body
    assert '"request_id": "req-42"' in body


def test_a_streamed_request_within_the_ceiling_is_replayed_whole():
    chunks = [b"%PDF-1.7\n", b"%%EOF\n"]

    called, received, sent = _middleware_case(1024, _body(chunks))

    assert called is True
    assert b"".join(received) == b"".join(chunks)
    assert sent == []


def test_content_length_over_the_ceiling_is_refused_without_reading_the_body():
    pending = [{"type": "http.request", "body": b"x" * 2048, "more_body": False}]
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return pending.pop(0)

    sent: list[dict] = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/ocr/jobs",
        "headers": [(b"content-length", b"4096")],
        "query_string": b"",
    }
    asyncio.run(
        RequestSizeLimitMiddleware(downstream, max_bytes=1024)(
            scope, receive, lambda message: _collect(sent, message)
        )
    )

    assert called is False
    assert pending == [{"type": "http.request", "body": b"x" * 2048, "more_body": False}]
    assert sent[0]["status"] == 413


async def _collect(sent, message):
    sent.append(message)


# --- structural validation on bytes (contract §2.3) -----------------------------


def test_every_supported_container_is_accepted_when_it_is_structurally_whole():
    tracer = build(limits=AdmissionLimits(capacity_ceiling=4))
    containers = [
        (pdf_bytes(pages=1), "application/pdf", "key-pdf-1"),
        (png_bytes(), "image/png", "key-png-1"),
        (jpeg_bytes(), "image/jpeg", "key-jpg-1"),
        (tiff_bytes(frames=2), "image/tiff", "key-tif-1"),
    ]

    responses = [
        submit(tracer, content=content, content_type=declared, key=key)
        for content, declared, key in containers
    ]

    assert [response.status_code for response in responses] == [202, 202, 202, 202]
    assert tracer.engine.calls == []


@pytest.mark.parametrize(
    "content, declared, code",
    [
        (pdf_bytes(pages=0, trailer=False), "application/pdf", "malformed_document"),
        (pdf_bytes(encrypted=True), "application/pdf", "encrypted_document"),
        (pdf_bytes()[:12], "application/pdf", "malformed_document"),
        (png_bytes(complete=False), "image/png", "malformed_document"),
        (jpeg_bytes(complete=False), "image/jpeg", "malformed_document"),
        (tiff_bytes(complete=False), "image/tiff", "malformed_document"),
    ],
)
def test_malformed_encrypted_and_truncated_documents_are_rejected_before_recognition(
    content, declared, code
):
    tracer = build()

    response = submit(tracer, content=content, content_type=declared)

    assert_error(response, 422, code)
    assert tracer.store.objects.names() == []
    assert tracer.engine.calls == []


def test_page_count_over_the_limit_is_rejected_before_a_job_exists():
    tracer = build(limits=AdmissionLimits(max_pages=5, capacity_ceiling=1))

    response = submit(tracer, content=pdf_bytes(pages=6))

    assert_error(response, 422, "too_many_pages")
    assert tracer.store.objects.names() == []
    assert tracer.engine.calls == []


def test_image_dimensions_over_the_pixel_budget_are_rejected():
    tracer = build()
    bomb = png_bytes(width=20_000, height=MAX_IMAGE_PIXELS // 20_000 + 1)

    response = submit(tracer, content=bomb, content_type="image/png")

    assert_error(response, 413, "file_too_large")
    assert tracer.store.objects.names() == []
    assert tracer.engine.calls == []


def test_declared_type_still_has_to_match_the_bytes():
    tracer = build()

    response = submit(tracer, content=png_bytes(), content_type="application/pdf")

    assert_error(response, 415, "unsupported_media_type")
    assert tracer.store.objects.names() == []


# --- the worker's own page bound (defence for what the bytes hid) ---------------


def test_the_worker_refuses_a_container_over_the_page_limit_without_recognizing_it():
    engine = CountingEngine(MAX_PAGES + 3)
    tracer = build(engine=engine)
    job_id = submit(tracer).json()["job_id"]

    tracer.dispatcher.run_pending()

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert (body["state"], body["reason"]) == ("failed", "input_unreadable")
    assert [page["error_code"] for page in body["pages"]] == ["too_many_pages"]
    assert engine.calls == []


def test_a_container_the_worker_cannot_decode_is_reported_instead_of_crashing():
    engine = UndecodableEngine()
    tracer = build(engine=engine)
    job_id = submit(tracer).json()["job_id"]

    tracer.dispatcher.run_pending()

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert (body["state"], body["reason"]) == ("failed", "input_unreadable")
    assert [page["error_code"] for page in body["pages"]] == ["malformed_document"]
    assert engine.calls == []


# --- global capacity: one serialized admission set (contract §6.4/D40) ----------


def admission_entries(store):
    found = store.objects.read(CAPACITY_NAME)
    return {} if found is None else json.loads(found[0])["entries"]


def job_records(objects):
    """`job.json` object names, so a 429 can be shown to have created no job."""
    return [name for name in objects.names() if name.endswith("job.json")]


class RendezvousCapacity(InMemoryObjectStore):
    """Hold the first two capacity reads until both submitters have seen the set."""

    def __init__(self) -> None:
        super().__init__()
        self._barrier = threading.Barrier(2)
        self._reads = 0

    def read(self, name):
        if name == CAPACITY_NAME and self._reads < 2:
            self._reads += 1
            self._barrier.wait(timeout=5)
        return super().read(name)


def test_the_admission_ceiling_answers_a_retryable_429_and_creates_no_job():
    tracer = build(limits=AdmissionLimits(capacity_ceiling=1))
    accepted = submit(tracer, key="capacity-key-1")
    assert accepted.status_code == 202

    rejected = submit(tracer, key="capacity-key-2")

    assert_error(rejected, 429, "capacity_exceeded")
    assert rejected.headers["Retry-After"] == "30"
    assert len(job_records(tracer.store.objects)) == 1
    assert len(admission_entries(tracer.store)) == 1


def test_two_simultaneous_submissions_admit_exactly_one_at_a_ceiling_of_one():
    objects = RendezvousCapacity()
    responses = {}

    def submit_with(key, tracer):
        responses[key] = submit(tracer, key=key)

    threads = [
        threading.Thread(target=submit_with, args=(key, build(objects=objects, limits=limits)))
        for key, limits in (
            ("race-key-1", AdmissionLimits(capacity_ceiling=1)),
            ("race-key-2", AdmissionLimits(capacity_ceiling=1)),
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    codes = sorted(response.status_code for response in responses.values())
    assert codes == [202, 429]
    assert len(job_records(objects)) == 1
    assert len(admission_entries(InMemoryOcrStore(objects))) == 1


def test_a_finalized_job_releases_its_slot_for_the_next_submission():
    tracer = build(limits=AdmissionLimits(capacity_ceiling=1))
    job_id = submit(tracer, key="release-key-1").json()["job_id"]

    tracer.dispatcher.run_pending()

    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200
    assert admission_entries(tracer.store) == {}
    assert submit(tracer, key="release-key-2").status_code == 202


def test_a_failed_job_also_releases_its_slot():
    tracer = build(
        engine=FakeOcrEngine([EnginePageError("malformed_document")]),
        limits=AdmissionLimits(capacity_ceiling=1),
    )
    job_id = submit(tracer, key="failed-key-1").json()["job_id"]

    tracer.dispatcher.run_pending()

    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()["state"] == "failed"
    assert admission_entries(tracer.store) == {}
    assert submit(tracer, key="failed-key-2").status_code == 202


def test_an_abandoned_entry_is_pruned_by_the_next_admission():
    tracer = build(limits=AdmissionLimits(capacity_ceiling=1))
    abandoned = submit(tracer, key="prune-key-1").json()["job_id"]
    # The submitter died after being admitted: its entry self-expires one
    # per-attempt timeout past the job's own deadline (§6.4, D40).
    tracer.clock.advance(JOB_LIFETIME + PER_ATTEMPT_TIMEOUT)

    accepted = submit(tracer, key="prune-key-2")

    assert accepted.status_code == 202
    assert set(admission_entries(tracer.store)) == {accepted.json()["job_id"]}
    assert abandoned not in admission_entries(tracer.store)


def test_a_corrupt_capacity_object_fails_closed_and_admits_nothing():
    tracer = build(limits=AdmissionLimits(capacity_ceiling=1))
    tracer.store.objects.create(CAPACITY_NAME, b"not json at all")

    response = submit(tracer)

    assert_error(response, 429, "capacity_exceeded")
    # Fail closed with no automatic rebuild (§6.4/D40): no job exists, and the object
    # an operator must repair is left exactly as it was found. The claim from §4 step 1
    # stays, so a retry after the repair materializes at the recorded job id.
    assert job_records(tracer.store.objects) == []
    assert tracer.store.objects.read(CAPACITY_NAME)[0] == b"not json at all"


def test_reconciliation_removes_a_leaked_entry_for_a_final_job():
    limits = AdmissionLimits(capacity_ceiling=1)
    tracer = build(limits=limits)
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()
    job = tracer.store.get_job(TEAM_ID, job_id)
    # A release the winner could not write: the entry is back while the job is final.
    tracer.store.acquire_admission(
        job_id,
        deadline_at=job.deadline_at,
        now=tracer.clock.now(),
        ceiling=limits.capacity_ceiling,
        per_attempt_timeout=limits.per_attempt_timeout,
    )
    assert set(admission_entries(tracer.store)) == {job_id}

    reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert admission_entries(tracer.store) == {}


# --- the configured bounds: DEV defaults, overridable, no capacity claim --------

CONFIGURED_ENV = (
    "OCR_MAX_UPLOAD_BYTES",
    "OCR_MAX_PAGES",
    "OCR_MAX_IMAGE_PIXELS",
    "OCR_CAPACITY_CEILING",
    "OCR_TASK_TIMEOUT_S",
)


def test_the_default_bounds_are_the_reviewed_dev_safe_values(monkeypatch):
    # An ambient override (a local run) must not move a default.
    for name in CONFIGURED_ENV:
        monkeypatch.delenv(name, raising=False)

    limits = OcrServiceSettings(project="thaiunion-ai-platform", bucket="b").admission_limits()

    assert limits == AdmissionLimits()
    assert limits.max_upload_bytes == MAX_UPLOAD_BYTES == 20 * 1024 * 1024
    assert limits.max_pages == MAX_PAGES
    assert limits.max_image_pixels == MAX_IMAGE_PIXELS
    assert limits.capacity_ceiling == DEFAULT_CAPACITY_CEILING == 1
    assert limits.per_attempt_timeout == PER_ATTEMPT_TIMEOUT


def test_every_bound_is_configurable_through_the_ocr_environment(monkeypatch):
    monkeypatch.setenv("OCR_MAX_UPLOAD_BYTES", str(1024 * 1024))
    monkeypatch.setenv("OCR_MAX_PAGES", "2")
    monkeypatch.setenv("OCR_MAX_IMAGE_PIXELS", "1000000")
    monkeypatch.setenv("OCR_CAPACITY_CEILING", "4")
    monkeypatch.setenv("OCR_TASK_TIMEOUT_S", "60")

    limits = OcrServiceSettings(project="thaiunion-ai-platform", bucket="b").admission_limits()

    assert limits == AdmissionLimits(
        max_upload_bytes=1024 * 1024,
        max_pages=2,
        max_image_pixels=1_000_000,
        capacity_ceiling=4,
        # An admission entry self-expires one per-attempt timeout past the deadline.
        per_attempt_timeout=timedelta(seconds=60),
    )


@pytest.mark.parametrize(
    "limits",
    [
        AdmissionLimits(max_upload_bytes=0),
        AdmissionLimits(max_upload_bytes=CLOUD_RUN_HTTP1_MAX_REQUEST_BYTES + 1),
        AdmissionLimits(max_pages=0),
        AdmissionLimits(max_image_pixels=0),
        AdmissionLimits(capacity_ceiling=0),
        # ceiling x max_entry_bytes must stay inside the capacity object budget.
        AdmissionLimits(capacity_ceiling=CAPACITY_OBJECT_BUDGET + 1),
    ],
)
def test_a_bound_outside_the_reviewed_range_is_refused(limits):
    with pytest.raises(ValueError):
        assert_limits(limits)


def test_an_invalid_bound_fails_the_composition_at_startup():
    # A misconfigured deployment must not come up admitting unbounded input.
    with pytest.raises(ValueError):
        build(limits=AdmissionLimits(max_pages=0))


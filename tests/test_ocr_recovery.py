"""Recovery and partial-page tests for the OCR v1 slice (issue #8).

Public HTTP lifecycle (seam S1) over a deterministic, fault-injectable engine:
per-page durable checkpoints, bounded retry of a transient page failure, honest
partial/failed finalization, and the create-once checkpoint rule that stops a
stale or duplicate worker from overwriting a page that already succeeded.

Fakes only. Real GCS concurrency, the real engine, and its per-page timeout
remain unverified (docs/ocr-v1-contracts.md, B1/B3).
"""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ocr_reconcile import reconcile
from app.ocr_store import SUCCEEDED, PageResult
from app.ocr_tracer import (
    DEADLINE_PAGE_ERROR,
    JOB_LIFETIME,
    PAGE_RETRIES,
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
from app.ocr_worker import run_worker

TEAM = "team-a"
EMAIL = "analyst-a@example.com"
TOKEN = "tok-a"
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
# A crash stands for anything that kills a worker mid-document: it is not a
# per-page failure and the attempt leaves no final result.
CRASH = RuntimeError("gs://private-bucket/engine-crashed")


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def build(*, objects=None, engine=None, acknowledge=True):
    objects = objects if objects is not None else InMemoryObjectStore()
    store = InMemoryOcrStore(objects)
    engine = engine or FakeOcrEngine([EnginePage(text="page one"), EnginePage(text="page two")])
    clock = FakeClock(START)
    dispatcher = ControllableDispatcher(
        store=store, engine=engine, clock=clock, acknowledge=acknowledge
    )
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            dispatcher=dispatcher,
            clock=clock,
            verifier=FakeTokenVerifier({TOKEN: Principal(email=EMAIL, subject="sub-a")}),
            directory=MappingTeamDirectory({EMAIL: TEAM}),
        ),
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )
    return SimpleNamespace(
        client=client, store=store, objects=objects, engine=engine, clock=clock,
        dispatcher=dispatcher,
    )


def submit(tracer, *, key="recovery-key-0001", content=PDF_BYTES):
    return tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("doc.pdf", content, "application/pdf")},
    )


def run(tracer, job_id, *, owner="executions/exec-1"):
    return run_worker(
        store=tracer.store,
        engine=tracer.engine,
        clock=tracer.clock,
        team_id=TEAM,
        job_id=job_id,
        owner=owner,
    )


def result_body(tracer, job_id):
    response = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert response.status_code == 200, response.text
    return response.json()


def checkpoint_names(tracer, job_id):
    prefix = f"jobs/{TEAM}/{job_id}/pages/"
    return [name for name in tracer.objects.names() if name.startswith(prefix)]


# --- Partial-page results (S1) ----------------------------------------------


def test_a_permanent_page_failure_is_reported_partial_with_number_and_code():
    tracer = build(engine=FakeOcrEngine([
        EnginePage(text="page one"),
        EnginePageError("malformed_document"),
    ]))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0
    body = result_body(tracer, job_id)

    assert (body["state"], body["completeness"], body["reason"]) == (
        "partially_succeeded", "partial", "input_unreadable"
    )
    assert body["accuracy"] == "not_verified"
    assert body["page_count"] == 2
    assert body["text"] == "page one"
    assert [page["page_number"] for page in body["pages"]] == [1, 2]
    assert body["pages"][1] == {
        "page_number": 2,
        "status": "failed",
        "text_source": "ocr",
        "text": None,
        "warnings": [],
        "error_code": "malformed_document",
    }
    # A failed page is never checkpointed, so a later attempt recomputes it (D41).
    assert checkpoint_names(tracer, job_id) == [f"jobs/{TEAM}/{job_id}/pages/00001.json"]


def test_a_transient_page_failure_is_retried_within_the_bound_then_reported():
    tracer = build(engine=FakeOcrEngine([
        EnginePage(text="page one"),
        [EnginePageError("page_engine_error")] * 5,  # never recovers
    ]))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0
    body = result_body(tracer, job_id)

    # Bounded: one retry inside the attempt, then the page is reported failed.
    assert tracer.engine.calls.count(2) == PAGE_RETRIES + 1
    assert body["state"] == "partially_succeeded"
    assert body["reason"] == "engine_unavailable"
    assert body["pages"][1]["error_code"] == "page_engine_error"


def test_a_transient_page_failure_that_recovers_on_the_retry_succeeds():
    tracer = build(engine=FakeOcrEngine([
        EnginePage(text="page one"),
        [EnginePageError("page_engine_error"), EnginePage(text="page two")],
    ]))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0
    body = result_body(tracer, job_id)

    assert body["state"] == "succeeded"
    assert body["completeness"] == "complete"
    assert body["reason"] is None
    assert body["pages"][1]["text"] == "page two"
    assert tracer.engine.calls == [1, 2, 2]


def test_a_permanent_page_failure_is_never_retried():
    tracer = build(engine=FakeOcrEngine([
        EnginePage(text="page one"),
        EnginePageError("malformed_document"),
    ]))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0

    assert tracer.engine.calls == [1, 2]


def test_a_failed_page_never_carries_document_text_or_a_raw_message():
    tracer = build(engine=FakeOcrEngine([
        EnginePage(text="page one"),
        RuntimeError("gs://private-bucket/secret-path leaked text"),
    ]))
    job_id = submit(tracer).json()["job_id"]
    # A whole-document crash is not a page failure: the attempt writes nothing and
    # the safe error boundary is exercised through the API, never the raw text.
    with pytest.raises(RuntimeError):
        run(tracer, job_id)

    assert "gs://" not in tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").text


# --- Crash / restart resumes only missing pages (S1) ------------------------


def test_a_crash_resumes_from_checkpoints_and_recomputes_only_missing_pages():
    objects = InMemoryObjectStore()
    first = build(objects=objects, engine=FakeOcrEngine([
        EnginePage(text="first"),
        EnginePage(text="second"),
        CRASH,
    ]))
    job_id = submit(first).json()["job_id"]

    with pytest.raises(RuntimeError):
        run(first, job_id)

    # Two pages were checkpointed before the crash; the job is not final.
    assert checkpoint_names(first, job_id) == [
        f"jobs/{TEAM}/{job_id}/pages/00001.json",
        f"jobs/{TEAM}/{job_id}/pages/00002.json",
    ]
    assert first.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 202

    restarted = build(objects=objects, engine=FakeOcrEngine([
        EnginePage(text="DIFFERENT"),
        EnginePage(text="DIFFERENT"),
        EnginePage(text="third"),
    ]))

    assert run(restarted, job_id) == 0
    body = result_body(restarted, job_id)

    assert body["state"] == "succeeded"
    assert body["text"] == "first\nsecond\nthird"
    # Only the page without a checkpoint was recognized on the restart.
    assert restarted.engine.calls == [3]


def test_a_stale_or_duplicate_worker_cannot_replace_a_page_that_succeeded():
    tracer = build(engine=FakeOcrEngine([EnginePage(text="live"), EnginePage(text="two")]))
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    # An earlier attempt already checkpointed page 1.
    tracer.store.put_page_checkpoint(
        job,
        PageResult(
            page_number=1, status=SUCCEEDED, text_source="ocr", text="earlier", warnings=[]
        ),
        now=START,
    )

    assert run(tracer, job_id) == 0
    body = result_body(tracer, job_id)

    assert body["pages"][0]["text"] == "earlier"  # the first writer wins (D41)
    assert tracer.engine.calls == [2]  # page 1 is not re-recognized


# --- Deadline behavior (S1) -------------------------------------------------


def test_an_attempt_that_starts_at_or_after_the_deadline_writes_nothing():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.clock.advance(JOB_LIFETIME)
    before = tracer.objects.names()

    assert run(tracer, job_id) == 1

    assert tracer.objects.names() == before
    assert tracer.engine.calls == []
    # Access is denied at the deadline (D39) until reconciliation finalizes.
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 410

    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == [
        f"finalized:deadline_exceeded:{TEAM}/{job_id}"
    ]
    body = result_body(tracer, job_id)
    assert (body["state"], body["reason"]) == ("failed", "deadline_exceeded")


class BlockingPageEngine:
    """Page 1 answers; page 2 blocks until the test releases it, past the deadline."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[int] = []

    def page_count(self, content: bytes, mime_type: str) -> int:
        return 3

    def recognize_page(self, content: bytes, mime_type: str, page_number: int) -> EnginePage:
        self.calls.append(page_number)
        if page_number == 1:
            return EnginePage(text="page one")
        self.entered.set()
        assert self.release.wait(timeout=5)
        return EnginePage(text="abandoned at the deadline")


def test_a_worker_crossing_the_deadline_finalizes_with_the_pages_it_finished():
    engine = BlockingPageEngine()
    tracer = build(engine=engine)
    job_id = submit(tracer).json()["job_id"]

    worker = threading.Thread(target=run, args=(tracer, job_id))
    worker.start()
    try:
        assert engine.entered.wait(timeout=5)
        tracer.clock.advance(JOB_LIFETIME + timedelta(seconds=1))
    finally:
        engine.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()

    body = result_body(tracer, job_id)

    # Never `succeeded` at or after the deadline, and every page is accounted for.
    assert (body["state"], body["reason"], body["completeness"]) == (
        "partially_succeeded", "deadline_exceeded", "partial"
    )
    assert body["page_count"] == 3
    assert [page["status"] for page in body["pages"]] == ["succeeded", "failed", "failed"]
    assert [page["error_code"] for page in body["pages"]] == [
        None,
        DEADLINE_PAGE_ERROR,
        DEADLINE_PAGE_ERROR,
    ]
    assert body["text"] == "page one"

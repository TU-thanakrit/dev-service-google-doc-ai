"""Worker and reconciliation tests for the Cloud Run Jobs slice (issue #6).

The worker entrypoint claims the job's lease, recognizes every page with the fake
engine, and publishes the result the API serves. Reconciliation repairs an
accepted-but-undispatched job, a lost acknowledgment, and a non-retryable launch
failure, idempotently and never with a competing authoritative completion.

Fakes only. Real GCS concurrency, the real Cloud Run Jobs runtime, and the real
`jobs:run` IAM surface remain unverified (docs/ocr-v1-contracts.md, B1/B2).
"""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.ocr_config import OcrServiceSettings
from app.ocr_reconcile import reconcile
from app.ocr_store import DISPATCH_ATTEMPT_CAP, DISPATCH_SPACING, LEASE_TTL
from app.ocr_tracer import (
    JOB_LIFETIME,
    EnginePage,
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
PAGES = (EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="receipt page two"))
REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def build(*, objects=None, acknowledge=True, launch_error=None, engine=None):
    objects = objects if objects is not None else InMemoryObjectStore()
    store = InMemoryOcrStore(objects)
    engine = engine or FakeOcrEngine(PAGES)
    clock = FakeClock(START)
    dispatcher = ControllableDispatcher(
        store=store,
        engine=engine,
        clock=clock,
        acknowledge=acknowledge,
        launch_error=launch_error,
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
        client=client,
        store=store,
        objects=objects,
        engine=engine,
        clock=clock,
        dispatcher=dispatcher,
    )


def submit(tracer, *, key="retry-key-0001", content=PDF_BYTES):
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


def job_objects(tracer, job_id):
    return [
        name
        for name in tracer.objects.names()
        if name.startswith(f"jobs/{TEAM}/{job_id}/")
    ]


# --- Worker entrypoint -------------------------------------------------------


def test_the_worker_claims_the_job_and_publishes_a_result_the_api_serves():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0

    result = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.json()["text"] == "ใบเสร็จรับเงิน\nreceipt page two"
    # The worker released its lease, and the job it ran is genuinely complete.
    job = tracer.store.get_job(TEAM, job_id)
    assert tracer.store.read_lease(job) is None
    assert job.finalized_at is not None


def test_a_duplicate_launch_finds_a_live_lease_and_writes_nothing():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    tracer.store.acquire_lease(job, owner="executions/exec-1", now=START)

    assert run(tracer, job_id, owner="executions/exec-2") == 1

    assert tracer.store.get_job(TEAM, job_id).state == "accepted"
    assert tracer.engine.calls == []
    assert job_objects(tracer, job_id) == [
        f"jobs/{TEAM}/{job_id}/claim.json",
        f"jobs/{TEAM}/{job_id}/dispatch.json",
        f"jobs/{TEAM}/{job_id}/job.json",
    ]


def test_the_worker_finishes_a_job_after_an_api_restart():
    objects = InMemoryObjectStore()
    first = build(objects=objects)
    job_id = submit(first).json()["job_id"]

    restarted = build(objects=objects)
    assert restarted.client.get(f"/v1/ocr/jobs/{job_id}").json()["state"] == "accepted"

    assert run(restarted, job_id) == 0

    result = restarted.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.json()["state"] == "succeeded"


def test_the_worker_writes_nothing_for_an_unknown_or_already_final_job():
    tracer = build()
    # Nothing to resume: writes nothing and exits non-zero (§5.5, D30).
    assert run(tracer, "6d1f9b2a-0000-4000-8000-000000000000") == 1
    assert tracer.objects.names() == []

    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()
    before = tracer.objects.names()

    # Already final: a straggler writes nothing and exits non-zero.
    assert run(tracer, job_id) == 1
    assert tracer.objects.names() == before
    assert len(tracer.engine.calls) == 1


# --- Reconciliation ----------------------------------------------------------


def test_reconcile_dispatches_an_accepted_job_whose_intent_was_lost():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    objects = tracer.objects
    name = f"jobs/{TEAM}/{job_id}/dispatch.json"
    _, generation = objects.read(name)
    objects.delete(name, generation)
    tracer.dispatcher.pending.clear()

    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"dispatched:{TEAM}/{job_id}"]
    assert tracer.dispatcher.pending == [(TEAM, job_id)]
    assert tracer.store.get_job(TEAM, job_id).attempts == 1


def test_a_lost_acknowledgment_is_redispatched_once_the_spacing_elapses():
    tracer = build(acknowledge=False)
    job_id = submit(tracer).json()["job_id"]
    assert tracer.store.get_job(TEAM, job_id).acknowledged is False
    tracer.dispatcher.pending.clear()

    # Too soon: the least spacing has not elapsed since the first attempt.
    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == []
    assert tracer.dispatcher.pending == []

    tracer.clock.advance(DISPATCH_SPACING)
    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"redispatched:{TEAM}/{job_id}"]
    assert tracer.dispatcher.pending == [(TEAM, job_id)]
    assert tracer.store.get_job(TEAM, job_id).attempts == 2


def test_reconcile_does_not_redispatch_while_a_lease_is_live():
    tracer = build(acknowledge=False)
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.pending.clear()
    tracer.clock.advance(DISPATCH_SPACING)
    # A live holder, claimed after the spacing elapsed: the re-dispatch is due but
    # must yield to the worker that is already processing the job.
    job = tracer.store.get_job(TEAM, job_id)
    tracer.store.acquire_lease(job, owner="executions/exec-1", now=tracer.clock.now())

    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == []
    assert tracer.dispatcher.pending == []


def test_a_non_retryable_launch_failure_is_finalized_once_by_reconciliation():
    tracer = build(launch_error="engine_unavailable")
    job_id = submit(tracer).json()["job_id"]
    view = tracer.store.get_job(TEAM, job_id)
    assert (view.state, view.acknowledged, view.last_error_code) == (
        "accepted",
        False,
        "engine_unavailable",
    )
    tracer.dispatcher.pending.clear()

    # Too soon for a second attempt.
    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == []
    tracer.clock.advance(DISPATCH_SPACING)
    # The second attempt is claimed and fails the same non-retryable way.
    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == [
        f"redispatched:{TEAM}/{job_id}"
    ]
    assert tracer.store.get_job(TEAM, job_id).attempts == DISPATCH_ATTEMPT_CAP
    # Attempts exhausted with a non-retryable error: finalize, never a retry loop.
    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"finalized:engine_unavailable:{TEAM}/{job_id}"]
    result = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.json()["state"] == "failed"
    assert result.json()["reason"] == "engine_unavailable"
    assert tracer.engine.calls == []

    # A further pass changes nothing: `final.json` is a single arbiter.
    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == []
    assert [
        name for name in tracer.objects.names() if name.endswith("final.json")
    ] == [f"jobs/{TEAM}/{job_id}/final.json"]


def test_reconcile_finalizes_an_unfinished_job_at_its_deadline():
    tracer = build(acknowledge=False)
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.pending.clear()
    tracer.clock.advance(JOB_LIFETIME)

    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"finalized:deadline_exceeded:{TEAM}/{job_id}"]
    result = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert (result["state"], result["reason"]) == ("failed", "deadline_exceeded")
    # No dispatch and no recognition at or after the deadline (§5.5).
    assert tracer.dispatcher.pending == []
    assert tracer.engine.calls == []


def test_reconcile_leaves_a_finished_job_alone():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()
    finalized = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()["timestamps"]

    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == []

    after = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()["timestamps"]
    assert after == finalized
    assert len(tracer.engine.calls) == 1


def test_a_stale_lease_holder_does_not_block_the_next_execution_forever():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    # A holder that died without releasing: its lease expires and is taken over.
    tracer.store.acquire_lease(job, owner="executions/dead", now=START)
    tracer.clock.advance(LEASE_TTL + timedelta(seconds=1))

    assert run(tracer, job_id) == 0
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200


def test_fake_worker_command_runs_without_cloud_clients():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ocr_worker_demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "worker demo ok" in result.stdout


def test_ocr_settings_read_environment_with_the_ocr_prefix(monkeypatch):
    monkeypatch.setenv("OCR_PROJECT", "proj-1")
    monkeypatch.setenv("OCR_BUCKET", "dev-ocr-service-x")
    monkeypatch.setenv("OCR_WORKER_JOB_NAME", "dev-ocr-service-worker-1")
    monkeypatch.setenv("OCR_TEAMS", '{"analyst@example.com": "team-a"}')

    settings = OcrServiceSettings()

    assert (settings.project, settings.bucket) == ("proj-1", "dev-ocr-service-x")
    assert settings.region == "asia-southeast1"
    assert settings.teams == {"analyst@example.com": "team-a"}
    assert settings.worker_container_name == "worker"

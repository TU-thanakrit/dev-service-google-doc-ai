"""Worker and reconciliation tests for the Cloud Run Jobs slice (issue #6).

The worker entrypoint claims the job's lease, recognizes every page with the fake
engine, and publishes the result the API serves. Reconciliation repairs an
accepted-but-undispatched job, a lost acknowledgment, and a non-retryable launch
failure, idempotently and never with a competing authoritative completion.

Fakes only. Real GCS concurrency, the real Cloud Run Jobs runtime, and the real
`jobs:run` IAM surface remain unverified (docs/ocr-v1-contracts.md, B1/B2).
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ocr_config import OcrServiceSettings
from app.ocr_engine import EngineRegistry
from app.ocr_pinning import WorkerPin
from app.ocr_reconcile import reconcile
from app.ocr_store import DISPATCH_ATTEMPT_CAP, DISPATCH_SPACING, LEASE_TTL
from app.ocr_tracer import (
    JOB_LIFETIME,
    EnginePage,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
    run_job,
)
from app.ocr_tracer_fakes import (
    FAKE_ENGINE_REF,
    ControllableDispatcher,
    FakeOcrEngine,
    FakeTokenVerifier,
    InMemoryObjectStore,
    InMemoryOcrStore,
    fake_engine_registry,
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
            directory=MappingTeamDirectory({f"{EMAIL}|sub-a": TEAM}),
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
        engines=fake_engine_registry(tracer.engine),
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
    assert len(tracer.engine.calls) == len(PAGES)


# --- The engine the job was pinned to (§8, D16) ------------------------------

NEW_ENGINE_REF = "fake-ocr-engine:v2"
NEW_DIGEST = "sha256:" + "9" * 64


def newer_engine(*texts):
    """A second, distinguishable engine revision: different ref, different text."""
    return FakeOcrEngine([EnginePage(text=text) for text in texts], engine_ref=NEW_ENGINE_REF)


def work_with(tracer, job_id, engines, *, owner="executions/exec-2"):
    return run_worker(
        store=tracer.store,
        engines=engines,
        clock=tracer.clock,
        team_id=TEAM,
        job_id=job_id,
        owner=owner,
    )


def result_text(tracer, job_id):
    return tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()["text"]


def test_the_worker_runs_the_engine_the_accepted_job_was_pinned_to():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    old = tracer.engine
    new = newer_engine("new engine page one", "new engine page two")
    # The operator has moved the deployment to the new revision. The accepted job
    # resolves through its own recorded ref, so the old engine runs it (§8).
    registry = EngineRegistry({FAKE_ENGINE_REF: lambda: old, NEW_ENGINE_REF: lambda: new})

    assert work_with(tracer, job_id, registry) == 0

    assert result_text(tracer, job_id) == "ใบเสร็จรับเงิน\nreceipt page two"
    assert old.calls == [1, 2]
    assert new.calls == []


def test_a_job_accepted_after_the_operator_change_runs_the_new_engine():
    tracer = build()
    # The operator's current pin is now the new revision, so the next accepted job
    # records it - while the job above keeps the old one.
    tracer.dispatcher.pin = WorkerPin.build(
        engine_ref=NEW_ENGINE_REF, image=NEW_DIGEST, container_name="worker", task_timeout_s=1800
    )
    job_id = submit(tracer, key="new-revision-key").json()["job_id"]
    new = newer_engine("new engine page one", "new engine page two")
    registry = EngineRegistry({FAKE_ENGINE_REF: lambda: tracer.engine, NEW_ENGINE_REF: lambda: new})

    assert work_with(tracer, job_id, registry) == 0

    assert result_text(tracer, job_id) == "new engine page one\nnew engine page two"
    assert tracer.engine.calls == []


def test_the_worker_refuses_a_pin_it_cannot_resolve_without_writing_anything():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    before = job_objects(tracer, job_id)

    # No fallback to the engine this deployment happens to be configured with.
    unresolved = EngineRegistry({NEW_ENGINE_REF: lambda: newer_engine("x")})

    assert work_with(tracer, job_id, unresolved) == 1

    assert job_objects(tracer, job_id) == before
    assert tracer.engine.calls == []
    assert tracer.store.get_job(TEAM, job_id).state == "accepted"


def test_the_worker_refuses_an_engine_that_declares_another_revision():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    before = job_objects(tracer, job_id)
    # Registered under the job's ref but declaring another one: the resolved
    # engine/config disagreement §5.5/D16 refuses, not a warning.
    registry = EngineRegistry({FAKE_ENGINE_REF: lambda: newer_engine("other")})

    assert work_with(tracer, job_id, registry) == 1

    assert job_objects(tracer, job_id) == before
    assert tracer.store.get_job(TEAM, job_id).state == "accepted"


def test_a_checkpoint_from_another_engine_is_rejected_and_recomputed():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    # A page checkpoint written by a superseded engine sits at the job's page name:
    # it is not a checkpoint for this pin, so the page is recomputed (§5.5, D41).
    tracer.objects.create(
        f"jobs/{TEAM}/{job_id}/pages/00001.json",
        json.dumps(
            {
                "schema_version": "ocr.v1",
                "job_id": job_id,
                "page_number": 1,
                "status": "succeeded",
                "text_source": "ocr",
                "text": "stale engine text",
                "warnings": [],
                "engine_ref": NEW_ENGINE_REF,
            }
        ).encode(),
    )

    assert run(tracer, job_id) == 0

    assert result_text(tracer, job_id) == "ใบเสร็จรับเงิน\nreceipt page two"
    assert tracer.engine.calls == [1, 2]


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
    assert len(tracer.engine.calls) == len(PAGES)


def test_a_stale_lease_holder_does_not_block_the_next_execution_forever():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    # A holder that died without releasing: its lease expires and is taken over.
    tracer.store.acquire_lease(job, owner="executions/dead", now=START)
    tracer.clock.advance(LEASE_TTL + timedelta(seconds=1))

    assert run(tracer, job_id) == 0
    assert tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").status_code == 200


# --- Fenced publication and lease renewal (review: protocol finding) -----------


def work_under(tracer, job_id, lease):
    return run_job(
        store=tracer.store,
        engine=tracer.engine,
        clock=tracer.clock,
        team_id=TEAM,
        job_id=job_id,
        lease=lease,
    )


def final_document(tracer, job_id):
    return json.loads(tracer.objects.read(f"jobs/{TEAM}/{job_id}/final.json")[0])


def test_each_execution_publishes_a_result_named_for_its_lease_fence():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    lease = tracer.store.acquire_lease(job, owner="executions/exec-1", now=START)

    assert work_under(tracer, job_id, lease) is True

    final = final_document(tracer, job_id)
    # The published object is this execution's fenced result, never a shared
    # `result.0.json` another execution could have written first.
    assert final["fence"] == lease.fence == 1
    assert final["result_object"] == f"jobs/{TEAM}/{job_id}/result.1.json"


def test_a_superseded_execution_writes_nothing_after_a_takeover():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    stale = tracer.store.acquire_lease(job, owner="executions/exec-1", now=START)
    tracer.clock.advance(LEASE_TTL + timedelta(seconds=1))
    fresh = tracer.store.acquire_lease(job, owner="executions/exec-2", now=tracer.clock.now())
    assert fresh.fence == stale.fence + 1
    before = job_objects(tracer, job_id)

    # The superseded execution resumes after the takeover: it must write nothing,
    # not even a page, and it must not publish or overwrite a result.
    assert work_under(tracer, job_id, stale) is False

    assert job_objects(tracer, job_id) == before
    assert tracer.engine.calls == []
    assert tracer.objects.read(f"jobs/{TEAM}/{job_id}/final.json") is None
    assert tracer.objects.read(f"jobs/{TEAM}/{job_id}/result.{stale.fence}.json") is None

    # The live execution still finishes the job, under its own fence.
    assert work_under(tracer, job_id, fresh) is True
    final = final_document(tracer, job_id)
    assert (final["fence"], final["result_object"].rsplit("/", 1)[-1]) == (
        fresh.fence,
        f"result.{fresh.fence}.json",
    )


def test_page_calls_are_bounded_by_the_deadline_and_the_lease_window():
    engine = BudgetRecordingEngine(PAGES)
    tracer = build(engine=engine)
    job_id = submit(tracer).json()["job_id"]
    # One minute of job lifetime left; the lease window is the five-minute TTL,
    # so the deadline is the tighter bound and must be what the page call gets.
    tracer.clock.advance(JOB_LIFETIME - timedelta(seconds=60))

    assert run(tracer, job_id) == 0

    assert engine.timeouts, "the worker must pass a bound with every page call"
    assert all(0 < timeout <= 60.0 for timeout in engine.timeouts)


class BudgetRecordingEngine(FakeOcrEngine):
    """The fake engine, recording the client-side bound each call was given."""

    def __init__(self, pages) -> None:
        super().__init__(pages)
        self.timeouts: list[float | None] = []

    def page_count(self, content, mime_type, *, timeout_s=None):
        self.timeouts.append(timeout_s)
        return super().page_count(content, mime_type)

    def recognize_page(self, content, mime_type, page_number, *, timeout_s=None):
        self.timeouts.append(timeout_s)
        return super().recognize_page(content, mime_type, page_number)


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
    monkeypatch.setenv("OCR_TEAMS", '{"analyst@example.com|sub-a": "team-a"}')

    settings = OcrServiceSettings()

    assert (settings.project, settings.bucket) == ("proj-1", "dev-ocr-service-x")
    assert settings.bucket_project == "proj-1"
    assert settings.region == "asia-southeast1"
    # The allowlist binds the verified email *and* its subject (§7.3).
    assert settings.teams == {"analyst@example.com|sub-a": "team-a"}
    assert settings.worker_container_name == "worker"


def test_a_malformed_team_allowlist_fails_at_startup(monkeypatch):
    # Strict parsing: a binding that does not carry both verified claims is a
    # deployment fault, not an entry that silently authorizes nobody (or the
    # wrong identity), so it fails where the settings are read.
    monkeypatch.setenv("OCR_PROJECT", "proj-1")
    monkeypatch.setenv("OCR_BUCKET", "dev-ocr-service-x")
    malformed = (
        '{"analyst@example.com": "team-a"}',       # email alone: no subject
        '{"analyst@example.com|": "team-a"}',      # empty subject
        '{"|sub-a": "team-a"}',                    # empty email
        '{"analyst@example.com|sub-a": ""}',       # empty team id
        '{"analyst@example.com|sub-a|x": "t"}',    # a third field
    )

    for value in malformed:
        monkeypatch.setenv("OCR_TEAMS", value)
        with pytest.raises(ValidationError):
            OcrServiceSettings()

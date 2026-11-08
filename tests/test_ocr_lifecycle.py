"""Deadline, expiry, and cleanup lifecycle tests for the OCR v1 slice (issue #9).

Seam S1 through the HTTP API (the expiry gate, prompt input deletion, the
post-cleanup ``404``) and seam S2 through the durable store and the cleanup pass
(the late-write fences, checkpoint credit at the deadline, the conditional purge
order).

Fakes only. Real GCS soft-delete/versioning behaviour, the deployed cleanup job,
and its scheduler alerting remain unverified (``docs/ocr-v1-contracts.md``,
B1/B4), so nothing here is evidence about a deployed service.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ocr_cleanup import (
    CLEANUP_SCHEDULE_PERIOD,
    CLEANUP_TARGET,
    PER_ATTEMPT_TIMEOUT,
    assert_cleanup_invariants,
    cleanup,
    cleanup_guard,
)
from app.ocr_reconcile import reconcile
from app.ocr_store import SUCCEEDED, JobRecord, PageResult, PreconditionFailed
from app.ocr_tracer import (
    JOB_LIFETIME,
    RETENTION,
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
    fake_engine_registry,
)
from app.ocr_worker import run_worker

TEAM = "team-a"
EMAIL = "analyst-a@example.com"
TOKEN = "tok-a"
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PAGES = (EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="receipt page two"))
# A crash stands for anything that kills a worker mid-document, after its earlier
# pages were durably checkpointed.
CRASH = RuntimeError("gs://private-bucket/engine-crashed")


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def build(*, objects=None, engine=None, store=None):
    store = store or InMemoryOcrStore(objects)
    engine = engine or FakeOcrEngine(PAGES)
    clock = FakeClock(START)
    dispatcher = ControllableDispatcher(store=store, engine=engine, clock=clock)
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
        client=client, store=store, objects=store.objects, engine=engine, clock=clock,
        dispatcher=dispatcher,
    )


def submit(tracer, *, key="lifecycle-key-0001", content=PDF_BYTES):
    return tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("doc.pdf", content, "application/pdf")},
    )


def work(tracer, job_id, *, owner="executions/exec-1"):
    return run_worker(
        store=tracer.store,
        engines=fake_engine_registry(tracer.engine),
        clock=tracer.clock,
        team_id=TEAM,
        job_id=job_id,
        owner=owner,
    )


def result_body(tracer, job_id):
    response = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert response.status_code == 200, response.text
    return response.json()


def job_object_names(tracer, job_id):
    return [name for name in tracer.objects.names() if name.startswith(f"jobs/{TEAM}/{job_id}/")]


def checkpoint_names(tracer, job_id):
    prefix = f"jobs/{TEAM}/{job_id}/pages/"
    return [name for name in tracer.objects.names() if name.startswith(prefix)]


def stored_job_state(tracer, job_id):
    """The raw `job.json` state, which no finalization projects into (D39)."""
    raw, _ = tracer.objects.read(f"jobs/{TEAM}/{job_id}/job.json")
    return json.loads(raw)["state"]


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    body = response.json()
    assert body["schema_version"] == "ocr.v1"
    assert body["error"]["code"] == code
    # Fixed allowlisted message: no path, bucket, job, or token.
    assert "/" not in body["error"]["message"]


def remove_object(tracer, name):
    found = tracer.objects.read(name)
    assert found is not None, name
    tracer.objects.delete(name, found[1])


def page(number, *, text="recognized text"):
    return PageResult(page_number=number, status=SUCCEEDED, text_source="ocr", text=text,
                      warnings=[])


# --- Late-write fences (S2, contract §5.5/D42) -------------------------------


def test_a_page_checkpoint_at_or_after_the_deadline_is_refused():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    tracer.clock.advance(JOB_LIFETIME)

    tracer.store.put_page_checkpoint(job, page(1), now=tracer.clock.now())

    assert checkpoint_names(tracer, job_id) == []


def test_a_finalized_job_cannot_reappear_as_running():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.clock.advance(JOB_LIFETIME)
    reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    job = tracer.store.get_job(TEAM, job_id)
    assert job.state == "failed"
    tracer.store.mark_running(job, now=tracer.clock.now())

    assert stored_job_state(tracer, job_id) == "accepted"


# --- The expiry gate denies before content is read (S1, contract §3.3/D39) ----


def test_access_is_denied_at_expiry_even_when_content_is_already_deleted():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()
    # Cleanup deletes content before records (D37), so a reader arriving mid-pass
    # must be denied by the gate rather than fail on the vanished result object.
    # A full success already deleted its input; the result object is what a
    # cleanup pass removes next.
    assert tracer.objects.read(input_name(job_id)) is None
    remove_object(tracer, f"jobs/{TEAM}/{job_id}/result.0.json")
    for name in (f"jobs/{TEAM}/{job_id}/job.json", f"jobs/{TEAM}/{job_id}/final.json"):
        assert tracer.objects.read(name) is not None
    tracer.clock.advance(RETENTION)

    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}"), 410, "job_expired")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}/result"), 410, "job_expired")


def test_a_missing_result_inside_the_retention_window_fails_closed():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.dispatcher.run_pending()
    remove_object(tracer, f"jobs/{TEAM}/{job_id}/result.0.json")

    # The gate serves (now < expires_at), so the absent result is corruption, not
    # expiry: 500 with an alert, never a served empty result (§2.2, §5.4).
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}/result"), 500, "internal_error")


# --- Prompt input deletion on full success (S1, contract §6.3, §10) -----------


def input_name(job_id):
    return f"inputs/{TEAM}/{job_id}"


def test_a_full_success_deletes_the_input_immediately():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    assert tracer.objects.read(input_name(job_id)) is not None

    assert work(tracer, job_id) == 0

    assert result_body(tracer, job_id)["state"] == "succeeded"
    assert tracer.objects.read(input_name(job_id)) is None


def test_a_partial_result_keeps_the_input_until_cleanup():
    tracer = build(engine=FakeOcrEngine([EnginePage(text="page one"),
                                         EnginePageError("malformed_document")]))
    job_id = submit(tracer).json()["job_id"]

    assert work(tracer, job_id) == 0

    assert result_body(tracer, job_id)["state"] == "partially_succeeded"
    assert tracer.objects.read(input_name(job_id)) is not None


def test_a_retry_after_success_does_not_reupload_the_deleted_input():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)

    retried = submit(tracer)

    assert retried.status_code == 200
    assert retried.json()["job_id"] == job_id
    # The 200 path writes nothing, so retention is not extended by the retry (§4).
    assert tracer.objects.read(input_name(job_id)) is None


def test_reconciliation_deletes_a_successful_input_the_worker_left_behind():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    # The worker died before its own deletion, so the input is still there while
    # the published state is a full success (§6.2/§6.3).
    job = tracer.store.get_job(TEAM, job_id)
    tracer.store.put_input(job, PDF_BYTES)

    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"input_deleted:{TEAM}/{job_id}"]
    assert tracer.objects.read(input_name(job_id)) is None
    assert reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock) == []


# --- Deadline finalization credits durable pages (S1, contract §6.2, D42) -----


def test_the_reconciler_finalizes_the_deadline_with_the_durable_checkpoints():
    tracer = build(engine=FakeOcrEngine([EnginePage(text="page one"),
                                         EnginePage(text="page two"), CRASH]))
    job_id = submit(tracer).json()["job_id"]
    with pytest.raises(RuntimeError):
        work(tracer, job_id)
    assert checkpoint_names(tracer, job_id) == [
        f"jobs/{TEAM}/{job_id}/pages/00001.json",
        f"jobs/{TEAM}/{job_id}/pages/00002.json",
    ]
    tracer.clock.advance(JOB_LIFETIME)

    actions = reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    assert actions == [f"finalized:deadline_exceeded:{TEAM}/{job_id}"]
    body = result_body(tracer, job_id)
    # Usable pages survive the deadline and the job is partial, never complete.
    assert (body["state"], body["reason"], body["completeness"]) == (
        "partially_succeeded", "deadline_exceeded", "partial"
    )
    assert body["text"] == "page one\npage two"
    assert [entry["text"] for entry in body["pages"]] == ["page one", "page two"]


def test_a_job_with_every_page_checkpointed_is_still_not_succeeded_at_the_deadline():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    for number in (1, 2):
        tracer.store.put_page_checkpoint(job, page(number), now=tracer.clock.now())
    tracer.clock.advance(JOB_LIFETIME)

    reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)

    body = result_body(tracer, job_id)
    # D42: no finalize at or after the deadline can be `succeeded`, even when every
    # page it accounted for did succeed.
    assert (body["state"], body["reason"]) == ("partially_succeeded", "deadline_exceeded")


# --- The cleanup guard and target (S2, contract §6.3, D32) --------------------


class RecordingDeletes(InMemoryObjectStore):
    """Records the delete order and the generation every delete carried."""

    def __init__(self):
        super().__init__()
        self.deleted: list[tuple[str, int]] = []

    def delete(self, name: str, generation: int) -> None:
        self.deleted.append((name, generation))
        super().delete(name, generation)


class RefusingFirstJobRecordDelete(InMemoryObjectStore):
    """Fails the first `job.json` delete, the way a lost race would."""

    def __init__(self):
        super().__init__()
        self.refusals = 0

    def delete(self, name: str, generation: int) -> None:
        if name.endswith("/job.json") and not self.refusals:
            self.refusals += 1
            raise PreconditionFailed(name)
        super().delete(name, generation)


def final_record(finalized_at, *, accepted_at=START):
    """A job finalized at `finalized_at`, for the guard arithmetic."""
    job = JobRecord(
        job_id="job-guard",
        team_id=TEAM,
        accepted_at=accepted_at,
        deadline_at=accepted_at + JOB_LIFETIME,
        content_hash="hash",
        byte_length=1,
        key_hash="key-hash",
        mime_type="application/pdf",
        engine_ref="engine-v1",
        worker_revision_digest="rev-1",
    )
    job.finalized_at = finalized_at
    job.expires_at = finalized_at + RETENTION
    return job


def test_the_guard_is_the_later_of_expiry_and_the_attempt_bound():
    # The worst case is a job finalized the instant it was accepted (§6.3): its
    # retention ends before the last page write of an attempt could land.
    early = final_record(START)
    assert early.expires_at is not None
    assert cleanup_guard(early) == early.deadline_at + PER_ATTEMPT_TIMEOUT
    assert cleanup_guard(early) - early.expires_at <= CLEANUP_TARGET

    # A job that ran for a day is guarded by its own expiry, not the attempt bound.
    late = final_record(START + JOB_LIFETIME - timedelta(minutes=1))
    assert late.expires_at is not None
    assert cleanup_guard(late) == late.expires_at
    assert cleanup_guard(late) - late.expires_at == timedelta(0)


def test_a_job_accepted_under_a_longer_pinned_attempt_bound_is_guarded_by_it():
    # D43: the launch sends the job's own pinned `task_timeout_s` as its override,
    # so a job accepted under a longer bound must be guarded by that bound, not by
    # whatever the deployment is currently configured with - otherwise the last
    # page write of its attempt could land after the pass started (§6.3).
    pinned = final_record(START)
    pinned.task_timeout_s = 3600
    unpinned = final_record(START)

    assert unpinned.task_timeout_s == 0
    assert cleanup_guard(pinned, per_attempt_timeout=timedelta(minutes=30)) == (
        pinned.deadline_at + timedelta(hours=1)
    )
    assert cleanup_guard(unpinned, per_attempt_timeout=timedelta(minutes=30)) == (
        unpinned.deadline_at + timedelta(minutes=30)
    )


def test_the_cleanup_schedule_invariant_is_asserted():
    # D32: per_attempt_timeout + cleanup_schedule_period <= 1 h keeps the target
    # reachable for the earliest legal pass.
    assert_cleanup_invariants(
        per_attempt_timeout=PER_ATTEMPT_TIMEOUT, schedule_period=CLEANUP_SCHEDULE_PERIOD
    )

    with pytest.raises(ValueError):
        assert_cleanup_invariants(
            per_attempt_timeout=PER_ATTEMPT_TIMEOUT,
            schedule_period=CLEANUP_TARGET - PER_ATTEMPT_TIMEOUT + timedelta(seconds=1),
        )


def test_a_pass_refuses_to_delete_before_the_guard():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    before = tracer.objects.names()
    job = tracer.store.get_job(TEAM, job_id)
    guard = cleanup_guard(job)
    tracer.clock.advance(guard - tracer.clock.now() - timedelta(seconds=1))

    assert cleanup(store=tracer.store, clock=tracer.clock) == []

    assert tracer.objects.names() == before


def test_a_pass_at_the_guard_deletes_every_object_and_is_repeatable():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    guard = cleanup_guard(tracer.store.get_job(TEAM, job_id))
    tracer.clock.advance(guard - tracer.clock.now())

    assert cleanup(store=tracer.store, clock=tracer.clock) == [f"cleaned:{TEAM}/{job_id}"]

    assert [name for name in tracer.objects.names() if job_id in name] == []
    assert [name for name in tracer.objects.names() if name.startswith("idem/")] == []
    # Access was already denied at expiry; after cleanup the records are gone too.
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}"), 404, "not_found")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{job_id}/result"), 404, "not_found")
    # Idempotent: a second pass over the same clock finds nothing left to do.
    assert cleanup(store=tracer.store, clock=tracer.clock) == []


def test_a_pass_deletes_content_before_records_at_real_generations():
    objects = RecordingDeletes()
    tracer = build(objects=objects, engine=FakeOcrEngine([EnginePage(text="page one"),
                                                         EnginePageError("malformed_document")]))
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)  # partial, so the input and its checkpoint are still there
    # The worker released its own lease on the way out; record the pass alone.
    objects.deleted.clear()
    guard = cleanup_guard(tracer.store.get_job(TEAM, job_id))
    tracer.clock.advance(guard - tracer.clock.now())

    assert cleanup(store=tracer.store, clock=tracer.clock) == [f"cleaned:{TEAM}/{job_id}"]

    key_hash = hashlib.sha256(f"{TEAM}\nlifecycle-key-0001".encode()).hexdigest()
    assert [name for name, _ in objects.deleted] == [
        f"inputs/{TEAM}/{job_id}",
        f"jobs/{TEAM}/{job_id}/pages/00001.json",
        f"jobs/{TEAM}/{job_id}/result.0.json",
        f"jobs/{TEAM}/{job_id}/dispatch.json",
        f"jobs/{TEAM}/{job_id}/final.json",
        f"jobs/{TEAM}/{job_id}/job.json",
        f"idem/{TEAM}/{key_hash}",
    ]
    # Every delete is conditional: none uses the create-only 0 precondition.
    assert all(generation > 0 for _, generation in objects.deleted)


def test_a_pass_removes_a_lease_a_crashed_holder_left_behind():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    job = tracer.store.get_job(TEAM, job_id)
    tracer.store.acquire_lease(job, owner="executions/dead", now=tracer.clock.now())
    # An attempt that never ran still holds a job the guard must protect until the
    # last page write of a late attempt could have landed.
    tracer.clock.advance(cleanup_guard(job) - tracer.clock.now())

    assert cleanup(store=tracer.store, clock=tracer.clock) == [f"cleaned:{TEAM}/{job_id}"]

    assert [name for name in tracer.objects.names() if job_id in name] == []


def test_a_deletion_past_the_target_is_signalled_overdue():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    # Nothing ran the cleanup job inside the target: data outlived its expiry.
    tracer.clock.advance(RETENTION + CLEANUP_TARGET)

    actions = cleanup(store=tracer.store, clock=tracer.clock)

    assert actions[0] == f"overdue:{TEAM}/{job_id}"
    assert f"cleaned:{TEAM}/{job_id}" in actions
    assert [name for name in tracer.objects.names() if job_id in name] == []


def test_a_partial_pass_is_completed_by_the_next_one():
    tracer = build(objects=RefusingFirstJobRecordDelete())
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    guard = cleanup_guard(tracer.store.get_job(TEAM, job_id))
    tracer.clock.advance(guard - tracer.clock.now())

    first = cleanup(store=tracer.store, clock=tracer.clock)

    assert first == [f"cleanup_incomplete:{TEAM}/{job_id}"]
    # Content first: only the record the lost race protected is left.
    assert [name for name in tracer.objects.names() if job_id in name] == [
        f"jobs/{TEAM}/{job_id}/job.json"
    ]

    tracer.clock.advance(CLEANUP_TARGET)
    second = cleanup(store=tracer.store, clock=tracer.clock)

    # `final.json` is already gone, so this pass is guarded by `deadline_at` alone
    # (§3.3/D39) and finishes the job rather than reporting it overdue.
    assert second == [f"cleaned:{TEAM}/{job_id}"]
    assert [name for name in tracer.objects.names() if job_id in name] == []


def test_a_pass_fails_closed_on_an_unreadable_final_record():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    final_name = f"jobs/{TEAM}/{job_id}/final.json"
    found = tracer.objects.read(final_name)
    tracer.objects.replace(final_name, b"{not json", found[1])
    tracer.clock.advance(RETENTION + CLEANUP_TARGET)
    before = tracer.objects.names()

    actions = cleanup(store=tracer.store, clock=tracer.clock)

    # An undecidable expiry is never deleted on: skipped, alerted, re-decided.
    assert actions == [f"skipped_unreadable:{TEAM}/{job_id}"]
    assert tracer.objects.names() == before


def test_a_pass_never_deletes_a_newer_record_for_a_reused_key():
    tracer = build()
    first = submit(tracer).json()["job_id"]
    work(tracer, first)
    # Beyond expiry the key no longer guarantees deduplication, so the same key
    # and bytes are accepted again and the record is replaced by CAS (§3.4).
    tracer.clock.advance(RETENTION + CLEANUP_TARGET)
    second = submit(tracer).json()["job_id"]
    assert second != first

    actions = cleanup(store=tracer.store, clock=tracer.clock)

    assert f"cleaned:{TEAM}/{first}" in actions
    key_hash = hashlib.sha256(f"{TEAM}\nlifecycle-key-0001".encode()).hexdigest()
    record, _ = tracer.store.claim(TEAM, key_hash)
    assert record.job_id == second
    assert tracer.objects.read(f"jobs/{TEAM}/{second}/job.json") is not None


def test_an_expired_job_is_denied_before_any_cleanup_runs():
    tracer = build()
    first = submit(tracer).json()["job_id"]
    work(tracer, first)
    tracer.clock.advance(RETENTION)
    before = tracer.objects.names()

    # Access ends at `expires_at`, cleanup or not (§3.3), and a same-key retry
    # after that point starts a new job instead of returning the expired one.
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{first}"), 410, "job_expired")
    assert_error(tracer.client.get(f"/v1/ocr/jobs/{first}/result"), 410, "job_expired")
    second = submit(tracer)

    assert second.status_code == 202
    assert second.json()["job_id"] != first
    # No pass has run — the guard is a further half hour away — yet every object of
    # the expired job is still on disk and none of it is reachable.
    assert [name for name in before if tracer.objects.read(name) is None] == []
    assert tracer.objects.read(f"jobs/{TEAM}/{first}/job.json") is not None
    assert tracer.objects.read(f"jobs/{TEAM}/{first}/final.json") is not None


def test_a_late_worker_cannot_replace_a_finalized_outcome():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    tracer.clock.advance(JOB_LIFETIME)
    reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)
    finalized = result_body(tracer, job_id)
    before = tracer.objects.names()

    assert work(tracer, job_id) == 1

    assert tracer.objects.names() == before
    assert tracer.engine.calls == []
    assert result_body(tracer, job_id) == finalized


def test_a_straggler_after_cleanup_creates_nothing():
    tracer = build()
    job_id = submit(tracer).json()["job_id"]
    work(tracer, job_id)
    tracer.clock.advance(cleanup_guard(tracer.store.get_job(TEAM, job_id)) - tracer.clock.now())
    cleanup(store=tracer.store, clock=tracer.clock)
    after = tracer.objects.names()
    calls = list(tracer.engine.calls)

    assert work(tracer, job_id) == 1

    # No records to resume from: the straggler creates nothing back (§5.5).
    assert tracer.objects.names() == after
    assert tracer.engine.calls == calls

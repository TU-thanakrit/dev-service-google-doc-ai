"""Storage-contract tests for the durable, idempotent job store (seam S2).

These exercise the single-object conditional-write protocol directly: create-once
records, CAS replacement gated on the generation read *and* the job id the record
names, create-once input, and `final.json` as the sole final-state arbiter.

The object store here is deterministic and in-memory. It emulates generations and
412s; it is NOT evidence about real GCS behaviour (`docs/ocr-v1-contracts.md`, B1).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.ocr_store import (
    DISPATCH_ATTEMPT_CAP,
    DISPATCH_SPACING,
    LEASE_TTL,
    RECONCILER_FENCE,
    CorruptRecord,
    DurableOcrStore,
    JobRecord,
    PreconditionFailed,
)
from app.ocr_tracer_fakes import InMemoryObjectStore

TEAM = "team-a"
KEY_HASH = "key-hash-0001"
SUBMITTED_AT = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)


def make_job(job_id="job-0001", *, content_hash="hash-a", byte_length=42):
    return JobRecord(
        job_id=job_id,
        team_id=TEAM,
        accepted_at=SUBMITTED_AT,
        deadline_at=SUBMITTED_AT + timedelta(hours=24),
        content_hash=content_hash,
        byte_length=byte_length,
        key_hash=KEY_HASH,
        mime_type="application/pdf",
        engine_ref="engine-v1",
        worker_revision_digest="rev-1",
    )


def claim_for(job_id="job-0001", *, content_hash="hash-a", byte_length=42):
    from app.ocr_store import Claim

    return Claim(
        job_id=job_id,
        content_hash=content_hash,
        byte_length=byte_length,
        claimed_at=SUBMITTED_AT,
    )


@pytest.fixture()
def objects():
    return InMemoryObjectStore()


@pytest.fixture()
def store(objects):
    return DurableOcrStore(objects)


def test_claim_is_create_once_and_reports_its_generation(objects, store):
    generation = store.create_claim(TEAM, KEY_HASH, claim_for())

    stored, read_generation = store.claim(TEAM, KEY_HASH)
    assert stored.job_id == "job-0001"
    assert stored.content_hash == "hash-a"
    assert stored.byte_length == 42
    assert read_generation == generation
    assert store.claim(TEAM, "another-key") is None

    with pytest.raises(PreconditionFailed):
        store.create_claim(TEAM, KEY_HASH, claim_for("job-0002"))


def test_claim_replacement_requires_the_generation_and_job_id_read(objects, store):
    generation = store.create_claim(TEAM, KEY_HASH, claim_for("job-0001"))

    with pytest.raises(PreconditionFailed):
        store.replace_claim(TEAM, KEY_HASH, claim_for("job-0002"), generation + 1, "job-0001")
    with pytest.raises(PreconditionFailed):
        store.replace_claim(TEAM, KEY_HASH, claim_for("job-0002"), generation, "job-9999")

    # Another actor moved the record first: the stale replacement must not land.
    store.replace_claim(TEAM, KEY_HASH, claim_for("job-0002"), generation, "job-0001")
    with pytest.raises(PreconditionFailed):
        store.replace_claim(TEAM, KEY_HASH, claim_for("job-0003"), generation, "job-0001")

    stored, _ = store.claim(TEAM, KEY_HASH)
    assert stored.job_id == "job-0002"


def test_claim_deletion_is_conditional_and_cannot_remove_a_replacement(objects, store):
    generation = store.create_claim(TEAM, KEY_HASH, claim_for("job-0001"))
    store.replace_claim(TEAM, KEY_HASH, claim_for("job-0002"), generation, "job-0001")

    with pytest.raises(PreconditionFailed):
        store.delete_claim(TEAM, KEY_HASH, generation, "job-0001")

    _, current = store.claim(TEAM, KEY_HASH)
    store.delete_claim(TEAM, KEY_HASH, current, "job-0002")
    assert store.claim(TEAM, KEY_HASH) is None


def test_job_record_is_create_once_and_verifies_identity_on_conflict(store):
    job = make_job()
    store.create_job(job)

    # A retry materializing the same recorded job is accepted as-is.
    assert store.create_job(make_job())

    with pytest.raises(CorruptRecord):
        store.create_job(make_job("job-0001", content_hash="different-hash"))

    assert store.get_job(TEAM, "job-0001").content_hash == "hash-a"


def test_each_team_sees_only_its_own_job_and_claim(store):
    store.create_claim(TEAM, KEY_HASH, claim_for())
    store.create_job(make_job())

    assert store.claim("team-b", KEY_HASH) is None
    assert store.get_job("team-b", "job-0001") is None


def test_input_is_create_once_and_a_precondition_conflict_is_success(store):
    job = make_job(byte_length=len(b"document"))
    store.create_job(job)

    store.put_input(job, b"document")
    store.put_input(job, b"document")  # 412 means the input is already present

    assert store.read_input(TEAM, job.job_id) == b"document"
    assert store.read_input(TEAM, "job-missing") is None


def test_present_input_of_the_wrong_size_fails_closed(objects, store):
    job = make_job(byte_length=99)
    store.create_job(job)
    objects.create("inputs/team-a/job-0001", b"short")

    with pytest.raises(CorruptRecord):
        store.put_input(job, b"a document of an unexpected length")


def test_result_without_final_is_unpublished_and_invisible(store):
    job = make_job()
    store.create_job(job)
    job.state = "succeeded"
    job.pages_total = 1
    job.finalized_at = SUBMITTED_AT
    job.expires_at = SUBMITTED_AT + timedelta(hours=24)

    # Write the result object only, the moment before the final arbiter lands.
    objects = store.objects
    objects.create("jobs/team-a/job-0001/result.0.json", b"{}")

    view = store.get_job(TEAM, "job-0001")
    assert view.state == "accepted"
    assert view.finalized_at is None
    assert view.pages == []


def test_final_is_the_only_arbiter_and_a_losing_writer_changes_nothing(store):
    job = make_job()
    store.create_job(job)
    job.state = "succeeded"
    job.finalized_at = SUBMITTED_AT
    job.expires_at = SUBMITTED_AT + timedelta(hours=24)
    job.pages_total = 1
    job.pages = []

    assert store.publish(job, fence=RECONCILER_FENCE) is True

    loser = make_job()
    loser.state = "failed"
    loser.reason = "engine_failed"
    loser.finalized_at = SUBMITTED_AT + timedelta(minutes=5)
    loser.expires_at = SUBMITTED_AT + timedelta(hours=24, minutes=5)
    assert store.publish(loser, fence=RECONCILER_FENCE) is False

    view = store.get_job(TEAM, "job-0001")
    assert (view.state, view.reason) == ("succeeded", None)
    assert view.finalized_at == SUBMITTED_AT


def test_dispatch_intent_has_exactly_one_owner(store):
    job = make_job()
    store.create_job(job)
    job.execution_ref = "exec-1"

    assert store.create_dispatch(job) is not None
    assert store.create_dispatch(job) is None  # another submitter already owns it

    store.record_dispatch_ack(job, "exec-1")
    view = store.get_job(TEAM, job.job_id)
    assert view.acknowledged is True
    assert view.execution_ref == "exec-1"


def test_running_state_is_written_once_and_never_overwrites_a_final_state(store):
    job = make_job()
    store.create_job(job)

    store.mark_running(job, now=SUBMITTED_AT)
    assert store.get_job(TEAM, job.job_id).state == "running"

    finished = make_job()
    finished.state = "succeeded"
    finished.finalized_at = SUBMITTED_AT
    finished.expires_at = SUBMITTED_AT + timedelta(hours=24)
    assert store.publish(finished, fence=RECONCILER_FENCE) is True

    store.mark_running(make_job(), now=SUBMITTED_AT)
    assert store.get_job(TEAM, job.job_id).state == "succeeded"


def test_unreadable_final_record_fails_closed(objects, store):
    store.create_job(make_job())
    objects.create("jobs/team-a/job-0001/final.json", b"{not json")

    with pytest.raises(CorruptRecord):
        store.get_job(TEAM, "job-0001")


def test_stored_documents_carry_the_contract_schema_version(objects, store):
    store.create_job(make_job())

    raw, _ = objects.read("jobs/team-a/job-0001/job.json")
    document = json.loads(raw)
    assert document["schema_version"] == "ocr.v1"
    assert document["idempotency_key_hash"] == KEY_HASH
    assert document["state"] == "accepted"


# --- Lease and dispatch bookkeeping (issue #6, contract §5.3, §6.1) ---------------


def test_a_lease_is_create_once_and_a_live_holder_blocks_takeover(store):
    job = make_job()
    store.create_job(job)

    first = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)

    assert first.fence == 1
    # A second execution arriving while the lease is live claims nothing.
    assert store.acquire_lease(job, owner="exec-2", now=SUBMITTED_AT) is None
    assert store.live_lease(job, SUBMITTED_AT).owner == "exec-1"

    store.release_lease(job, first)
    assert store.read_lease(job) is None


def test_an_expired_lease_is_taken_over_with_a_higher_fence(store):
    job = make_job()
    store.create_job(job)
    first = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)
    later = SUBMITTED_AT + LEASE_TTL + timedelta(seconds=1)

    assert store.live_lease(job, later) is None
    second = store.acquire_lease(job, owner="exec-2", now=later)

    assert second.fence == 2
    # The superseded holder's conditional release is skipped, not forced.
    store.release_lease(job, first)
    assert store.read_lease(job).owner == "exec-2"
    assert store.live_lease(job, later).owner == "exec-2"


def test_a_renewal_extends_the_lease_without_changing_its_fence(store):
    job = make_job()
    store.create_job(job)
    lease = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)

    renewed = store.renew_lease(job, lease, now=SUBMITTED_AT + timedelta(minutes=1))

    assert renewed is not None
    assert (renewed.fence, renewed.owner) == (lease.fence, lease.owner)
    assert renewed.generation != lease.generation
    assert renewed.lease_expires_at > lease.lease_expires_at
    # The renewal is the object at the new generation: the next CAS uses it.
    assert store.read_lease(job).generation == renewed.generation


def test_a_renewal_after_a_takeover_is_refused(store):
    job = make_job()
    store.create_job(job)
    superseded = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)
    later = SUBMITTED_AT + LEASE_TTL + timedelta(seconds=1)
    assert store.acquire_lease(job, owner="exec-2", now=later).fence == 2

    # The superseded holder learns it no longer owns the execution and writes
    # nothing else; that `None` is the worker's stop rule (contract §5.3).
    assert store.renew_lease(job, superseded, now=later) is None
    assert store.read_lease(job).owner == "exec-2"


def test_two_executions_publish_fence_unique_results_and_final_names_the_winner(store, objects):
    job = make_job()
    store.create_job(job)
    first = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)
    later = SUBMITTED_AT + LEASE_TTL + timedelta(seconds=1)
    second = store.acquire_lease(job, owner="exec-2", now=later)

    job.pages_total = 1
    job.state = "succeeded"
    job.finalized_at = SUBMITTED_AT
    job.expires_at = SUBMITTED_AT + timedelta(hours=24)
    assert store.publish(job, fence=first.fence) is True

    # The second execution has its own object name, so it never reads or rewrites
    # the first one's result; it loses `final.json` and changes nothing.
    job.state = "failed"
    job.reason = "deadline_exceeded"
    assert store.publish(job, fence=second.fence) is False

    assert objects.read("jobs/team-a/job-0001/result.1.json") is not None
    assert objects.read("jobs/team-a/job-0001/result.2.json") is not None
    final = json.loads(objects.read("jobs/team-a/job-0001/final.json")[0])
    assert final["fence"] == first.fence
    assert final["result_object"] == "jobs/team-a/job-0001/result.1.json"
    assert store.get_job(TEAM, job.job_id).state == "succeeded"


def test_a_release_after_renewal_removes_this_holders_lease(store):
    job = make_job()
    store.create_job(job)
    lease = store.acquire_lease(job, owner="exec-1", now=SUBMITTED_AT)
    renewed = store.renew_lease(job, lease, now=SUBMITTED_AT + timedelta(minutes=1))

    # The heartbeat moved the object past the original generation; the release is
    # conditional on a fresh read plus the holder's identity, so it still lands.
    store.release_lease(job, renewed)
    assert store.read_lease(job) is None


def test_the_admission_page_count_is_persisted_with_the_job(objects, store):
    job = make_job()
    job.pages_total = 3
    store.create_job(job)
    # The `accepted -> running` CAS rewrites the whole document: the count must
    # round-trip, because a refused container is accounted from it.
    store.mark_running(job, now=SUBMITTED_AT)

    assert store.get_job_meta(TEAM, "job-0001").pages_total == 3


def test_a_job_without_a_known_page_count_stores_null(objects, store):
    store.create_job(make_job())

    raw, _ = objects.read("jobs/team-a/job-0001/job.json")
    assert json.loads(raw)["pages_total"] is None
    assert store.get_job_meta(TEAM, "job-0001").pages_total is None


def test_dispatch_attempts_are_counted_capped_and_spaced(store):
    job = make_job()
    store.create_job(job)

    assert store.create_dispatch(job) is not None
    assert store.get_job(TEAM, job.job_id).attempts == 1
    # Too soon: less than the least spacing since the last attempt.
    assert store.claim_next_attempt(job, SUBMITTED_AT + timedelta(minutes=1)) is None

    assert store.claim_next_attempt(job, SUBMITTED_AT + DISPATCH_SPACING) is not None
    assert store.get_job(TEAM, job.job_id).attempts == 2
    # The attempt cap is reached; no further launch, however long the wait.
    assert store.claim_next_attempt(job, SUBMITTED_AT + DISPATCH_SPACING * 4) is None
    assert store.get_job(TEAM, job.job_id).attempts == DISPATCH_ATTEMPT_CAP


def test_a_recorded_launch_error_is_cleared_by_a_later_acknowledgment(store):
    job = make_job()
    store.create_job(job)
    store.create_dispatch(job)

    store.record_dispatch_error(job, "engine_unavailable")
    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.execution_ref, view.last_error_code) == (
        False,
        None,
        "engine_unavailable",
    )

    store.record_dispatch_ack(job, "exec-9")
    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.execution_ref, view.last_error_code) == (True, "exec-9", None)


def test_list_jobs_enumerates_every_team_and_ignores_non_job_objects(store):
    store.create_job(make_job("job-1"))
    other = JobRecord(
        job_id="job-2",
        team_id="team-b",
        accepted_at=SUBMITTED_AT,
        deadline_at=SUBMITTED_AT + timedelta(hours=24),
        content_hash="hash-b",
        byte_length=7,
        key_hash=KEY_HASH,
        mime_type="image/png",
        engine_ref="engine-v1",
        worker_revision_digest="rev-1",
    )
    store.create_job(other)
    store.create_claim(TEAM, KEY_HASH, claim_for())

    listed = store.list_jobs()

    assert [(job.team_id, job.job_id) for job in listed] == [
        ("team-a", "job-1"),
        ("team-b", "job-2"),
    ]


def test_a_corrupt_job_record_fails_the_enumeration_closed(objects, store):
    store.create_job(make_job("job-1"))
    name = "jobs/team-a/job-1/job.json"
    _, generation = objects.read(name)
    objects.replace(name, b"{not json", generation)

    with pytest.raises(CorruptRecord):
        store.list_jobs()


# --- Page checkpoints (issue #8, contract §5.5/D41) -------------------------------


def page_checkpoint(number=1, *, text="recognized text", status="succeeded"):
    from app.ocr_store import PageResult

    return PageResult(
        page_number=number, status=status, text_source="ocr", text=text, warnings=[]
    )


def page_name(number=1):
    return f"jobs/team-a/job-0001/pages/{number:05d}.json"


def test_a_page_checkpoint_is_create_once_and_keeps_the_first_writer(store):
    job = make_job()
    store.create_job(job)

    store.put_page_checkpoint(job, page_checkpoint(text="first writer"), now=SUBMITTED_AT)
    store.put_page_checkpoint(job, page_checkpoint(text="second writer"), now=SUBMITTED_AT)

    assert store.read_page_checkpoint(job, 1).text == "first writer"


def test_a_checkpoint_pinned_to_another_engine_is_treated_as_absent(objects, store):
    job = make_job()
    store.create_job(job)
    store.put_page_checkpoint(job, page_checkpoint(), now=SUBMITTED_AT)
    _, generation = objects.read(page_name())
    # The same page written by a differently pinned engine is not compatible (D16).
    objects.replace(
        page_name(),
        json.dumps(
            {
                "schema_version": "ocr.v1",
                "job_id": "job-0001",
                "page_number": 1,
                "status": "succeeded",
                "text_source": "ocr",
                "text": "other engine",
                "warnings": [],
                "engine_ref": "engine-other",
            }
        ).encode(),
        generation,
    )

    assert store.read_page_checkpoint(job, 1) is None


def test_an_invalid_checkpoint_is_replaced_only_at_the_generation_read(objects, store):
    job = make_job()
    store.create_job(job)
    # A checkpoint that does not validate (a failed page is never one) is treated
    # as absent and may be replaced, conditionally, by a valid one (D41).
    objects.create(
        page_name(),
        json.dumps(
            {
                "schema_version": "ocr.v1",
                "job_id": "job-0001",
                "page_number": 7,
                "status": "failed",
                "text_source": "ocr",
                "text": None,
                "warnings": [],
                "engine_ref": job.engine_ref,
            }
        ).encode(),
    )

    store.put_page_checkpoint(job, page_checkpoint(text="recovered"), now=SUBMITTED_AT)

    assert store.read_page_checkpoint(job, 1).text == "recovered"

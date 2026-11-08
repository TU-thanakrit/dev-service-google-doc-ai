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

    assert store.publish(job) is True

    loser = make_job()
    loser.state = "failed"
    loser.reason = "engine_failed"
    loser.finalized_at = SUBMITTED_AT + timedelta(minutes=5)
    loser.expires_at = SUBMITTED_AT + timedelta(hours=24, minutes=5)
    assert store.publish(loser) is False

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

    store.mark_running(job)
    assert store.get_job(TEAM, job.job_id).state == "running"

    finished = make_job()
    finished.state = "succeeded"
    finished.finalized_at = SUBMITTED_AT
    finished.expires_at = SUBMITTED_AT + timedelta(hours=24)
    assert store.publish(finished) is True

    store.mark_running(make_job())
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

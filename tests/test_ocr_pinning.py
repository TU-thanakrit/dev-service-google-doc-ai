"""The per-job engine and worker pin (contract §8, D18/D19/D43).

An accepted job freezes its engine identity and its worker revision in its own
`job.json`, and every later retry runs that pin instead of whatever the
deployment is currently set to. These tests are the boundary where a
half-configured, unversioned, or drifted pin must fail closed rather than fall
back silently. Fakes only: no cloud client is constructed and no Cloud Run job is
read.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.ocr_config import OcrServiceSettings
from app.ocr_engine import EngineRegistry
from app.ocr_pinning import (
    PinError,
    WorkerPin,
    normalize_image_digest,
    versioned_worker_job_name,
)
from app.ocr_store import DurableOcrStore, JobRecord
from app.ocr_tracer import EnginePage
from app.ocr_tracer_fakes import FakeOcrEngine, InMemoryObjectStore

IMAGE = "asia-southeast1-docker.pkg.dev/thaiunion-ai-platform/ocr/worker"
DIGEST = "sha256:" + "0123456789ab" * 5 + "cdef"
SLUG = "0123456789ab"
TEAM = "team-a"
ACCEPTED_AT = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)


def build_pin(**overrides):
    kwargs = {
        "engine_ref": "engine-v1",
        "image": DIGEST,
        "job_name": "",
        "container_name": "worker",
        "args": (),
        "env": (),
        "task_timeout_s": 1800,
        "task_max_retries": 0,
    }
    kwargs.update(overrides)
    return WorkerPin.build(**kwargs)


def job_record(**overrides):
    kwargs = {
        "job_id": "job-0001",
        "team_id": TEAM,
        "accepted_at": ACCEPTED_AT,
        "deadline_at": ACCEPTED_AT + timedelta(hours=24),
        "content_hash": "hash-a",
        "byte_length": 4,
        "key_hash": "key-hash-0001",
        "mime_type": "application/pdf",
        "engine_ref": "engine-v1",
        "worker_revision_digest": DIGEST,
        "worker_job_name": f"dev-ocr-service-worker-{SLUG}",
    }
    kwargs.update(overrides)
    return JobRecord(**kwargs)


# --- The pinned worker image (§8: by immutable digest, never a tag) -----------


def test_an_image_reference_normalizes_to_its_immutable_digest():
    assert normalize_image_digest(DIGEST) == DIGEST
    assert normalize_image_digest(f"{IMAGE}@{DIGEST}") == DIGEST
    assert normalize_image_digest(f"  {IMAGE}@{DIGEST}  ") == DIGEST


@pytest.mark.parametrize(
    "reference",
    [
        "worker:latest",
        "worker",
        IMAGE,
        IMAGE + ":v1",
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,
        "sha1:" + "a" * 40,
        DIGEST + "-extra",
    ],
)
def test_an_image_that_is_not_a_full_sha256_digest_is_refused(reference):
    with pytest.raises(PinError):
        normalize_image_digest(reference)


def test_the_versioned_worker_job_name_is_derived_from_the_pinned_digest():
    assert versioned_worker_job_name(DIGEST) == f"dev-ocr-service-worker-{SLUG}"
    assert (
        versioned_worker_job_name(DIGEST, prefix="dev-ocr-service-ocrworker")
        == f"dev-ocr-service-ocrworker-{SLUG}"
    )


# --- A pin that cannot be used fails closed (§6.1 step 2, §8) -----------------


def test_a_job_name_that_is_not_version_qualified_is_refused():
    # One shared, unversioned job can be re-pointed under a job that was accepted
    # for another revision, which is the drift this pin exists to detect (§8).
    for name in ("dev-ocr-service-worker", SLUG, f"dev-ocr-service-worker-{SLUG}x", DIGEST):
        with pytest.raises(PinError):
            build_pin(job_name=name)


def test_a_job_name_may_keep_an_operator_prefix_as_long_as_it_is_version_qualified():
    pin = build_pin(job_name=f"dev-ocr-service-ocrworker-{SLUG}")

    assert pin.worker_job_name == f"dev-ocr-service-ocrworker-{SLUG}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine_ref": ""},
        {"engine_ref": " engine-v1"},
        {"container_name": ""},
        {"task_timeout_s": 0},
        {"task_timeout_s": -1},
        {"task_max_retries": -1},
        {"args": ("--once", 1)},
        {"env": (("OCR_JOB_ID",),)},
        {"env": (("OCR_JOB_ID", "job-1", "extra"),)},
        {"image": "worker:latest"},
    ],
)
def test_a_half_configured_pin_is_refused(overrides):
    with pytest.raises(PinError):
        build_pin(**overrides)


# --- The recorded pin is the pin (D43: written once, frozen for the job) ------


def test_the_pin_read_back_from_a_job_record_is_the_recorded_pin():
    pin = build_pin(
        args=("--once",),
        env=(("OCR_JOB_TEAM_ID", "team-a"), ("OCR_JOB_ID", "job-0001")),
    )
    job = job_record(**pin.job_fields())

    assert WorkerPin.from_job(job) == pin


def test_the_default_pin_sets_task_max_retries_to_zero_and_persists_it(monkeypatch):
    # Application dispatch attempts (<=2) own retry policy; Cloud Run task-level
    # retries default to 0 (`0` means tasks run once) and are pinned and read back.
    monkeypatch.delenv("OCR_TASK_MAX_RETRIES", raising=False)
    settings = OcrServiceSettings(
        project="thaiunion-ai-platform",
        bucket="dev-ocr-service-b",
        worker_revision_digest=DIGEST,
    )
    pin = settings.pin()
    assert pin.task_max_retries == 0

    store = DurableOcrStore(InMemoryObjectStore())
    store.create_job(job_record(**pin.job_fields()))
    stored = store.get_job(TEAM, "job-0001")
    assert stored is not None
    assert stored.task_max_retries == 0
    assert WorkerPin.from_job(stored) == pin


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine_ref": ""},
        {"worker_revision_digest": IMAGE + ":v1"},
        {"worker_job_name": ""},
        {"worker_job_name": "dev-ocr-service-worker"},
        {"task_timeout_s": 0},
    ],
)
def test_a_job_record_whose_recorded_pin_is_unusable_is_refused(overrides):
    with pytest.raises(PinError):
        WorkerPin.from_job(job_record(**overrides))


# --- The engine registry (§8: the job's engine, never the current one) --------


def test_the_registry_builds_the_engine_registered_for_the_recorded_ref():
    old = FakeOcrEngine([EnginePage(text="old engine")], engine_ref="engine-v1")
    new = FakeOcrEngine([EnginePage(text="new engine")], engine_ref="engine-v2")
    registry = EngineRegistry({"engine-v1": lambda: old, "engine-v2": lambda: new})

    assert registry.build("engine-v1") is old
    assert registry.build("engine-v2") is new


def test_an_unregistered_engine_ref_is_refused_without_falling_back():
    new = FakeOcrEngine([EnginePage(text="new engine")], engine_ref="engine-v2")
    registry = EngineRegistry({"engine-v2": lambda: new})

    with pytest.raises(PinError):
        registry.build("engine-v1")

    assert new.calls == []


def test_an_engine_that_declares_another_ref_is_refused():
    other = FakeOcrEngine([EnginePage(text="other")], engine_ref="engine-v9")
    registry = EngineRegistry({"engine-v1": lambda: other})

    with pytest.raises(PinError):
        registry.build("engine-v1")

"""Dispatch tests: the Cloud Run Admin adapter and the shared launch step.

The adapter speaks the documented `jobs:run` REST shape over an injected
transport, so these tests assert the request the adapter builds and how it maps
an HTTP outcome onto the retryable / non-retryable / unacknowledged distinction
(contract §6.1). No Google client is constructed and no call leaves the process.

The launch step is exercised against the durable store, because "acknowledged"
and "complete" must not be the same thing: a launch acknowledgment is recorded
in `dispatch.json`, and OCR completion is observed only from `final.json`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.ocr_dispatch import (
    CloudRunJobsDispatcher,
    LaunchNotRetryable,
    launch,
)
from app.ocr_store import FAILED, SUCCEEDED, JobRecord
from app.ocr_tracer_fakes import InMemoryOcrStore

TEAM = "team-a"
SUBMITTED_AT = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
RUN_URL = "https://run.googleapis.com/v2/projects/proj-1/locations/asia-southeast1/jobs/worker-job:run"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Records requests and replays one queued outcome per call."""

    def __init__(self, outcomes):
        self.requests = []
        self._outcomes = list(outcomes)

    def request(self, method, url, *, json, timeout):
        self.requests.append({"method": method, "url": url, "json": json, "timeout": timeout})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubDispatcher:
    """Minimal `Dispatcher`: replays one outcome, raising it if it is an exception."""

    engine_ref = "engine-v1"
    worker_revision_digest = "rev-1"

    def __init__(self, outcome):
        self._outcome = outcome

    def dispatch(self, job):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def build_dispatcher(session):
    return CloudRunJobsDispatcher(
        session=session,
        project="proj-1",
        region="asia-southeast1",
        job_name="worker-job",
        container_name="worker",
        engine_ref="engine-v1",
        worker_revision_digest="rev-1",
        task_timeout_s=1800,
        worker_args=("--once",),
        worker_env=(("OCR_JOB_TEAM_ID", "team-a"),),
    )


def sample_job():
    return JobRecord(
        job_id="job-0001",
        team_id=TEAM,
        accepted_at=SUBMITTED_AT,
        deadline_at=SUBMITTED_AT + timedelta(hours=24),
        content_hash="hash-a",
        byte_length=4,
        key_hash="key-hash-0001",
        mime_type="application/pdf",
        engine_ref="engine-v1",
        worker_revision_digest="rev-1",
    )


def build_store_with_job():
    store = InMemoryOcrStore()
    job = sample_job()
    store.create_job(job)
    return store, job


# --- Cloud Run Admin adapter -------------------------------------------------


def test_a_launch_posts_the_documented_run_body_and_returns_the_operation():
    session = FakeSession([FakeResponse(200, {"name": "operations/abc"})])

    reference = build_dispatcher(session).dispatch(sample_job())

    assert reference == "operations/abc"
    assert session.requests == [
        {
            "method": "POST",
            "url": RUN_URL,
            "json": {
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "worker",
                            "args": ["--once"],
                            "env": [{"name": "OCR_JOB_TEAM_ID", "value": "team-a"}],
                        }
                    ],
                    "taskCount": 1,
                    "timeout": "1800s",
                }
            },
            "timeout": 60.0,
        }
    ]
    # The override payload carries no image: the versioned job name is the pin.
    overrides = session.requests[0]["json"]["overrides"]["containerOverrides"][0]
    assert "image" not in overrides


def test_a_forbidden_or_missing_pinned_job_is_not_retryable():
    for status in (403, 404):
        with pytest.raises(LaunchNotRetryable):
            build_dispatcher(FakeSession([FakeResponse(status)])).dispatch(sample_job())


def test_a_server_error_or_a_lost_response_is_an_unacknowledged_launch():
    assert build_dispatcher(FakeSession([FakeResponse(500)])).dispatch(sample_job()) is None
    session = FakeSession([TimeoutError("no response")])
    assert build_dispatcher(session).dispatch(sample_job()) is None


# --- The shared launch step --------------------------------------------------


def test_an_acknowledged_launch_is_recorded_but_does_not_finalize_the_job():
    store, job = build_store_with_job()
    store.create_dispatch(job)

    launch(store, StubDispatcher("executions/exec-1"), job)

    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.execution_ref) == (True, "executions/exec-1")
    # Acknowledgment is not completion: no result and no final arbiter exist yet.
    assert view.state != SUCCEEDED and view.finalized_at is None


def test_a_lost_acknowledgment_leaves_the_job_accepted_and_retryable():
    store, job = build_store_with_job()
    store.create_dispatch(job)

    launch(store, StubDispatcher(None), job)

    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.execution_ref, view.last_error_code) == (False, None, None)


def test_a_non_retryable_launch_error_is_recorded_for_reconciliation():
    store, job = build_store_with_job()
    store.create_dispatch(job)

    launch(store, StubDispatcher(LaunchNotRetryable()), job)

    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.last_error_code) == (False, "engine_unavailable")
    assert view.finalized_at is None  # reconciliation finalizes it, not the submission
    assert view.state == "accepted" and view.state != FAILED

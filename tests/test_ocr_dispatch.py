"""Dispatch tests: the Cloud Run Admin adapter, the pin, and the shared launch step.

The adapter speaks the documented `jobs:run` REST shape over an injected
transport, so these tests assert the request the adapter builds and how it maps
an HTTP outcome onto the retryable / non-retryable / unacknowledged distinction
(contract §6.1). They also assert the two pin rules of §8/D43: the launch is built
from the pin **recorded in the job** rather than the dispatcher's current one, and
the pinned job's template is re-read and compared first, failing closed on any
mismatch. No Google client is constructed and no call leaves the process.

The launch step is exercised against the durable store, because "acknowledged"
and "complete" must not be the same thing: a launch acknowledgment is recorded
in `dispatch.json`, and OCR completion is observed only from `final.json`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.ocr_dispatch import (
    CloudRunJobsDispatcher,
    CloudRunJobTemplateGuard,
    LaunchNotRetryable,
    launch,
)
from app.ocr_pinning import WorkerPin, versioned_worker_job_name
from app.ocr_store import FAILED, SUCCEEDED, JobRecord
from app.ocr_tracer_fakes import InMemoryOcrStore

TEAM = "team-a"
SUBMITTED_AT = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
PROJECT = "proj-1"
REGION = "asia-southeast1"
BASE = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs"

# Two distinguishable worker revisions: the current deployment and the one an
# already-accepted job carries.
DIGEST_V1 = "sha256:" + "1" * 64
DIGEST_V2 = "sha256:" + "2" * 64
OLD_PIN = WorkerPin.build(
    engine_ref="engine-v1",
    image=DIGEST_V1,
    container_name="worker",
    env=(("OCR_JOB_TEAM_ID", TEAM), ("OCR_JOB_ID", "job-0001")),
    task_timeout_s=1800,
)
NEW_PIN = WorkerPin.build(
    engine_ref="engine-v2",
    image=DIGEST_V2,
    container_name="worker",
    task_timeout_s=600,
    task_max_retries=1,
)
OLD_JOB_NAME = versioned_worker_job_name(DIGEST_V1)
RUN_URL = f"{BASE}/{OLD_JOB_NAME}:run"
TEMPLATE_URL = f"{BASE}/{OLD_JOB_NAME}"


class FakeResponse:
    def __init__(self, status_code, payload=None, *, unparsable=False):
        self.status_code = status_code
        self._payload = payload
        self._unparsable = unparsable

    def json(self):
        if self._unparsable:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records requests and replays one queued outcome per call."""

    def __init__(self, outcomes):
        self.requests = []
        self._outcomes = list(outcomes)

    def request(self, method, url, *, timeout, json=None):
        self.requests.append({"method": method, "url": url, "json": json, "timeout": timeout})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubDispatcher:
    """Minimal `Dispatcher`: replays one outcome, raising it if it is an exception."""

    pin = OLD_PIN

    def __init__(self, outcome):
        self._outcome = outcome

    def dispatch(self, job):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def build_dispatcher(session, *, pin=NEW_PIN):
    return CloudRunJobsDispatcher(
        session=session,
        project=PROJECT,
        region=REGION,
        pin=pin,
        guard=CloudRunJobTemplateGuard(session=session, project=PROJECT, region=REGION),
    )


def sample_job(pin=OLD_PIN):
    return JobRecord(
        job_id="job-0001",
        team_id=TEAM,
        accepted_at=SUBMITTED_AT,
        deadline_at=SUBMITTED_AT + timedelta(hours=24),
        content_hash="hash-a",
        byte_length=4,
        key_hash="key-hash-0001",
        mime_type="application/pdf",
        engine_ref=pin.engine_ref,
        worker_revision_digest=pin.worker_revision_digest,
        worker_job_name=pin.worker_job_name,
        worker_container_name=pin.worker_container_name,
        worker_args=pin.worker_args,
        worker_env=pin.worker_env,
        task_timeout_s=pin.task_timeout_s,
        task_max_retries=pin.task_max_retries,
    )


def template_body(pin, **overrides):
    """The documented Cloud Run job template shape for `pin`, with drift applied."""
    containers = {
        "name": overrides.get("name", pin.worker_container_name),
        "image": overrides.get("image", pin.worker_revision_digest),
        "args": list(overrides.get("args", pin.worker_args)),
        "env": [
            {"name": name, "value": value}
            for name, value in overrides.get("env", pin.worker_env)
        ],
    }
    return {
        "name": f"projects/{PROJECT}/locations/{REGION}/jobs/{pin.worker_job_name}",
        "template": {
            "template": {
                "containers": [containers],
                "timeout": overrides.get("timeout", f"{pin.task_timeout_s}s"),
                "maxRetries": overrides.get("max_retries", str(pin.task_max_retries)),
            }
        },
    }


def build_store_with_job(pin=OLD_PIN):
    store = InMemoryOcrStore()
    job = sample_job(pin)
    store.create_job(job)
    return store, job


# --- Cloud Run Admin adapter -------------------------------------------------


def test_a_launch_reads_the_template_and_posts_the_recorded_pins_override_body():
    session = FakeSession(
        [FakeResponse(200, template_body(OLD_PIN)), FakeResponse(200, {"name": "operations/abc"})]
    )

    reference = build_dispatcher(session).dispatch(sample_job())

    assert reference == "operations/abc"
    # The read and the launch address the job the *record* names, not the
    # dispatcher's current pin: an operator change reaches new jobs only (§8, D43).
    assert session.requests == [
        {"method": "GET", "url": TEMPLATE_URL, "json": None, "timeout": 60.0},
        {
            "method": "POST",
            "url": RUN_URL,
            "json": {
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "worker",
                            "args": [],
                            "env": [
                                {"name": "OCR_JOB_TEAM_ID", "value": TEAM},
                                {"name": "OCR_JOB_ID", "value": "job-0001"},
                            ],
                        }
                    ],
                    "taskCount": 1,
                    "timeout": "1800s",
                }
            },
            "timeout": 60.0,
        },
    ]
    # The override payload carries no image: the versioned job name is the pin.
    overrides = session.requests[1]["json"]["overrides"]["containerOverrides"][0]
    assert "image" not in overrides
    # Neither the current job name nor its timeout appears anywhere in the launch.
    assert NEW_PIN.worker_job_name != OLD_JOB_NAME
    assert "600s" != session.requests[1]["json"]["overrides"]["timeout"]


@pytest.mark.parametrize(
    "drift",
    [
        {"image": DIGEST_V2},
        {"name": "other-container"},
        {"timeout": "600s"},
        {"max_retries": "2"},
        {"args": ["--other"]},
        {"env": (("OCR_ENGINE_LANG", "eng"),)},
    ],
)
def test_a_template_that_does_not_match_the_recorded_pin_refuses_the_launch(drift):
    session = FakeSession([FakeResponse(200, template_body(OLD_PIN, **drift))])

    with pytest.raises(LaunchNotRetryable):
        build_dispatcher(session).dispatch(sample_job())

    # Only the comparison read happened: a drifted job is never launched (§6.1 step 2).
    assert [request["method"] for request in session.requests] == ["GET"]


def test_a_template_whose_pinned_args_or_env_are_absent_is_still_launchable():
    # The pinned args/env travel as overrides, so a template that pins none is a
    # match - and the override body still carries the pinned values (§6.1 step 2).
    body = template_body(OLD_PIN, args=[], env=[])
    session = FakeSession([FakeResponse(200, body), FakeResponse(200, {"name": "operations/abc"})])

    assert build_dispatcher(session).dispatch(sample_job()) == "operations/abc"
    assert session.requests[1]["json"]["overrides"]["containerOverrides"][0]["env"] == [
        {"name": "OCR_JOB_TEAM_ID", "value": TEAM},
        {"name": "OCR_JOB_ID", "value": "job-0001"},
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse(403),
        FakeResponse(404),
        FakeResponse(500),
        FakeResponse(200, unparsable=True),
        FakeResponse(200, {"name": "some-other-job"}),
        TimeoutError("no response"),
    ],
)
def test_a_template_that_cannot_be_read_or_compared_fails_closed(outcome):
    session = FakeSession([outcome])

    with pytest.raises(LaunchNotRetryable):
        build_dispatcher(session).dispatch(sample_job())

    assert [request["method"] for request in session.requests] == ["GET"]


def test_a_record_whose_pin_is_not_normalized_is_refused_before_any_read():
    session = FakeSession([])
    job = sample_job()
    job.worker_revision_digest = "worker:latest"

    with pytest.raises(LaunchNotRetryable):
        build_dispatcher(session).dispatch(job)

    assert session.requests == []


def test_a_forbidden_or_missing_pinned_job_is_not_retryable():
    for status in (403, 404):
        session = FakeSession([FakeResponse(200, template_body(OLD_PIN)), FakeResponse(status)])
        with pytest.raises(LaunchNotRetryable):
            build_dispatcher(session).dispatch(sample_job())


def test_a_server_error_or_a_lost_response_is_an_unacknowledged_launch():
    for outcome in (FakeResponse(500), TimeoutError("no response")):
        session = FakeSession([FakeResponse(200, template_body(OLD_PIN)), outcome])
        assert build_dispatcher(session).dispatch(sample_job()) is None


def test_the_run_response_may_name_the_operation_at_either_documented_level():
    session = FakeSession(
        [
            FakeResponse(200, template_body(OLD_PIN)),
            FakeResponse(200, {"metadata": {"name": "operations/nested"}}),
        ]
    )

    assert build_dispatcher(session).dispatch(sample_job()) == "operations/nested"


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


def test_a_drifted_template_is_recorded_as_a_non_retryable_dispatch_error():
    store, job = build_store_with_job()
    store.create_dispatch(job)
    session = FakeSession([FakeResponse(200, template_body(OLD_PIN, image=DIGEST_V2))])

    launch(store, build_dispatcher(session), job)

    view = store.get_job(TEAM, job.job_id)
    assert (view.acknowledged, view.last_error_code) == (False, "engine_unavailable")
    assert view.finalized_at is None

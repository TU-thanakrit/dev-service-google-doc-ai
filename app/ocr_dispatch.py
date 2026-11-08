"""Dispatch of one accepted job to its version-pinned Cloud Run worker (contract §6.1, §8).

`launch` is the shared dispatch step: it calls the injected dispatcher once and
records the outcome as a launch *acknowledgment*, which no decision reads [D31].
OCR *completion* is observed only from the durable `final.json` arbiter (§5.4),
so a successful launch call alone never finalizes a job.

`CloudRunJobsDispatcher` is the Cloud Run Admin adapter: one
`POST /v2/projects/{project}/locations/{region}/jobs/{job}:run` with the
documented body `{validateOnly, etag, overrides}` and an `Operation` response.
`overrides` needs `run.jobs.runWithOverrides` and may carry only container args,
env, and a timeout - there is no image field, so the versioned job name *is* part
of the pin (§8).

The launch is built from the pin **recorded in the job**, never from this
dispatcher's current one [D43]: a job accepted before an operator changed the
engine, the image, or a timeout keeps its own revision for every retry, while
`pin` here only decides what *new* jobs are accepted with.

`CloudRunJobTemplateGuard` is the injected pre-launch read-and-compare of that
pin (D43): it re-reads the pinned job's template and refuses the launch on any
mismatch, a missing job, or a denied read. Being read-then-act, it cannot close
the window between the read and the call, so a job re-pointed inside that window
still runs the wrong template - the residual is stated in
`docs/ocr-v1-pinning.md` (§8), and resolving the real execution name remains
deployment evidence [B2].

The HTTP transport is injected: tests pass a fake, the production composition
passes an authenticated session. Nothing here imports a Google client.
"""

from typing import Any, Protocol

from app.ocr_pinning import PinError, WorkerPin, normalize_image_digest
from app.ocr_store import DurableOcrStore, JobRecord

CLOUD_RUN_API = "https://run.googleapis.com/v2"

# The worker reads the one job it was launched for from its environment (contract
# §6.1 step 2, `app/ocr_worker.py`). These are injected per launch from the
# accepted record, never taken from the pin or the template.
JOB_TEAM_ENV = "OCR_JOB_TEAM_ID"
JOB_ID_ENV = "OCR_JOB_ID"
JOB_ENV_NAMES = frozenset({JOB_TEAM_ENV, JOB_ID_ENV})

# A launch that a retry cannot fix (§6.1 step 5): missing or forbidden job, or any
# pin mismatch of step 2.
NON_RETRYABLE_STATUS = (403, 404)


class LaunchNotRetryable(Exception):
    """A launch error that retrying cannot fix; reconciliation finalizes the job."""

    def __init__(self, code: str = "engine_unavailable") -> None:
        super().__init__(code)
        self.code = code


class Dispatcher(Protocol):
    """Records the launch intent for a job and pins the worker that runs it.

    `pin` is the operator's pin for *newly accepted* jobs (§8, D43): the tracer
    copies it into the job record at acceptance. A launch never reads it - it uses
    the record - so an operator change reaches new jobs only. Implementations must
    return without running recognition: submission answers before recognition
    completes, and the returned reference is a launch acknowledgement, never a
    result.
    """

    pin: WorkerPin

    def dispatch(self, job: JobRecord) -> str | None: ...


class DispatchSession(Protocol):
    """The injected HTTP transport: `requests`-shaped `request(method, url, ...)`."""

    def request(
        self, method: str, url: str, *, timeout: float, json: Any = None
    ) -> Any: ...


class TemplateGuard(Protocol):
    """The injected pre-launch pin comparison (§6.1 step 2, D43).

    Raises `LaunchNotRetryable` when the pinned job's template cannot be read or
    does not match the pin: every failure here is a non-retryable
    `failed`/`engine_unavailable` conclusion, never a launch on an unverified
    template.
    """

    def verify(self, pin: WorkerPin) -> None: ...


def launch(store: DurableOcrStore, dispatcher: Dispatcher, job: JobRecord) -> None:
    """Perform one launch attempt and record only its acknowledgment.

    A non-retryable launch error is recorded as a dispatch error for
    reconciliation to act on; a lost or unacknowledged call records
    `acknowledged = false` (§6.1). Either way the job stays non-final here.
    """
    try:
        execution_ref = dispatcher.dispatch(job)
    except LaunchNotRetryable as exc:
        store.record_dispatch_error(job, exc.code)
        return
    store.record_dispatch_ack(job, execution_ref)


class CloudRunJobsDispatcher:
    """Launch a pinned Cloud Run Job with per-attempt overrides (§6.1, §8)."""

    def __init__(
        self,
        *,
        session: DispatchSession,
        project: str,
        region: str,
        pin: WorkerPin,
        guard: TemplateGuard,
        request_timeout_s: float = 60.0,
    ) -> None:
        self._session = session
        self._project = project
        self._region = region
        self._request_timeout_s = request_timeout_s
        self._guard = guard
        self.pin = pin

    def dispatch(self, job: JobRecord) -> str | None:
        """Launch the job's recorded pin; return the operation name, or `None`.

        The launch is not awaited: the response is an `Operation`, and worker
        completion is observed only from `final.json` (§6.1). A non-retryable
        status, an unusable recorded pin, or a template that does not match it
        raises `LaunchNotRetryable`; a transport failure or a server error is an
        unacknowledged launch that reconciliation may retry.
        """
        try:
            pin = WorkerPin.from_job(job)
        except PinError:
            # Nothing is launched from a record whose pin cannot be used (§6.1
            # step 2, §8): this is the fail-closed conclusion, not a retry.
            raise LaunchNotRetryable() from None
        self._guard.verify(pin)
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "name": pin.worker_container_name,
                        "args": list(pin.worker_args),
                        "env": _launch_env(pin, job),
                    }
                ],
                "taskCount": 1,
                "timeout": f"{pin.task_timeout_s}s",
            }
        }
        url = f"{CLOUD_RUN_API}/{self._resource(pin)}:run"
        try:
            response = self._session.request(
                "POST", url, json=body, timeout=self._request_timeout_s
            )
        except Exception:
            # A lost or timed-out launch response is neither completion nor a
            # conclusion the job can act on: it stays unacknowledged (§6.1).
            return None
        if response.status_code in NON_RETRYABLE_STATUS:
            raise LaunchNotRetryable()
        if response.status_code >= 400:
            return None
        operation = response.json() or {}
        return operation.get("name") or operation.get("metadata", {}).get("name")

    def _resource(self, pin: WorkerPin) -> str:
        return (
            f"projects/{self._project}/locations/{self._region}/jobs/{pin.worker_job_name}"
        )


class CloudRunJobTemplateGuard:
    """Read the pinned Cloud Run Job's template and compare it with the pin (D43).

    The job is mutable state, so the pin is enforcement-by-comparison: the image
    by digest, the container name, the timeout, the platform retry bound, and the
    pinned args/env are read back from the job resource and compared with the
    job's recorded pin immediately before the launch. A missing job, a denied
    read, an unreadable body, or any difference refuses the launch (§6.1 step 2).

    Read-then-act: the comparison cannot close the window between this read and
    `jobs:run`, so a job re-pointed inside that window still runs the wrong
    template (§8). The `jobs:run` request also accepts an `etag`, but its run-time
    failure semantics are undocumented and are not assumed here [B2].
    """

    def __init__(
        self,
        *,
        session: DispatchSession,
        project: str,
        region: str,
        request_timeout_s: float = 60.0,
    ) -> None:
        self._session = session
        self._project = project
        self._region = region
        self._request_timeout_s = request_timeout_s

    def verify(self, pin: WorkerPin) -> None:
        """Fail closed unless the pinned job's template matches `pin` (§6.1 step 2)."""
        url = (
            f"{CLOUD_RUN_API}/projects/{self._project}/locations/{self._region}"
            f"/jobs/{pin.worker_job_name}"
        )
        try:
            response = self._session.request("GET", url, timeout=self._request_timeout_s)
        except Exception:
            # An unread template cannot be compared, and §6.1 step 2 makes an
            # unreadable pin a non-retryable conclusion rather than a launch.
            raise LaunchNotRetryable() from None
        if response.status_code != 200:
            raise LaunchNotRetryable()
        try:
            document = response.json()
        except Exception:
            document = None
        if not _template_matches(document, pin):
            raise LaunchNotRetryable()


def _launch_env(pin: WorkerPin, job: JobRecord) -> list[dict[str, str]]:
    """The override env: the pinned pairs, then this job's own identifiers.

    A pinned env entry never decides which job runs: any pinned value for the two
    identifier names is dropped and the accepted record's values are appended, so
    a stale pin cannot point an execution at another job (§6.1).
    """
    environment = [
        {"name": name, "value": value}
        for name, value in pin.worker_env
        if name not in JOB_ENV_NAMES
    ]
    environment.append({"name": JOB_TEAM_ENV, "value": job.team_id})
    environment.append({"name": JOB_ID_ENV, "value": job.job_id})
    return environment


def _template_matches(document: Any, pin: WorkerPin) -> bool:
    """True when the read job resource matches the pin field by field (§6.1 step 2).

    Shape: a Cloud Run job's `template.template` is the task template, holding
    `containers`, `timeout`, and `maxRetries`. Reading it is a documented API
    shape but is **not** verified against a live API here [B2]; a body that does
    not carry the compared fields is a mismatch, so an unexpected shape fails
    closed instead of passing by omission.

    The pinned args/env must be equal *or absent*: they travel as dispatch
    overrides, so a template that pins nothing is still launchable.
    """
    try:
        task = document["template"]["template"]
        containers = task["containers"]
        container = next(
            item for item in containers if item.get("name") == pin.worker_container_name
        )
        image = normalize_image_digest(str(container["image"]))
    except (TypeError, KeyError, StopIteration, PinError):
        return False
    if image != pin.worker_revision_digest:
        return False
    if _seconds(task.get("timeout")) != pin.task_timeout_s:
        return False
    if _as_int(task.get("maxRetries", 0)) != pin.task_max_retries:
        return False
    arguments = container.get("args")
    if arguments and [str(argument) for argument in arguments] != list(pin.worker_args):
        return False
    environment = container.get("env")
    if environment and _env_pairs(environment) != pin.worker_env:
        return False
    return True


def _seconds(value: Any) -> int | None:
    """A Cloud Run duration (`"600s"`) as whole seconds; `None` if unparsable."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.endswith("s"):
        try:
            return int(value[:-1])
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _env_pairs(environment: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, list):
        return ()
    return tuple(
        (str(entry.get("name", "")), str(entry.get("value", "")))
        for entry in environment
        if isinstance(entry, dict)
    )

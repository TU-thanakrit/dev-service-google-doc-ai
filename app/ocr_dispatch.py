"""Dispatch of one accepted job to its version-pinned Cloud Run worker (contract §6.1).

`launch` is the shared dispatch step: it calls the injected dispatcher once and
records the outcome as a launch *acknowledgment*, which no decision reads [D31].
OCR *completion* is observed only from the durable `final.json` arbiter (§5.4),
so a successful launch call alone never finalizes a job.

`CloudRunJobsDispatcher` is the Cloud Run Admin adapter: one
`POST /v2/projects/{project}/locations/{region}/jobs/{job}:run` with the
documented body `{validateOnly, etag, overrides}` and an `Operation` response.
`overrides` needs `run.jobs.runWithOverrides` and may carry only container args,
env, and a timeout - there is no image field, so the versioned job name *is* part
of the pin (§8). The pre-launch template read-and-compare of the pin (D43) and
resolving the real execution name are deployment evidence [B2]; this adapter
carries the pin rather than enforcing it, and returns the operation name as an
opaque `execution_ref`.

The HTTP transport is injected: tests pass a fake, the production composition
passes an authenticated session. Nothing here imports a Google client.
"""

from typing import Any, Protocol

from app.ocr_store import DurableOcrStore, JobRecord

CLOUD_RUN_API = "https://run.googleapis.com/v2"

# A launch that a retry cannot fix (§6.1 step 5): missing or forbidden job.
NON_RETRYABLE_STATUS = (403, 404)


class LaunchNotRetryable(Exception):
    """A launch error that retrying cannot fix; reconciliation finalizes the job."""

    def __init__(self, code: str = "engine_unavailable") -> None:
        super().__init__(code)
        self.code = code


class Dispatcher(Protocol):
    """Records the launch intent for a job and pins the worker that runs it.

    `engine_ref` and `worker_revision_digest` are the fake, stable per-worker
    identity written once into every job (contract §8); comparing them against a
    real Cloud Run template is ticket #11. Implementations must return without
    running recognition: submission answers before recognition completes, and the
    returned reference is a launch acknowledgement, never a result.
    """

    engine_ref: str
    worker_revision_digest: str

    def dispatch(self, job: JobRecord) -> str | None: ...


class DispatchSession(Protocol):
    """The injected HTTP transport: `requests`-shaped `request(method, url, ...)`."""

    def request(self, method: str, url: str, *, json: dict[str, Any], timeout: float) -> Any: ...


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
    """Launch a pinned Cloud Run Job with per-attempt overrides (§6.1)."""

    engine_ref: str
    worker_revision_digest: str

    def __init__(
        self,
        *,
        session: DispatchSession,
        project: str,
        region: str,
        job_name: str,
        container_name: str,
        engine_ref: str,
        worker_revision_digest: str,
        task_timeout_s: int,
        worker_args: tuple[str, ...] = (),
        worker_env: tuple[tuple[str, str], ...] = (),
        request_timeout_s: float = 60.0,
    ) -> None:
        self._session = session
        self._resource = f"projects/{project}/locations/{region}/jobs/{job_name}"
        self._container_name = container_name
        self._task_timeout_s = task_timeout_s
        self._worker_args = tuple(worker_args)
        self._worker_env = tuple(worker_env)
        self._request_timeout_s = request_timeout_s
        self.engine_ref = engine_ref
        self.worker_revision_digest = worker_revision_digest

    def dispatch(self, job: JobRecord) -> str | None:
        """Launch the pinned job; return the operation name, or `None` (lost ack).

        The launch is not awaited: the response is an `Operation`, and worker
        completion is observed only from `final.json` (§6.1). A non-retryable
        status raises `LaunchNotRetryable`; a transport failure or a server error
        is an unacknowledged launch that reconciliation may retry.
        """
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "name": self._container_name,
                        "args": list(self._worker_args),
                        "env": [{"name": name, "value": value} for name, value in self._worker_env],
                    }
                ],
                "taskCount": 1,
                "timeout": f"{self._task_timeout_s}s",
            }
        }
        try:
            response = self._session.request(
                "POST",
                f"{CLOUD_RUN_API}/{self._resource}:run",
                json=body,
                timeout=self._request_timeout_s,
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

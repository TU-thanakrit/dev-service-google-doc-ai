"""Cloud Run Jobs reconciliation entrypoint (container command `python -m app.ocr_reconcile`).

Cloud Scheduler runs this job periodically. It scans the durable state and repairs
only what is safe to repair with conditional single-object writes, so concurrent
reconcilers cannot double-finalize: `final.json` is the single arbiter (contract
§6.2, §5.4).

Rows are evaluated in order. Reconciliation never resumes page work; it only
dispatches, finalizes, or leaves a job for its deadline.

Deferred, and named rather than silently missing: the capacity set, cleanup, and
the missing-input/`idem`-record repairs of §6.2 belong to later tickets. So does
assembling page checkpoints on the deadline path: reconciliation has no engine,
so it cannot know a job's page count and finalizes the deadline `failed` with no
page list rather than claim a wrong one. Resuming and accounting for such a job
belongs to the deadline ticket (#9).
"""

from app.ocr_dispatch import Dispatcher, launch
from app.ocr_store import (
    DISPATCH_ATTEMPT_CAP,
    FAILED,
    DurableOcrStore,
    JobRecord,
)
from app.ocr_tracer import RETENTION, Clock, finalize_deadline


def reconcile(*, store: DurableOcrStore, dispatcher: Dispatcher, clock: Clock) -> list[str]:
    """Repair every non-final job; return the actions taken, for the job log.

    Each action is `"<action>:<team>/<job>"` (the deadline and launch-failure
    rows carry the reason) so the Cloud Run job log has an allowlisted record
    without any document content [D20].
    """
    now = clock.now()
    actions: list[str] = []
    for job in store.list_jobs():
        action = _reconcile_job(store, dispatcher, job, now)
        if action is not None:
            actions.append(action)
    return actions


def _reconcile_job(
    store: DurableOcrStore, dispatcher: Dispatcher, job: JobRecord, now
) -> str | None:
    if job.finalized_at is not None:
        return None
    if now >= job.deadline_at:
        # At or after the deadline the job is finalized and can never be
        # `succeeded` [D42]. The deadline is the one bound that always ends a job.
        # A worker that crossed the deadline already published its usable pages;
        # a job reaching here has none durable the reconciler can account for.
        finalize_deadline(job, now)
        store.publish(job)
        return f"finalized:deadline_exceeded:{job.team_id}/{job.job_id}"
    if job.attempts == 0:
        # Crash between materialization and launch, or a lost `202` [D28].
        if store.create_dispatch(job) is not None:
            launch(store, dispatcher, job)
            return f"dispatched:{job.team_id}/{job.job_id}"
        return None
    if job.attempts >= DISPATCH_ATTEMPT_CAP:
        if job.last_error_code is not None:
            # A non-retryable launch error with attempts exhausted: finalize
            # rather than retry forever (§6.1 step 5).
            _finalize_unavailable(store, job, now)
            return f"finalized:{job.last_error_code}:{job.team_id}/{job.job_id}"
        # Attempts exhausted with no non-retryable error: wait for the deadline.
        return None
    if store.live_lease(job, now) is not None:
        return None
    if store.claim_next_attempt(job, now) is not None:
        # A lost or failed launch, or a launch that never held a lease: one more
        # attempt, `acknowledged` not consulted [D31].
        launch(store, dispatcher, job)
        return f"redispatched:{job.team_id}/{job.job_id}"
    return None


def _finalize_unavailable(store: DurableOcrStore, job: JobRecord, now) -> None:
    job.state = FAILED
    job.reason = "engine_unavailable"
    job.finalized_at = now
    job.expires_at = now + RETENTION
    store.publish(job)


def main(argv: list[str] | None = None) -> int:
    """Deployed entrypoint: scan the bucket once and repair what it can."""
    import logging

    from app.gcs_objects import create_gcs_ocr_store
    from app.ocr_config import OcrServiceSettings
    from app.ocr_dispatch import CloudRunJobsDispatcher
    from app.ocr_tracer import SystemClock

    settings = OcrServiceSettings()
    session = _authorized_session()
    store = create_gcs_ocr_store(settings.bucket, project=settings.bucket_project)
    dispatcher = CloudRunJobsDispatcher(
        session=session,
        project=settings.project,
        region=settings.region,
        job_name=settings.worker_job_name,
        container_name=settings.worker_container_name,
        engine_ref=settings.engine_ref,
        worker_revision_digest=settings.worker_revision_digest,
        task_timeout_s=settings.task_timeout_s,
        request_timeout_s=settings.request_timeout_s,
    )
    actions = reconcile(store=store, dispatcher=dispatcher, clock=SystemClock())
    logging.getLogger(__name__).info("ocr_reconcile actions=%d", len(actions))
    for action in actions:
        logging.getLogger(__name__).info("ocr_reconcile action=%s", action)
    return 0


def _authorized_session():
    """An authenticated session for `jobs:run`; needs `run.jobs.runWithOverrides`."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


if __name__ == "__main__":
    raise SystemExit(main())

"""Cloud Run Jobs cleanup entrypoint (container command `python -m app.ocr_cleanup`).

Cloud Scheduler runs this job periodically; it is the only physical deleter
(contract §6.3, D26). Object Lifecycle Management changes take up to 24 h to
propagate, so a lifecycle rule cannot meet the one-hour target and is at most an
orphan backstop, never the deletion mechanism.

The pass is application-driven and idempotent. It enumerates the jobs, refuses
every job whose guard has not passed, and deletes content before records with
every delete conditional on the generation read (`job.json` last among the job's
objects, the `idem` record last of all, and only while it still names that job).
A delete whose precondition fails is skipped, not forced, and the next pass
re-decides.

Access never depends on this job: the read gate denies at `expires_at` whether or
not a pass has run (§3.3), so a late or failed pass signals a problem instead of
extending exposure. What a late pass actually means is an overdue deletion, which
`cleanup` reports as an `overdue:` action for the job log.

Nothing here contacts GCP in fake mode, and no live scheduler exists. The bucket's
soft-delete/versioning policy (D17: retention `0`, versioning disabled) and the
alert wiring on these log lines are deployment configuration to verify, not code
(contract §10, B4); no exact-second erasure is claimed.
"""

import logging
from datetime import timedelta

from app.ocr_store import CorruptRecord, DurableOcrStore, JobRecord
from app.ocr_tracer import Clock

# Contract §10 / D32, provisional and benchmark-gated [B3]: cleanup targets one
# hour after expiry, and the earliest legal pass start stays inside it only while
# `per_attempt_timeout + cleanup_schedule_period <= 1 h`.
CLEANUP_TARGET = timedelta(hours=1)
PER_ATTEMPT_TIMEOUT = timedelta(minutes=30)
CLEANUP_SCHEDULE_PERIOD = timedelta(minutes=15)

logger = logging.getLogger(__name__)


def cleanup_guard(job: JobRecord, *, per_attempt_timeout: timedelta = PER_ATTEMPT_TIMEOUT):
    """The earliest instant a pass may delete this job's objects (§6.3, D39).

    `max(expiry_bound, deadline_at + per_attempt_timeout)`: no dispatch happens at
    or after `deadline_at`, an attempt is bounded by the per-attempt timeout, an
    attempt that starts at or after the deadline writes nothing, and a page call is
    abandoned at the deadline. So the last page write that can land is
    `deadline_at + per_attempt_timeout`, and no pass may start before it — otherwise
    a straggler could re-create objects after the pass reported completion.
    """
    expiry_bound = job.expires_at or job.deadline_at
    return max(expiry_bound, job.deadline_at + per_attempt_timeout)


def assert_cleanup_invariants(
    *,
    per_attempt_timeout: timedelta,
    schedule_period: timedelta,
    target: timedelta = CLEANUP_TARGET,
) -> None:
    """Assert the D32 pair that keeps the cleanup target reachable at startup."""
    if per_attempt_timeout + schedule_period > target:
        raise ValueError(
            "per_attempt_timeout + cleanup_schedule_period exceeds the cleanup target"
        )


def cleanup(
    *,
    store: DurableOcrStore,
    clock: Clock,
    per_attempt_timeout: timedelta = PER_ATTEMPT_TIMEOUT,
) -> list[str]:
    """Run one cleanup pass; return the actions taken, for the job log.

    Each action is `"<action>:<team>/<job>"`, so the Cloud Run job log carries an
    allowlisted record without any document content [D20]. A job whose record
    cannot be read is skipped and alerted rather than deleted on an unknown expiry;
    a deletion that was due more than `CLEANUP_TARGET` after expiry is reported as
    overdue and then attempted anyway.
    """
    now = clock.now()
    actions: list[str] = []
    for team_id, job_id in store.list_job_ids():
        try:
            job = store.get_job_meta(team_id, job_id)
        except CorruptRecord:
            # Fail closed: deleting on an undecidable expiry could destroy data
            # still inside its retention window (§6.3).
            actions.append(f"skipped_unreadable:{team_id}/{job_id}")
            continue
        if job is None:
            continue
        if now < cleanup_guard(job, per_attempt_timeout=per_attempt_timeout):
            continue
        if job.expires_at is not None and now >= job.expires_at + CLEANUP_TARGET:
            actions.append(f"overdue:{team_id}/{job_id}")
        if store.purge_job(job):
            actions.append(f"cleaned:{team_id}/{job_id}")
        else:
            # A skipped precondition or a failed delete: the next pass re-decides.
            actions.append(f"cleanup_incomplete:{team_id}/{job_id}")
    return actions


def main(argv: list[str] | None = None) -> int:
    """Deployed entrypoint: one cleanup pass over the bucket."""
    from app.gcs_objects import create_gcs_ocr_store
    from app.ocr_config import OcrServiceSettings
    from app.ocr_tracer import SystemClock

    settings = OcrServiceSettings()
    per_attempt_timeout = timedelta(seconds=settings.task_timeout_s)
    assert_cleanup_invariants(
        per_attempt_timeout=per_attempt_timeout,
        schedule_period=timedelta(seconds=settings.cleanup_schedule_period_s),
    )
    store = create_gcs_ocr_store(settings.bucket, project=settings.bucket_project)
    actions = cleanup(
        store=store, clock=SystemClock(), per_attempt_timeout=per_attempt_timeout
    )
    overdue = sum(1 for action in actions if action.startswith("overdue:"))
    logger.info("ocr_cleanup actions=%d overdue=%d", len(actions), overdue)
    for action in actions:
        logger.info("ocr_cleanup action=%s", action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Cloud Run Jobs worker entrypoint (container command `python -m app.ocr_worker`).

The dispatcher launches this job for one configured document: the dispatch
override carries `OCR_JOB_TEAM_ID` / `OCR_JOB_ID`. The worker claims that job's
lease, recognizes every page with the engine the job was **pinned** to, and
publishes the result the API already serves. It never listens on a port and exits
non-zero on failure, per the Cloud Run container runtime contract.

The engine is chosen through an injected registry keyed by the `engine_ref`
recorded in the job's `job.json` (§8), never by whatever this deployment is
currently configured with: a job accepted under a superseded engine keeps that
engine for every retry, and a ref this deployment cannot resolve is refused
rather than served by a substitute (D16). A worker image is per-revision, so a
deployment usually registers one ref; registering several is how one image can
still serve the revisions a live job may carry (`docs/ocr-v1-pinning.md`).
"""

import os

from app.ocr_admission import AdmissionLimits
from app.ocr_config import OcrServiceSettings
from app.ocr_engine import EngineRegistry, build_engine
from app.ocr_pinning import PinError
from app.ocr_store import DurableOcrStore, Lease
from app.ocr_tracer import Clock, SystemClock, run_job


def run_worker(
    *,
    store: DurableOcrStore,
    engines: EngineRegistry,
    clock: Clock,
    team_id: str,
    job_id: str,
    owner: str,
    limits: AdmissionLimits | None = None,
) -> int:
    """Run one configured job under a lease; return the process exit code.

    Reads the job before writing anything; a job that is absent or already final
    writes nothing and exits non-zero [D30]. The engine is then resolved from the
    job's recorded pin: a ref with no registered engine - or one that resolves to
    a different engine than the record names - writes nothing and exits non-zero,
    so no page is ever recognized under a pin the job was not accepted with
    (§5.5/D16). A duplicate launch that finds a live lease also writes nothing and
    exits non-zero, so two executions never both recognize the document (contract
    §5.3); the `final.json` arbiter still protects the published result if they do
    (§5.4). Exits non-zero, too, for an attempt that reached the job at or after
    its deadline, which writes nothing and is finalized by reconciliation (§5.5,
    §6.2). Releases its lease on completion or give-up. The deployment's own page
    bound travels here, so the worker refuses an over-limit container before
    recognizing any page (issue #10).
    """
    limits = limits or AdmissionLimits()
    job = store.get_job(team_id, job_id)
    if job is None or job.finalized_at is not None:
        return 1
    try:
        engine = engines.build(job.engine_ref)
    except PinError:
        # Fail closed with no write at all - not even a lease: an unresolvable
        # engine is a deployment fault, and the job waits for its deadline rather
        # than being recognized by whatever engine happened to be configured.
        return 1
    lease: Lease | None = store.acquire_lease(job, owner=owner, now=clock.now())
    if lease is None:
        return 1
    try:
        published = run_job(
            store=store,
            engine=engine,
            clock=clock,
            team_id=team_id,
            job_id=job_id,
            limits=limits,
        )
    finally:
        store.release_lease(job, lease)
    return 0 if published else 1


def main(argv: list[str] | None = None) -> int:
    """Deployed entrypoint: build the store and the engine registry, run one job."""
    from app.gcs_objects import create_gcs_ocr_store

    settings = OcrServiceSettings()
    store = create_gcs_ocr_store(settings.bucket, project=settings.bucket_project)
    engine_ref = settings.resolved_engine_ref()
    owner = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{settings.job_id}"
    return run_worker(
        store=store,
        # This image's own engine, under the ref its settings declare. A job
        # pinned to another ref is refused here, not served by this one.
        engines=EngineRegistry({engine_ref: lambda: build_engine(settings)}),
        clock=SystemClock(),
        team_id=settings.job_team_id,
        job_id=settings.job_id,
        owner=owner,
        limits=settings.admission_limits(),
    )


if __name__ == "__main__":
    raise SystemExit(main())

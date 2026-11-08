"""Cloud Run Jobs worker entrypoint (container command `python -m app.ocr_worker`).

The dispatcher launches this job for one configured document: the dispatch
override carries `OCR_JOB_TEAM_ID` / `OCR_JOB_ID`. The worker claims that job's
lease, recognizes every page with the fake engine, and publishes the result the
API already serves. It never listens on a port and exits non-zero on failure, per
the Cloud Run container runtime contract.

The engine is a deterministic fake: the operator-pinned real engine and its
enforcement are ticket #11, and `docs/ocr-v1-contracts.md` B3 (the CPU benchmark)
still gates the real one.
"""

import hashlib
import os

from app.ocr_config import OcrServiceSettings
from app.ocr_store import DurableOcrStore, Lease
from app.ocr_tracer import Clock, EnginePage, EngineResult, OcrEngine, SystemClock, run_job


class FakePagesEngine:
    """Deterministic placeholder engine: page text derived from the input bytes.

    Content-dependent so two different documents are distinguishable without
    invoking a real OCR model. Not a recognition quality claim of any kind.
    """

    def recognize(self, content: bytes, mime_type: str) -> EngineResult:
        digest = hashlib.sha256(content).hexdigest()[:12]
        return EngineResult(
            pages=(
                EnginePage(text=f"fake-ocr:{digest}:page-1"),
                EnginePage(text=f"fake-ocr:{digest}:page-2"),
            )
        )


def run_worker(
    *,
    store: DurableOcrStore,
    engine: OcrEngine,
    clock: Clock,
    team_id: str,
    job_id: str,
    owner: str,
) -> int:
    """Run one configured job under a lease; return the process exit code.

    Reads the job before writing anything; a job that is absent or already final
    writes nothing and exits non-zero [D30]. A duplicate launch that finds a live
    lease also writes nothing and exits non-zero, so two executions never both
    recognize the document (contract §5.3); the `final.json` arbiter still
    protects the published result if they do (§5.4). Releases its lease on
    completion or give-up.
    """
    job = store.get_job(team_id, job_id)
    if job is None or job.finalized_at is not None:
        return 1
    lease: Lease | None = store.acquire_lease(job, owner=owner, now=clock.now())
    if lease is None:
        return 1
    try:
        run_job(store=store, engine=engine, clock=clock, team_id=team_id, job_id=job_id)
    finally:
        store.release_lease(job, lease)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Deployed entrypoint: build the store from the environment and run one job."""
    from app.gcs_objects import create_gcs_ocr_store

    settings = OcrServiceSettings()
    store = create_gcs_ocr_store(settings.bucket, project=settings.project)
    owner = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{settings.job_id}"
    return run_worker(
        store=store,
        engine=FakePagesEngine(),
        clock=SystemClock(),
        team_id=settings.job_team_id,
        job_id=settings.job_id,
        owner=owner,
    )


if __name__ == "__main__":
    raise SystemExit(main())

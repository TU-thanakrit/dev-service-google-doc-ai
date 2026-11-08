"""Dev/test doubles for the OCR tracer: store, engine, dispatcher, identity.

Dev/test only. These are in-memory stand-ins that hold object identity instead
of GCS generations and conditional writes, so they are not evidence about real
GCS behaviour (`docs/ocr-v1-contracts.md`, B1).
"""

from fastapi import Request

from app.ocr_tracer import Clock, EngineResult, JobRecord, OcrEngine, OcrStore, run_job

# Fake, stable worker identity pinned into every local job (contract §8). The real
# per-revision pin, its template comparison, and its enforcement are ticket #11.
FAKE_ENGINE_REF = "fake-ocr-engine:v1"
FAKE_WORKER_REVISION_DIGEST = "sha256:fake-worker-revision-0001"


class InMemoryOcrStore:
    """Fake store: one process, plain dicts, no generations or conditional writes."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], JobRecord] = {}
        self.job_ids_by_key: dict[tuple[str, str], str] = {}

    def save_job(self, job: JobRecord) -> None:
        self.jobs[(job.team_id, job.job_id)] = job
        self.job_ids_by_key[(job.team_id, job.key_hash)] = job.job_id

    def get_job(self, team_id: str, job_id: str) -> JobRecord | None:
        return self.jobs.get((team_id, job_id))

    def find_job_by_key(self, team_id: str, key_hash: str) -> JobRecord | None:
        job_id = self.job_ids_by_key.get((team_id, key_hash))
        return None if job_id is None else self.jobs.get((team_id, job_id))


class FakeOcrEngine:
    """Fake recognition engine: returns its configured pages for the content given."""

    def __init__(self, pages) -> None:
        self._pages = tuple(pages)
        self.calls: list[tuple[bytes, str]] = []

    def recognize(self, content: bytes, mime_type: str) -> EngineResult:
        self.calls.append((content, mime_type))
        return EngineResult(pages=self._pages)


class ControllableDispatcher:
    """Fake dispatcher: records the launch intent and runs nothing until asked."""

    def __init__(
        self,
        *,
        store: OcrStore,
        engine: OcrEngine,
        clock: Clock,
        acknowledge: bool = True,
    ) -> None:
        self._store = store
        self._engine = engine
        self._clock = clock
        self._acknowledge = acknowledge
        self.engine_ref = FAKE_ENGINE_REF
        self.worker_revision_digest = FAKE_WORKER_REVISION_DIGEST
        self.pending: list[tuple[str, str]] = []

    def dispatch(self, job: JobRecord) -> str | None:
        self.pending.append((job.team_id, job.job_id))
        # A stable fake execution reference, the way Cloud Run names an execution
        # under the launched job; `acknowledge=False` is the lost-launch case.
        return f"fake-execution-{job.job_id}" if self._acknowledge else None

    def run_pending(self) -> None:
        """Run every queued job, the way a worker would."""
        while self.pending:
            team_id, job_id = self.pending.pop(0)
            run_job(
                store=self._store,
                engine=self._engine,
                clock=self._clock,
                team_id=team_id,
                job_id=job_id,
            )


class StaticIdentity:
    """Fake identity resolver. Real ID-token verification is ticket #5."""

    def __init__(self, team_id: str | None) -> None:
        self._team_id = team_id

    def team_id(self, request: Request) -> str | None:
        return self._team_id

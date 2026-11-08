"""Dev/test doubles for the OCR tracer: store, engine, dispatcher, identity.

Dev/test only. The in-memory object store emulates GCS single-object atomicity
with real generations and 412s, so the durable store's conditional logic is
exercised deterministically. It is still not evidence about real GCS behaviour
(`docs/ocr-v1-contracts.md`, B1).
"""

import threading

from fastapi import Request

from app.ocr_store import (
    DurableOcrStore,
    JobRecord,
    PreconditionFailed,
)
from app.ocr_tracer import Clock, EngineResult, OcrEngine, run_job

# Fake, stable worker identity pinned into every local job (contract §8). The real
# per-revision pin, its template comparison, and its enforcement are ticket #11.
FAKE_ENGINE_REF = "fake-ocr-engine:v1"
FAKE_WORKER_REVISION_DIGEST = "sha256:fake-worker-revision-0001"


class InMemoryObjectStore:
    """Deterministic GCS-like objects: generations, create-if-absent, 412s."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, int]] = {}
        self._next_generation = 1
        # The race tests submit on two threads; nothing here may interleave a
        # generation bump with a write.
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return sorted(self._objects)

    def read(self, name: str) -> tuple[bytes, int] | None:
        return self._objects.get(name)

    def create(self, name: str, payload: bytes) -> int:
        with self._lock:
            if name in self._objects:
                raise PreconditionFailed(name)
            return self._write(name, payload)

    def replace(self, name: str, payload: bytes, generation: int) -> int:
        with self._lock:
            current = self._objects.get(name)
            if current is None or current[1] != generation:
                raise PreconditionFailed(name)
            return self._write(name, payload)

    def delete(self, name: str, generation: int) -> None:
        with self._lock:
            current = self._objects.get(name)
            if current is None or current[1] != generation:
                raise PreconditionFailed(name)
            del self._objects[name]

    def _write(self, name: str, payload: bytes) -> int:
        generation = self._next_generation
        self._next_generation += 1
        self._objects[name] = (payload, generation)
        return generation


class InMemoryOcrStore(DurableOcrStore):
    """The durable store over in-memory objects, shared by app restarts in tests."""

    def __init__(self, objects: InMemoryObjectStore | None = None) -> None:
        super().__init__(objects or InMemoryObjectStore())


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
        store: DurableOcrStore,
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

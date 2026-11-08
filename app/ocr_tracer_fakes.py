"""Dev/test doubles for the OCR tracer: store, engine, dispatcher, identity.

Dev/test only. The in-memory object store emulates GCS single-object atomicity
with real generations and 412s, so the durable store's conditional logic is
exercised deterministically. It is still not evidence about real GCS behaviour
(`docs/ocr-v1-contracts.md`, B1).
"""

import threading
from collections.abc import Mapping

from fastapi import Request

from app.ocr_dispatch import LaunchNotRetryable
from app.ocr_pinning import WorkerPin
from app.ocr_store import (
    DurableOcrStore,
    JobRecord,
    PreconditionFailed,
)
from app.ocr_tracer import Clock, EnginePage, OcrEngine, Principal, run_job

# Fake, stable engine and worker identity pinned into every local job (contract
# §8). A real digest, because a tag is exactly what the pin refuses.
FAKE_ENGINE_REF = "fake-ocr-engine:v1"
FAKE_WORKER_REVISION_DIGEST = "sha256:" + "1a2b3c4d5e6f" * 5 + "1a2b"
FAKE_WORKER_PIN = WorkerPin.build(
    engine_ref=FAKE_ENGINE_REF,
    image=FAKE_WORKER_REVISION_DIGEST,
    container_name="worker",
    task_timeout_s=1800,
)


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

    def list_names(self, prefix: str) -> list[str]:
        return [name for name in sorted(self._objects) if name.startswith(prefix)]

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
    """Fault-injectable per-page fake engine (seam S3).

    One scripted outcome per page: an `EnginePage` (succeeds), an exception to
    raise (`EnginePageError` for a page failure, anything else for a crash), or a
    list of those taken one per call so a retry can be scripted. `calls` records
    the page numbers recognized, so reuse of a checkpoint shows up as an absent
    page number.
    """

    def __init__(self, pages, *, engine_ref: str = FAKE_ENGINE_REF) -> None:
        self._pages = tuple(pages)
        # The fake's declared identity: two fakes with different refs are two
        # distinguishable engine revisions for the pin tests.
        self.engine_ref = engine_ref
        self.calls: list[int] = []

    def page_count(self, content: bytes, mime_type: str) -> int:
        return len(self._pages)

    def recognize_page(self, content: bytes, mime_type: str, page_number: int) -> EnginePage:
        self.calls.append(page_number)
        outcome = self._pages[page_number - 1]
        if isinstance(outcome, (list, tuple)):
            index = min(self.calls.count(page_number) - 1, len(outcome) - 1)
            outcome = outcome[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fake_engine_registry(engine: OcrEngine):
    """The worker's registry for one fake engine, keyed by its own declared ref.

    The production wiring registers this image's engine under the ref its settings
    declare; the fake is registered the same way, so a worker test exercises the
    same lookup with the same fail-closed outcome for an unknown ref.
    """
    from app.ocr_engine import EngineRegistry

    return EngineRegistry({engine.engine_ref: lambda: engine})


class ControllableDispatcher:
    """Fake dispatcher: records the launch intent and runs nothing until asked."""

    def __init__(
        self,
        *,
        store: DurableOcrStore,
        engine: OcrEngine,
        clock: Clock,
        pin: WorkerPin = FAKE_WORKER_PIN,
        acknowledge: bool = True,
        launch_error: str | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._clock = clock
        # The operator's current pin for new jobs: the fake never launches, so a
        # test can change it to model an operator revision change while an
        # already-accepted job keeps the pin in its own record.
        self.pin = pin
        self._acknowledge = acknowledge
        self._launch_error = launch_error
        self.pending: list[tuple[str, str]] = []

    def dispatch(self, job: JobRecord) -> str | None:
        self.pending.append((job.team_id, job.job_id))
        if self._launch_error is not None:
            # The non-retryable launch case (§6.1 step 5): the fake never starts
            # an execution, so reconciliation is the only actor that can end it.
            raise LaunchNotRetryable(self._launch_error)
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


class FakeTokenVerifier:
    """Fake authentication: issues exactly the tokens it was given (401 otherwise).

    Real signature/issuer/audience/expiry verification lives in `app.ocr_auth`;
    it is not imported here, so fake mode loads no GCP library.
    """

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        self._tokens = dict(tokens)

    def verify(self, request: Request) -> Principal | None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self._tokens.get(token.strip())

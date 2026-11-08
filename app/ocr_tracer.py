"""Dev/test tracer for the approved asynchronous OCR v1 lifecycle (issues #3, #4).

Dev/test only. Every collaborator is injected: this app is fake-backed and is
deliberately not mounted by the production app (`app.main`), so nothing on this
path constructs a GCP client. Persistence goes through the durable store seam in
`app.ocr_store`; the reviewed protocol's cross-object limits
(`docs/ocr-v1-contracts.md` §5.2) are stated there and not papered over here.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from app.ocr_dispatch import Dispatcher, launch
from app.ocr_store import (
    FAILED,
    MATERIALIZATION_GRACE,
    SCHEMA_VERSION,
    SUCCEEDED,
    Claim,
    DurableOcrStore,
    JobRecord,
    PageResult,
    PreconditionFailed,
    StorageUnavailable,
)

logger = logging.getLogger(__name__)

# Spec-confirmed ceilings: 24 h maximum job lifetime, retention ends one day
# after finalization. Benchmark-gated numbers are deliberately absent here.
JOB_LIFETIME = timedelta(hours=24)
RETENTION = timedelta(hours=24)

# Allowlisted messages: no path, bucket, job, processor, or token.
ERROR_MESSAGES = {
    "unauthorized_caller": "caller identity could not be verified",
    "team_not_authorized": "verified caller is not mapped to a team",
    "not_found": "job not found",
    "idempotency_conflict": "Idempotency-Key was already used with different content",
    "invalid_idempotency_key": "Idempotency-Key is missing or malformed",
    "empty_document": "document is empty",
    "unsupported_media_type": "declared and detected content type do not match a supported type",
    "job_expired": "job record or result has expired",
    "storage_unavailable": "durable storage is temporarily unavailable",
    "internal_error": "internal error",
}

# Idempotency-Key: 8-128 chars of [A-Za-z0-9._:~-], scoped to the verified team.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:~-]{8,128}")

# Container gate: bytes decide, never the declared type. Content-level checks
# (encrypted, malformed, truncated) belong to the implementation tickets.
CONTAINER_MAGIC = (
    ("application/pdf", b"%PDF-"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/tiff", b"II*\x00"),
    ("image/tiff", b"MM\x00*"),
)

# Bounded re-decide on a lost idempotency race (contract §3.4): re-read, decide
# again. Exhaustion fails closed instead of creating a second job.
MAX_IDEMPOTENCY_ATTEMPTS = 5


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock implementation for local runs."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller: the identity the credential proves, nothing caller-declared."""

    email: str
    subject: str


class TokenVerifier(Protocol):
    """Authentication seam: request credential -> verified principal (§7.1).

    `None` means the credential is missing or fails verification (401). The
    adapter refuses every identity header; only the presented token counts.
    """

    def verify(self, request: Request) -> Principal | None: ...


class TeamDirectory(Protocol):
    """Authorization seam: verified principal -> stable internal team_id (§7.3)."""

    def team_id(self, principal: Principal) -> str | None: ...


class MappingTeamDirectory:
    """Deployed allowlist: verified principal email -> internal team_id.

    `team_id` is the internal ownership key (never an email), so a future Entra
    identity can be mapped onto the same records (issue #5).
    """

    def __init__(self, teams: dict[str, str]) -> None:
        self._teams = dict(teams)

    def team_id(self, principal: Principal) -> str | None:
        return self._teams.get(principal.email)


@dataclass(frozen=True, slots=True)
class EnginePage:
    text: str
    text_source: str = "ocr"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineResult:
    pages: tuple[EnginePage, ...]


class OcrEngine(Protocol):
    """Application-owned recognition adapter seam (S3).

    Engine/model pinning per job is ticket #11, so no engine identity is
    recorded or exposed here.
    """

    def recognize(self, content: bytes, mime_type: str) -> EngineResult: ...


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _key_hash(team_id: str, key: str) -> str:
    return hashlib.sha256(f"{team_id}\n{key}".encode()).hexdigest()


def _sniff_container(content: bytes) -> str | None:
    for mime_type, magic in CONTAINER_MAGIC:
        if content.startswith(magic):
            return mime_type
    return None


def _deny_instant(job: JobRecord) -> datetime:
    """The instant from which the job stops serving.

    One expiry rule: the finalized expiry when it exists, otherwise the deadline.
    """
    return job.expires_at or job.deadline_at


class OcrError(Exception):
    """A contract error to render as the allowlisted error body."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _error(
    status_code: int, code: str, request: Request, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "schema_version": SCHEMA_VERSION,
            "error": {
                "code": code,
                "message": ERROR_MESSAGES[code],
                "request_id": request.headers.get("X-Request-Id"),
            },
        },
    )


def _verified_team(verifier: TokenVerifier, directory: TeamDirectory, request: Request) -> str:
    principal = verifier.verify(request)
    if principal is None:
        raise OcrError(401, "unauthorized_caller")
    team_id = directory.team_id(principal)
    if team_id is None:
        raise OcrError(403, "team_not_authorized")
    return team_id


def _servable_job(store: DurableOcrStore, team_id: str, job_id: str, now: datetime) -> JobRecord:
    """One expiry rule, applied before any content is read (contract §3.3).

    A foreign team's job id is simply absent, so team isolation needs no
    separate branch here.
    """
    job = store.get_job(team_id, job_id)
    if job is None:
        raise OcrError(404, "not_found")
    if now >= _deny_instant(job):
        raise OcrError(410, "job_expired")
    return job


def _status_body(job: JobRecord) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.job_id,
        "state": job.state,
        "timestamps": {
            "accepted_at": _iso(job.accepted_at),
            "deadline_at": _iso(job.deadline_at),
            "finalized_at": _iso(job.finalized_at),
            "expires_at": _iso(job.expires_at),
        },
        "dispatch": {"acknowledged": job.acknowledged, "execution_ref": job.execution_ref},
    }
    if job.pages_total is not None:
        body["progress"] = {
            "pages_total": job.pages_total,
            "pages_done": sum(1 for page in job.pages if page.status == SUCCEEDED),
        }
    return body


def _page_result(page_number: int, page: EnginePage) -> PageResult:
    return PageResult(
        page_number=page_number,
        status=SUCCEEDED,
        text_source=page.text_source,
        text=page.text.strip(),
        warnings=list(page.warnings),
    )


def _result_body(job: JobRecord) -> dict[str, Any]:
    succeeded = [page for page in job.pages if page.status == SUCCEEDED]
    warnings: list[str] = []
    for page in job.pages:
        warnings.extend(warning for warning in page.warnings if warning not in warnings)

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.job_id,
        "state": job.state,
        "reason": job.reason,
        "completeness": "complete"
        if job.state == SUCCEEDED and len(succeeded) == len(job.pages)
        else "partial",
        "accuracy": "not_verified",
        "page_count": len(job.pages),
    }
    if succeeded:
        # Contract text rule: successful pages joined by a single newline, in
        # ascending page order; failed pages contribute nothing.
        body["text"] = "\n".join(page.text or "" for page in succeeded)
    body["pages"] = [page.as_body() for page in job.pages]
    body["warnings"] = warnings
    body["timestamps"] = {
        "accepted_at": _iso(job.accepted_at),
        "finalized_at": _iso(job.finalized_at),
        "expires_at": _iso(job.expires_at),
    }
    return body


def _new_job(
    claim: Claim,
    *,
    team_id: str,
    key_hash: str,
    mime_type: str,
    dispatcher: Dispatcher,
) -> JobRecord:
    return JobRecord(
        job_id=claim.job_id,
        team_id=team_id,
        accepted_at=claim.claimed_at,
        deadline_at=claim.claimed_at + JOB_LIFETIME,
        content_hash=claim.content_hash,
        byte_length=claim.byte_length,
        key_hash=key_hash,
        mime_type=mime_type,
        engine_ref=dispatcher.engine_ref,
        worker_revision_digest=dispatcher.worker_revision_digest,
    )


def _record_dispatch(store: DurableOcrStore, dispatcher: Dispatcher, job: JobRecord) -> None:
    """Only the creator of the dispatch intent launches the worker (contract §6.1)."""
    if store.create_dispatch(job) is not None:
        launch(store, dispatcher, job)


def _materialize(
    store: DurableOcrStore, dispatcher: Dispatcher, job: JobRecord, content: bytes
) -> None:
    """Ensure the job record, its input, and one dispatch intent exist.

    Every step is create-once, so a retry that repairs a crash materializes
    nothing new. Returns nothing: a retry that adopted another actor's job id
    must not compensate for it.
    """
    store.create_job(job)
    store.put_input(job, content)
    _record_dispatch(store, dispatcher, job)


def _compensate(
    store: DurableOcrStore,
    team_id: str,
    key_hash: str,
    job: JobRecord,
    claim_generation: int,
) -> None:
    """Undo only what this claim creator wrote itself (contract §4 step 5).

    Content first, then records, every step conditional. A step that loses its
    precondition is skipped, not forced; reconciliation owns the remainder.
    """
    for cleanup in (
        lambda: store.delete_input(job),
        lambda: store.delete_job(job),
        lambda: store.delete_claim(team_id, key_hash, claim_generation, job.job_id),
    ):
        try:
            cleanup()
        except (PreconditionFailed, StorageUnavailable):
            pass


def _submit(
    *,
    store: DurableOcrStore,
    dispatcher: Dispatcher,
    team_id: str,
    key_hash: str,
    content: bytes,
    content_hash: str,
    mime_type: str,
    now: datetime,
) -> tuple[int, JobRecord]:
    """Claim, materialize, and record the launch intent (contract §4).

    Returns the HTTP status and the job to render. Same-key decisions follow
    §3.4: a live record with the same content returns the existing job, different
    content conflicts, and a record whose job is absent is either a crash-window
    retry inside `materialization_grace` or a stale record beyond it.
    """
    proposed = Claim(
        job_id=str(uuid.uuid4()),
        content_hash=content_hash,
        byte_length=len(content),
        claimed_at=now,
    )

    for _ in range(MAX_IDEMPOTENCY_ATTEMPTS):
        existing = store.claim(team_id, key_hash)
        if existing is None:
            try:
                claim_generation = store.create_claim(team_id, key_hash, proposed)
            except PreconditionFailed:
                continue
            return _accept(
                store,
                dispatcher,
                team_id,
                key_hash,
                proposed,
                content,
                mime_type,
                claim_generation=claim_generation,
            )

        record, generation = existing
        job = store.get_job(team_id, record.job_id)
        same_content = record.content_hash == content_hash and record.byte_length == len(content)

        if job is not None and now < _deny_instant(job):
            if not same_content:
                raise OcrError(409, "idempotency_conflict")
            if job.finalized_at is None:
                # A same-key retry starts no second logical job; it only repairs
                # a materialization or dispatch intent that a crash lost.
                _materialize(store, dispatcher, job, content)
            return 200, store.get_job(team_id, job.job_id) or job

        if job is None and now - record.claimed_at < MATERIALIZATION_GRACE:
            if not same_content:
                raise OcrError(409, "idempotency_conflict")
            # Crash between the claim and `job.json`: materialize at the recorded
            # job id, so a 200 never names a job that does not exist.
            rebuilt = _new_job(
                record,
                team_id=team_id,
                key_hash=key_hash,
                mime_type=mime_type,
                dispatcher=dispatcher,
            )
            _materialize(store, dispatcher, rebuilt, content)
            return 200, store.get_job(team_id, rebuilt.job_id) or rebuilt

        # Expired, or a stale claim whose job record is gone: replace the record
        # at the generation read, conditional on it still naming that job.
        try:
            store.replace_claim(team_id, key_hash, proposed, generation, record.job_id)
        except PreconditionFailed:
            continue
        return _accept(
            store,
            dispatcher,
            team_id,
            key_hash,
            proposed,
            content,
            mime_type,
            claim_generation=None,
        )

    # Five lost races is not a second job: fail closed (contract §3.4).
    raise StorageUnavailable("idempotency claim did not settle")


def _accept(
    store: DurableOcrStore,
    dispatcher: Dispatcher,
    team_id: str,
    key_hash: str,
    claim: Claim,
    content: bytes,
    mime_type: str,
    claim_generation: int | None,
) -> tuple[int, JobRecord]:
    """Create a new job for a claim this request owns, or fail closed with 503."""
    job = _new_job(
        claim, team_id=team_id, key_hash=key_hash, mime_type=mime_type, dispatcher=dispatcher
    )
    _, created = store.create_job(job)
    try:
        store.put_input(job, content)
        _record_dispatch(store, dispatcher, job)
    except StorageUnavailable:
        # Remove only a record and objects this request created itself; a record
        # it replaced is left for the materialization-grace retry to repair.
        if claim_generation is not None and created:
            _compensate(store, team_id, key_hash, job, claim_generation)
        raise
    return 202, store.get_job(team_id, job.job_id) or job


def run_job(
    *,
    store: DurableOcrStore,
    engine: OcrEngine,
    clock: Clock,
    team_id: str,
    job_id: str,
) -> None:
    """Worker step: recognize every page of one accepted job.

    Local form of the Cloud Run Job entrypoint. It reads the job before writing
    anything and writes nothing when the job is absent, already final, or has no
    input. It never produces `succeeded` at or after `deadline_at` (D42): a
    worker that reaches the job past the deadline finalizes `deadline_exceeded`
    without processing it, and one that crosses the deadline while recognizing
    discards that attempt's pages. Per-page failure and partial finalization stay
    with ticket #8; reconciliation of an unusable job is ticket #9.
    """
    job = store.get_job(team_id, job_id)
    if job is None or job.finalized_at is not None:
        return

    now = clock.now()
    if now >= job.deadline_at:
        # No page work is allowed at or after the deadline, so nothing is
        # recognized and the job is finalized as it stands.
        finalize_deadline(job, now)
        store.publish(job)
        return

    store.mark_running(job)
    content = store.read_input(team_id, job_id)
    if content is None:
        return

    result = engine.recognize(content, job.mime_type)
    now = clock.now()
    if now >= job.deadline_at:
        finalize_deadline(job, now)
    else:
        job.pages_total = len(result.pages)
        job.pages = [
            _page_result(number, page) for number, page in enumerate(result.pages, start=1)
        ]
        job.state = SUCCEEDED
        job.finalized_at = now
        job.expires_at = now + RETENTION
    store.publish(job)


def finalize_deadline(job: JobRecord, now: datetime) -> None:
    """Finalize a job at or after its deadline; never `succeeded` (D42).

    Partial page results belong to ticket #8, so this slice publishes no pages
    and no combined text and the outcome is `failed`.
    """
    job.state = FAILED
    job.reason = "deadline_exceeded"
    job.finalized_at = now
    job.expires_at = now + RETENTION


def create_ocr_tracer_app(
    *,
    store: DurableOcrStore,
    dispatcher: Dispatcher,
    clock: Clock,
    verifier: TokenVerifier,
    directory: TeamDirectory,
) -> FastAPI:
    """Build the tracer app from injected collaborators. Dev/test composition only.

    The recognition engine is owned by the injected dispatcher/worker, not by the
    API, so the API never constructs or holds one.
    """
    app = FastAPI(title="OCR v1 local lifecycle tracer (dev/test only)", version="0.1.0")

    @app.exception_handler(OcrError)
    async def handle_ocr_error(request: Request, exc: OcrError) -> JSONResponse:
        return _error(exc.status_code, exc.code, request)

    @app.exception_handler(StorageUnavailable)
    async def handle_storage_unavailable(
        request: Request, exc: StorageUnavailable
    ) -> JSONResponse:
        # Fail closed: a record or job read that cannot be decided must never
        # create a second job (contract §3.4).
        logger.error(
            "ocr_tracer storage unavailable error_code=%s request_id=%s job_id=%s",
            "storage_unavailable",
            request.headers.get("X-Request-Id"),
            request.path_params.get("job_id"),
        )
        return _error(503, "storage_unavailable", request, headers={"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Allowlisted fields only: an external exception's text can carry a
        # storage path or a token, so it is never logged (D20).
        logger.error(
            "ocr_tracer request failed error_code=%s request_id=%s job_id=%s",
            "internal_error",
            request.headers.get("X-Request-Id"),
            request.path_params.get("job_id"),
        )
        return _error(500, "internal_error", request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/ocr/jobs")
    async def submit_job(
        request: Request,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        team_id = _verified_team(verifier, directory, request)

        key = idempotency_key or ""
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
            raise OcrError(400, "invalid_idempotency_key")

        content = await file.read()
        declared_type = (file.content_type or "").split(";")[0].strip().lower()
        if not content:
            raise OcrError(422, "empty_document")
        if _sniff_container(content) != declared_type:
            raise OcrError(415, "unsupported_media_type")

        status_code, job = _submit(
            store=store,
            dispatcher=dispatcher,
            team_id=team_id,
            key_hash=_key_hash(team_id, key),
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            mime_type=declared_type,
            now=clock.now(),
        )
        return JSONResponse(status_code=status_code, content=_status_body(job))

    @app.get("/v1/ocr/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> JSONResponse:
        job = _servable_job(
            store, _verified_team(verifier, directory, request), job_id, clock.now()
        )
        return JSONResponse(status_code=200, content=_status_body(job))

    @app.get("/v1/ocr/jobs/{job_id}/result")
    def get_result(job_id: str, request: Request) -> JSONResponse:
        job = _servable_job(
            store, _verified_team(verifier, directory, request), job_id, clock.now()
        )
        if job.finalized_at is None:
            return JSONResponse(
                status_code=202,
                content=_status_body(job),
                headers={"Retry-After": "10"},
            )
        return JSONResponse(status_code=200, content=_result_body(job))

    return app

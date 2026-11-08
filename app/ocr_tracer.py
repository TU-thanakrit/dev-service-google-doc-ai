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

from app.ocr_admission import (
    FILE_TOO_LARGE_MESSAGE,
    AdmissionLimits,
    InputRejected,
    RequestSizeLimitMiddleware,
    assert_limits,
    validate_document,
)
from app.ocr_dispatch import Dispatcher, launch
from app.ocr_store import (
    FAILED,
    MATERIALIZATION_GRACE,
    PARTIALLY_SUCCEEDED,
    SCHEMA_VERSION,
    SUCCEEDED,
    CapacityExceeded,
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
    "file_too_large": FILE_TOO_LARGE_MESSAGE,
    "too_many_pages": "document exceeds the page limit",
    "encrypted_document": "document is encrypted",
    "malformed_document": "document structure is not readable",
    "capacity_exceeded": "processing capacity is temporarily full",
    "job_expired": "job record or result has expired",
    "storage_unavailable": "durable storage is temporarily unavailable",
    "internal_error": "internal error",
}

# Idempotency-Key: 8-128 chars of [A-Za-z0-9._:~-], scoped to the verified team.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:~-]{8,128}")

# Bounded re-decide on a lost idempotency race (contract §3.4): re-read, decide
# again. Exhaustion fails closed instead of creating a second job.
MAX_IDEMPOTENCY_ATTEMPTS = 5

# Per-page recovery (issue #8). `PAGE_RETRIES` is the provisional D21 value: a
# transient page call is re-issued at most this many times inside one attempt, so
# no new limit is introduced here. Between-attempt backoff is deliberately absent
# (contract §11.1: no sleep); a backoff or spaced-retry value is benchmark-gated
# [B3] and belongs with the real engine's per-page timeout (ticket #11).
PAGE_RETRIES = 1

# Reviewed page-error categories (contract §2.7's closed vocabulary). A transient
# page failure may recover on a retry; a permanent one (an undecodable page) never
# can, so an unreadable document does not enter a retry loop. The job `reason` a
# failed page maps onto comes from §2.5's closed reason set.
TRANSIENT_PAGE_ERROR = "page_engine_error"
PERMANENT_PAGE_ERROR = "malformed_document"
# The submission bound's own code, reused on the worker path when the container the
# byte scan accepted turns out to hold more pages than the deployment admits (§2.3).
TOO_MANY_PAGES = "too_many_pages"
PAGE_ERROR_REASON = {
    TRANSIENT_PAGE_ERROR: "engine_unavailable",
    PERMANENT_PAGE_ERROR: "input_unreadable",
    # A container refused before any page is numbered keeps the reason its code
    # names: an over-limit document is unreadable input, never an engine failure.
    TOO_MANY_PAGES: "input_unreadable",
}
# A page abandoned at the deadline still needs an entry, so it carries the
# deadline's own code from the same closed set (§2.6, D42).
DEADLINE_PAGE_ERROR = "deadline_exceeded"


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


class EnginePageError(Exception):
    """One page's recognition failure, carrying its code from the closed set.

    `code` is published on the failed page (§2.6/§2.7); whether the page may be
    retried is the application's decision (see `PAGE_RETRIES` and the category
    constants above), never the engine's, so the retry bound cannot be widened by
    an engine's own claim.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OcrEngine(Protocol):
    """Application-owned recognition adapter seam (S3).

    `page_count` decodes the container for its source page count without
    recognizing anything (one page per PDF page / TIFF frame; JPEG and PNG are
    single-page, §2.6/D24). `recognize_page` recognizes one page, so a restart
    re-issues only the pages that are not already checkpointed (§5.5). Engine and
    model pinning per job is ticket #11, so no engine identity is recorded here.
    """

    def page_count(self, content: bytes, mime_type: str) -> int: ...

    def recognize_page(
        self, content: bytes, mime_type: str, page_number: int
    ) -> EnginePage: ...


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _key_hash(team_id: str, key: str) -> str:
    return hashlib.sha256(f"{team_id}\n{key}".encode()).hexdigest()


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

    The gate reads `job.json` and `final.json` only, so access is denied at
    `expires_at` whether or not cleanup has already deleted the content (D39): the
    result object is loaded after the gate has served, and only for a final job.
    A foreign team's job id is simply absent, so team isolation needs no separate
    branch here.
    """
    job = store.get_job_meta(team_id, job_id)
    if job is None:
        raise OcrError(404, "not_found")
    if now >= _deny_instant(job):
        raise OcrError(410, "job_expired")
    if job.finalized_at is not None:
        # `final.json` exists, so the gate served: the published result must be
        # present and readable, and a missing one is `500` plus an alert (§2.2).
        store.read_result(job)
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


def _failed_page(page_number: int, error_code: str) -> PageResult:
    """A failed page keeps its number and a closed-set code, and carries no text.

    Failed pages are always present in the accounting and are never checkpointed,
    so the next attempt recomputes them (§2.6/D23, D41).
    """
    return PageResult(
        page_number=page_number,
        status=FAILED,
        text_source="ocr",
        text=None,
        warnings=[],
        error_code=error_code,
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
    store: DurableOcrStore,
    dispatcher: Dispatcher,
    job: JobRecord,
    content: bytes,
    *,
    limits: AdmissionLimits,
    now: datetime,
) -> None:
    """Ensure the job record, its input, and one dispatch intent exist.

    Every step is create-once, so a retry that repairs a crash materializes
    nothing new. Returns nothing: a retry that adopted another actor's job id
    must not compensate for it.
    """
    _ensure_admission(store, job, limits, now)
    store.create_job(job)
    store.put_input(job, content)
    _record_dispatch(store, dispatcher, job)


def _compensate(
    store: DurableOcrStore,
    team_id: str,
    key_hash: str,
    job: JobRecord,
    claim_generation: int,
    *,
    now: datetime,
) -> None:
    """Undo only what this claim creator wrote itself (contract §4 step 5).

    Content first, then records, every step conditional. A step that loses its
    precondition is skipped, not forced; reconciliation owns the remainder. The
    creator's own admission entry goes too (§4 step 5); one it fails to remove
    self-expires at `deadline_at + per_attempt_timeout`.
    """
    for cleanup in (
        lambda: store.delete_input_if_present(job),
        lambda: store.delete_job(job),
        lambda: store.delete_claim(team_id, key_hash, claim_generation, job.job_id),
    ):
        try:
            cleanup()
        except (PreconditionFailed, StorageUnavailable):
            pass
    try:
        store.release_admission(job.job_id, now=now)
    except StorageUnavailable:
        pass


def _ensure_admission(
    store: DurableOcrStore, job: JobRecord, limits: AdmissionLimits, now: datetime
) -> None:
    """Ensure the global admission set holds this job before it is materialized.

    Contract §4 step 2 / §6.4/D40: the acquire happens on the paths that create or
    resume a job's work, before its record exists, and fails closed with `429` and
    no dispatch. A job already in the set is an idempotent success, so a same-key
    retry takes no second slot and a final-job retry acquires nothing at all.
    """
    try:
        store.acquire_admission(
            job.job_id,
            deadline_at=job.deadline_at,
            now=now,
            ceiling=limits.capacity_ceiling,
            per_attempt_timeout=limits.per_attempt_timeout,
        )
    except CapacityExceeded:
        raise OcrError(429, "capacity_exceeded") from None


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
    limits: AdmissionLimits,
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
                limits=limits,
                now=now,
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
                _materialize(store, dispatcher, job, content, limits=limits, now=now)
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
            _materialize(store, dispatcher, rebuilt, content, limits=limits, now=now)
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
            limits=limits,
            now=now,
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
    limits: AdmissionLimits,
    now: datetime,
) -> tuple[int, JobRecord]:
    """Create a new job for a claim this request owns, or fail closed 429/503.

    Admission comes first (§4 step 2): a request that cannot be admitted leaves no
    job record and no dispatch, so a retry after capacity frees up is a fresh
    attempt rather than a half-created job.
    """
    job = _new_job(
        claim, team_id=team_id, key_hash=key_hash, mime_type=mime_type, dispatcher=dispatcher
    )
    _ensure_admission(store, job, limits, now)
    _, created = store.create_job(job)
    try:
        store.put_input(job, content)
        _record_dispatch(store, dispatcher, job)
    except StorageUnavailable:
        # Remove only a record and objects this request created itself; a record
        # it replaced is left for the materialization-grace retry to repair.
        if claim_generation is not None and created:
            _compensate(store, team_id, key_hash, job, claim_generation, now=now)
        raise
    return 202, store.get_job(team_id, job.job_id) or job


def _is_retryable(code: str) -> bool:
    """Only the reviewed transient category is retried.

    Anything else fails the page at once, so an unreadable or misreported page
    never enters a retry loop (issue #8; the spec leaves the categories open).
    """
    return code == TRANSIENT_PAGE_ERROR


def _recognize_page(
    engine: OcrEngine, job: JobRecord, content: bytes, page_number: int
) -> PageResult:
    """Recognize one page, re-issuing a transient failure up to `PAGE_RETRIES`."""
    attempts = 0
    while True:
        try:
            page = engine.recognize_page(content, job.mime_type, page_number)
        except EnginePageError as exc:
            if _is_retryable(exc.code) and attempts < PAGE_RETRIES:
                attempts += 1
                continue
            return _failed_page(page_number, exc.code)
        return _page_result(page_number, page)


def _recognize_pages(
    store: DurableOcrStore,
    engine: OcrEngine,
    clock: Clock,
    job: JobRecord,
    content: bytes,
    total: int,
) -> list[PageResult]:
    """Recognize the pages without a compatible checkpoint, then account for the rest.

    A page with a valid checkpoint is reused and never recomputed (D41); a page
    not reached because the attempt crossed `deadline_at` is reported failed with
    the deadline's code, so the result still holds one entry per source page (§2.6).
    """
    pages: list[PageResult] = []
    for page_number in range(1, total + 1):
        checkpoint = store.read_page_checkpoint(job, page_number)
        if checkpoint is not None:
            pages.append(checkpoint)
            continue
        if clock.now() >= job.deadline_at:
            break
        page = _recognize_page(engine, job, content, page_number)
        if clock.now() >= job.deadline_at:
            # The page call is abandoned at the deadline: it contributes no page
            # and is not checkpointed (D42).
            break
        if page.status == SUCCEEDED:
            store.put_page_checkpoint(job, page, now=clock.now())
        pages.append(page)
    for page_number in range(len(pages) + 1, total + 1):
        pages.append(_failed_page(page_number, DEADLINE_PAGE_ERROR))
    return pages


def _page_count(engine: OcrEngine, content: bytes, job: JobRecord) -> tuple[int | None, str | None]:
    """The engine's source-page count, and the closed-set code when it refuses the bytes.

    A decoder failure is input, not a program error: it must reach the caller as a
    refusal instead of failing an attempt with a traceback (§9.14). The engine's own
    page-error code is kept when it reports one (§2.7), and anything else an engine
    raises is the application's permanent refusal, so an engine that fails outside
    that vocabulary can neither crash the worker nor escape the page bound.
    """
    try:
        return engine.page_count(content, job.mime_type), None
    except EnginePageError as exc:
        return None, exc.code
    except Exception:
        return None, PERMANENT_PAGE_ERROR


def _finalize_refused_input(
    job: JobRecord, now: datetime, *, page_count: int | None, error_code: str
) -> None:
    """Finalize a document the worker refuses to recognize: no page work at all.

    One failed page is published rather than one entry per source page, because the
    document is over the configured bound (or undecodable) and recognizing it to
    enumerate its pages is exactly what must not happen; `error_code` says which
    bound fired and comes from §2.7's closed set. This is the only place §2.6's
    per-source-page accounting is not met, and it is stated in
    `docs/ocr-v1-admission.md`.
    """
    job.pages_total = page_count
    job.pages = [_failed_page(1, error_code)]
    job.state = FAILED
    job.reason = PAGE_ERROR_REASON.get(error_code, "input_unreadable")
    job.finalized_at = now
    job.expires_at = now + RETENTION


def _job_reason(failed: list[PageResult]) -> str:
    """One reason for a non-succeeded job; a permanent page failure is the more
    specific cause (contract §2.5's closed reason set)."""
    codes = {page.error_code for page in failed}
    for code in (PERMANENT_PAGE_ERROR, TRANSIENT_PAGE_ERROR):
        if code in codes:
            return PAGE_ERROR_REASON[code]
    return "internal_error"


def _finalize_result(job: JobRecord, now: datetime) -> None:
    """Pick the honest final state for a completed attempt (§2.5, §3).

    Partial output is never labelled complete and no page is dropped: a job with
    at least one usable page is `partially_succeeded`, otherwise `failed`; both
    carry the reason of the dominant page failure. Reason is `null` only on
    `succeeded` (§2.5).
    """
    failed = [page for page in job.pages if page.status != SUCCEEDED]
    if not failed:
        job.state = SUCCEEDED
        job.reason = None
    else:
        job.state = (
            PARTIALLY_SUCCEEDED
            if any(page.status == SUCCEEDED for page in job.pages)
            else FAILED
        )
        job.reason = _job_reason(failed)
    job.finalized_at = now
    job.expires_at = now + RETENTION


def run_job(
    *,
    store: DurableOcrStore,
    engine: OcrEngine,
    clock: Clock,
    team_id: str,
    job_id: str,
    limits: AdmissionLimits | None = None,
) -> bool:
    """Worker step: recognize the job's unfinished pages and finalize honestly.

    Local form of the Cloud Run Job entrypoint. Returns `True` when it published a
    final result, `False` when it wrote nothing: a job that is absent, already
    final, or has no input, or an attempt that reaches the job at or after
    `deadline_at` [D30, D42]. Reads the job before writing anything, reuses
    compatible page checkpoints so only the missing pages are recomputed, and
    never produces `succeeded` at or after the deadline. The page count is checked
    against `limits.max_pages` before any page is recognized, and a container the
    engine cannot decode is refused the same way instead of failing the attempt
    (issues #10, #7).
    """
    limits = limits or AdmissionLimits()
    job = store.get_job(team_id, job_id)
    if job is None or job.finalized_at is not None:
        return False

    if clock.now() >= job.deadline_at:
        # Never works at or after the deadline: the attempt writes nothing and
        # exits non-zero, and reconciliation finalizes the job (§5.5, §6.2, D42).
        return False

    content = store.read_input(team_id, job_id)
    if content is None:
        return False

    store.mark_running(job, now=clock.now())
    total, refusal = _page_count(engine, content, job)
    # `total is None` is the engine's own refusal: `_page_count` returns a code only
    # when it has no count to report.
    if total is None or not 1 <= total <= limits.max_pages:
        # The API refuses an over-limit document on its bytes; this is the second
        # gate for what those bytes hid (a page tree inside an object stream), and
        # it is also what keeps a container the engine cannot decode from failing
        # an attempt with a traceback (§9.14). No page is recognized either way.
        now = clock.now()
        if now >= job.deadline_at:
            # Past the deadline the attempt publishes no readable decision (D42).
            finalize_deadline(job, now)
        else:
            _finalize_refused_input(
                job,
                now,
                page_count=total,
                # The engine's own code wins; otherwise the bound that fired does.
                error_code=refusal or (TOO_MANY_PAGES if total else PERMANENT_PAGE_ERROR),
            )
        store.publish(job)
        return True
    job.pages_total = total
    job.pages = _recognize_pages(store, engine, clock, job, content, total)

    now = clock.now()
    if now >= job.deadline_at:
        finalize_deadline(job, now)
    else:
        _finalize_result(job, now)
    if store.publish(job) and job.state == SUCCEEDED:
        # Full success: the input goes at once rather than at expiry (§6.3). Only
        # the `final.json` winner decides, so a losing attempt of a partial job
        # cannot delete the input that partial job keeps until cleanup.
        if not store.delete_input_if_present(job):
            # Momentary: reconciliation retries it, and cleanup signals it overdue
            # (§6.2, §6.3). Allowlisted fields only [D20].
            logger.error(
                "ocr_worker input deletion pending job_id=%s team_id=%s",
                job.job_id,
                job.team_id,
            )
    return True


def finalize_deadline(job: JobRecord, now: datetime) -> None:
    """Finalize at or after the deadline; never `succeeded` (D42).

    Usable pages survive the deadline: a job with at least one succeeded page is
    `partially_succeeded`, otherwise `failed`, always `deadline_exceeded` (§3).
    """
    job.state = (
        PARTIALLY_SUCCEEDED if any(page.status == SUCCEEDED for page in job.pages) else FAILED
    )
    job.reason = DEADLINE_PAGE_ERROR
    job.finalized_at = now
    job.expires_at = now + RETENTION


def create_ocr_tracer_app(
    *,
    store: DurableOcrStore,
    dispatcher: Dispatcher,
    clock: Clock,
    verifier: TokenVerifier,
    directory: TeamDirectory,
    limits: AdmissionLimits | None = None,
) -> FastAPI:
    """Build the tracer app from injected collaborators. Dev/test composition only.

    The recognition engine is owned by the injected dispatcher/worker, not by the
    API, so the API never constructs or holds one. The input and capacity bounds
    are injected (`AdmissionLimits`) and asserted at startup, so a misconfigured
    deployment fails here rather than admitting an unbounded request or set.
    """
    limits = limits or AdmissionLimits()
    assert_limits(limits)
    app = FastAPI(title="OCR v1 local lifecycle tracer (dev/test only)", version="0.1.0")
    # Ahead of every route and of multipart parsing: the body is bounded before
    # anything can spool it (contract §10/D38).
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=limits.max_upload_bytes)

    @app.exception_handler(OcrError)
    async def handle_ocr_error(request: Request, exc: OcrError) -> JSONResponse:
        # A full admission set is retryable, and the contract fixes the hint at 30 s
        # (§2.2/§6.4); every other error is decided by its own status.
        headers = {"Retry-After": "30"} if exc.status_code == 429 else None
        return _error(exc.status_code, exc.code, request, headers=headers)

    @app.exception_handler(InputRejected)
    async def handle_input_rejected(request: Request, exc: InputRejected) -> JSONResponse:
        # The input boundary's rejection, rendered with the same allowlisted body as
        # every other contract error (contract §2.3, §2.7).
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
        mime_type = validate_document(content, declared_type, limits)

        status_code, job = _submit(
            store=store,
            dispatcher=dispatcher,
            team_id=team_id,
            key_hash=_key_hash(team_id, key),
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            mime_type=mime_type,
            now=clock.now(),
            limits=limits,
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

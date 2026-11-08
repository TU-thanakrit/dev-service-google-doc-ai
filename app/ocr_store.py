"""Durable, idempotent OCR job persistence over a conditional object store.

Implements the single-object half of the reviewed protocol in
`docs/ocr-v1-contracts.md` §3.3-§5.4 over an injected object store:

- `idem/{team}/{key_hash}` is the create-once claim; replacement and deletion are
  conditional on the generation read *and* on the record still naming the job id
  that was read, so a stale actor can never remove or replace a live record.
- `jobs/{team}/{job}/job.json` is create-once and holds identity, pins,
  `accepted_at`, `deadline_at`; the only later CAS update is accepted -> running.
- `inputs/{team}/{job}` is create-once; a 412 there is success.
- `jobs/{team}/{job}/result.{fence}.json` is created once and
  `jobs/{team}/{job}/final.json` is created once: the first final writer wins and
  is the sole authority for final state and expiry.

No cross-object transaction is claimed. A crash between any two writes leaves
objects a reader ignores (an unpublished result) or that a rerun repairs (a
missing dispatch intent), which is the property the tests in
`tests/test_ocr_store.py` and `tests/test_ocr_tracer.py` pin down.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

SCHEMA_VERSION = "ocr.v1"

# Contract defaults (docs/ocr-v1-contracts.md §3.4). A claim whose job record is
# absent is treated as a crash-window retry inside this window and as a stale
# record beyond it.
MATERIALIZATION_GRACE = timedelta(minutes=5)

ACCEPTED = "accepted"
RUNNING = "running"
SUCCEEDED = "succeeded"
PARTIALLY_SUCCEEDED = "partially_succeeded"
FAILED = "failed"

FINAL_STATES = (SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED)

# One fence for this slice: leases and their fencing counter belong to the worker
# ticket. `final.json` create-if-absent is what picks a single result, and the
# result object is create-once, so a rerun after a crash reuses it.
RESULT_FENCE = 0


class StorageUnavailable(Exception):
    """The object store could not be read or written. Rendered as 503."""


class PreconditionFailed(Exception):
    """A conditional create, replace, or delete lost its race. Rendered per call."""


class CorruptRecord(Exception):
    """A stored record exists but cannot be validated. Fail closed."""


class ObjectStore(Protocol):
    """The injected storage client seam: single-object atomicity plus generations.

    `read` returns `(payload, generation)` or `None`. `create` is the
    `ifGenerationMatch=0` write and `replace`/`delete` carry a real generation.
    """

    def read(self, name: str) -> tuple[bytes, int] | None: ...

    def create(self, name: str, payload: bytes) -> int: ...

    def replace(self, name: str, payload: bytes, generation: int) -> int: ...

    def delete(self, name: str, generation: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Claim:
    """The idempotency record for one `(team_id, key)`, written create-once."""

    job_id: str
    content_hash: str
    byte_length: int
    claimed_at: datetime


@dataclass(slots=True)
class PageResult:
    page_number: int
    status: str
    text_source: str
    text: str | None
    warnings: list[str]
    error_code: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "status": self.status,
            "text_source": self.text_source,
            "text": self.text,
            "warnings": self.warnings,
            "error_code": self.error_code,
        }


@dataclass(slots=True)
class JobRecord:
    """One job's durable state, composed from `job.json`, `dispatch.json`, `final.json`."""

    job_id: str
    team_id: str
    accepted_at: datetime
    deadline_at: datetime
    content_hash: str
    byte_length: int
    key_hash: str
    mime_type: str
    engine_ref: str
    worker_revision_digest: str
    state: str = ACCEPTED
    pages_total: int | None = None
    pages: list[PageResult] = field(default_factory=list)
    execution_ref: str | None = None
    acknowledged: bool = False
    finalized_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _decode(payload: bytes, name: str) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptRecord(name) from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise CorruptRecord(name)
    return document


def _field(document: dict[str, Any], name: str, kind: type) -> Any:
    value = document.get(name)
    if not isinstance(value, kind):
        raise CorruptRecord(name)
    return value


def _job_dir(team_id: str, job_id: str) -> str:
    return f"jobs/{team_id}/{job_id}"


def _idem_name(team_id: str, key_hash: str) -> str:
    return f"idem/{team_id}/{key_hash}"


def _job_name(team_id: str, job_id: str) -> str:
    return f"{_job_dir(team_id, job_id)}/job.json"


def _input_name(team_id: str, job_id: str) -> str:
    return f"inputs/{team_id}/{job_id}"


def _dispatch_name(team_id: str, job_id: str) -> str:
    return f"{_job_dir(team_id, job_id)}/dispatch.json"


def _final_name(team_id: str, job_id: str) -> str:
    return f"{_job_dir(team_id, job_id)}/final.json"


def _result_name(team_id: str, job_id: str, fence: int) -> str:
    return f"{_job_dir(team_id, job_id)}/result.{fence}.json"


def _encode_claim(claim: Claim) -> bytes:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": claim.job_id,
            "content_hash": claim.content_hash,
            "byte_length": claim.byte_length,
            "claimed_at": _iso(claim.claimed_at),
        }
    ).encode()


def _decode_claim(payload: bytes, name: str) -> Claim:
    document = _decode(payload, name)
    return Claim(
        job_id=_field(document, "job_id", str),
        content_hash=_field(document, "content_hash", str),
        byte_length=_field(document, "byte_length", int),
        claimed_at=_parse_iso(_field(document, "claimed_at", str)),
    )


def _encode_job(job: JobRecord) -> bytes:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": job.job_id,
            "team_id": job.team_id,
            "accepted_at": _iso(job.accepted_at),
            "deadline_at": _iso(job.deadline_at),
            "content_hash": job.content_hash,
            "byte_length": job.byte_length,
            "idempotency_key_hash": job.key_hash,
            "mime_type": job.mime_type,
            "engine_ref": job.engine_ref,
            "worker_revision_digest": job.worker_revision_digest,
            "state": job.state,
        }
    ).encode()


def _decode_job(payload: bytes, name: str) -> JobRecord:
    document = _decode(payload, name)
    return JobRecord(
        job_id=_field(document, "job_id", str),
        team_id=_field(document, "team_id", str),
        accepted_at=_parse_iso(_field(document, "accepted_at", str)),
        deadline_at=_parse_iso(_field(document, "deadline_at", str)),
        content_hash=_field(document, "content_hash", str),
        byte_length=_field(document, "byte_length", int),
        key_hash=_field(document, "idempotency_key_hash", str),
        mime_type=_field(document, "mime_type", str),
        engine_ref=_field(document, "engine_ref", str),
        worker_revision_digest=_field(document, "worker_revision_digest", str),
        state=_field(document, "state", str),
    )


def _encode_result(job: JobRecord) -> bytes:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": job.job_id,
            "pages_total": job.pages_total,
            "pages": [page.as_body() for page in job.pages],
        }
    ).encode()


def _decode_result(payload: bytes, name: str) -> tuple[int | None, list[PageResult]]:
    document = _decode(payload, name)
    raw_pages = document.get("pages")
    if not isinstance(raw_pages, list):
        raise CorruptRecord(name)
    pages = []
    for raw in raw_pages:
        if not isinstance(raw, dict):
            raise CorruptRecord(name)
        warnings = raw.get("warnings")
        if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
            raise CorruptRecord(name)
        text = raw.get("text")
        error_code = raw.get("error_code")
        if text is not None and not isinstance(text, str):
            raise CorruptRecord(name)
        if error_code is not None and not isinstance(error_code, str):
            raise CorruptRecord(name)
        pages.append(
            PageResult(
                page_number=_field(raw, "page_number", int),
                status=_field(raw, "status", str),
                text_source=_field(raw, "text_source", str),
                text=text,
                warnings=warnings,
                error_code=error_code,
            )
        )
    pages_total = document.get("pages_total")
    if pages_total is not None and not isinstance(pages_total, int):
        raise CorruptRecord(name)
    return pages_total, pages


class DurableOcrStore:
    """Job state and idempotency records over one conditional object store."""

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    # --- idempotency records -------------------------------------------------

    def claim(self, team_id: str, key_hash: str) -> tuple[Claim, int] | None:
        name = _idem_name(team_id, key_hash)
        found = self.objects.read(name)
        if found is None:
            return None
        return _decode_claim(found[0], name), found[1]

    def create_claim(self, team_id: str, key_hash: str, claim: Claim) -> int:
        return self.objects.create(_idem_name(team_id, key_hash), _encode_claim(claim))

    def replace_claim(
        self,
        team_id: str,
        key_hash: str,
        claim: Claim,
        generation: int,
        expected_job_id: str,
    ) -> int:
        name = _idem_name(team_id, key_hash)
        current = self.objects.read(name)
        if current is None or current[1] != generation:
            raise PreconditionFailed(name)
        if _decode_claim(current[0], name).job_id != expected_job_id:
            raise PreconditionFailed(name)
        return self.objects.replace(name, _encode_claim(claim), current[1])

    def delete_claim(
        self, team_id: str, key_hash: str, generation: int, expected_job_id: str
    ) -> None:
        name = _idem_name(team_id, key_hash)
        current = self.objects.read(name)
        if current is None or current[1] != generation:
            raise PreconditionFailed(name)
        if _decode_claim(current[0], name).job_id != expected_job_id:
            raise PreconditionFailed(name)
        self.objects.delete(name, current[1])

    # --- job records ---------------------------------------------------------

    def create_job(self, job: JobRecord) -> tuple[int, bool]:
        """Create `job.json`, or accept an identical one a racing retry wrote.

        Returns `(generation, created_here)`: a caller that did not create the
        record must not treat it as its own to compensate for.
        """
        name = _job_name(job.team_id, job.job_id)
        try:
            return self.objects.create(name, _encode_job(job)), True
        except PreconditionFailed:
            found = self.objects.read(name)
            if found is None:
                raise
            existing = _decode_job(found[0], name)
            if (existing.job_id, existing.team_id, existing.content_hash, existing.byte_length) != (
                job.job_id,
                job.team_id,
                job.content_hash,
                job.byte_length,
            ):
                raise CorruptRecord(name) from None
            return found[1], False

    def get_job(self, team_id: str, job_id: str) -> JobRecord | None:
        name = _job_name(team_id, job_id)
        found = self.objects.read(name)
        if found is None:
            return None
        job = _decode_job(found[0], name)

        final_name = _final_name(team_id, job_id)
        final = self.objects.read(final_name)
        if final is not None:
            document = _decode(final[0], final_name)
            result_name = _field(document, "result_object", str)
            result = self.objects.read(result_name)
            if result is None:
                raise CorruptRecord(result_name)
            job.state = _field(document, "state", str)
            if job.state not in FINAL_STATES:
                raise CorruptRecord(final_name)
            reason = document.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise CorruptRecord(final_name)
            job.reason = reason
            job.finalized_at = _parse_iso(_field(document, "finalized_at", str))
            job.expires_at = _parse_iso(_field(document, "expires_at", str))
            job.pages_total, job.pages = _decode_result(result[0], result_name)

        dispatch_name = _dispatch_name(team_id, job_id)
        dispatch = self.objects.read(dispatch_name)
        if dispatch is not None:
            document = _decode(dispatch[0], dispatch_name)
            job.acknowledged = bool(document.get("acknowledged"))
            job.execution_ref = document.get("execution_ref")
        return job

    def mark_running(self, job: JobRecord) -> None:
        """The single permitted later CAS on `job.json`: accepted -> running."""
        name = _job_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            return
        current = _decode_job(found[0], name)
        if current.state != ACCEPTED:
            return
        current.state = RUNNING
        try:
            self.objects.replace(name, _encode_job(current), found[1])
        except PreconditionFailed:
            pass

    def delete_job(self, job: JobRecord) -> None:
        name = _job_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        self.objects.delete(name, found[1])

    # --- input ---------------------------------------------------------------

    def put_input(self, job: JobRecord, content: bytes) -> int:
        """Create the input object. A 412 means it is already there; size is checked."""
        name = _input_name(job.team_id, job.job_id)
        try:
            return self.objects.create(name, content)
        except PreconditionFailed:
            found = self.objects.read(name)
            if found is None or len(found[0]) != job.byte_length:
                raise CorruptRecord(name) from None
            return found[1]

    def read_input(self, team_id: str, job_id: str) -> bytes | None:
        found = self.objects.read(_input_name(team_id, job_id))
        return None if found is None else found[0]

    def delete_input(self, job: JobRecord) -> None:
        name = _input_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        self.objects.delete(name, found[1])

    # --- dispatch intent -----------------------------------------------------

    def create_dispatch(self, job: JobRecord) -> int | None:
        """Record the launch intent. Only the creator may launch (contract §6.1)."""
        document = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": job.job_id,
                "attempted_at": _iso(job.accepted_at),
                "acknowledged": False,
                "execution_ref": None,
            }
        ).encode()
        try:
            return self.objects.create(_dispatch_name(job.team_id, job.job_id), document)
        except PreconditionFailed:
            return None

    def record_dispatch_ack(self, job: JobRecord, execution_ref: str | None) -> None:
        name = _dispatch_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        document = _decode(found[0], name)
        document["acknowledged"] = execution_ref is not None
        document["execution_ref"] = execution_ref
        try:
            self.objects.replace(name, json.dumps(document).encode(), found[1])
        except PreconditionFailed:
            pass

    # --- publication ---------------------------------------------------------

    def publish(self, job: JobRecord, fence: int = RESULT_FENCE) -> bool:
        """Write the result, then `final.json`. True only for the final arbiter."""
        result_name = _result_name(job.team_id, job.job_id, fence)
        try:
            self.objects.create(result_name, _encode_result(job))
        except PreconditionFailed:
            # A previous attempt with this fence already published its result;
            # results are created once and never rewritten, so reuse it.
            found = self.objects.read(result_name)
            if found is None:
                raise
            job.pages_total, job.pages = _decode_result(found[0], result_name)

        final_name = _final_name(job.team_id, job.job_id)
        document = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "finalized_at": _iso(job.finalized_at),
                "state": job.state,
                "reason": job.reason,
                "result_object": result_name,
                "fence": fence,
                "expires_at": _iso(job.expires_at),
            }
        ).encode()
        try:
            self.objects.create(final_name, document)
        except PreconditionFailed:
            return False
        return True

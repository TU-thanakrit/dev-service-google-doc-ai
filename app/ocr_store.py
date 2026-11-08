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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

SCHEMA_VERSION = "ocr.v1"

# Contract defaults (docs/ocr-v1-contracts.md §3.4). A claim whose job record is
# absent is treated as a crash-window retry inside this window and as a stale
# record beyond it.
MATERIALIZATION_GRACE = timedelta(minutes=5)

# Lease and dispatch bounds (contract §5.3, §6.1 step 4; values benchmark-gated,
# B3). The fake engine finishes well inside one TTL, so heartbeat renewal of a
# running attempt is not implemented: shortcut -> mid-attempt lease renewal
# belongs with the real engine and its per-attempt timeout (ticket #11).
LEASE_TTL = timedelta(minutes=5)
DISPATCH_ATTEMPT_CAP = 2
DISPATCH_SPACING = timedelta(minutes=5)

ACCEPTED = "accepted"
RUNNING = "running"
SUCCEEDED = "succeeded"
PARTIALLY_SUCCEEDED = "partially_succeeded"
FAILED = "failed"

FINAL_STATES = (SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED)

# The fence of a writer that owns no worker execution (reconciliation and the
# cleanup of an unreachable job). Every execution publishes under its own lease
# fence, so its result object name is unique to that execution and is never
# rewritten by another one (§5.4). A reconciler's result is unpublished unless it
# wins `final.json`, which is the same arbiter every writer uses.
RECONCILER_FENCE = 0

# Global capacity is one serialized admission set (contract §6.4/D40): a single
# object holding every admission, keyed by `job_id`, pruned inside the same CAS
# write that acquires or releases an entry. Its sizes bound the object twice over.
CAPACITY_NAME = "capacity/inflight.json"
CAPACITY_SCHEMA_VERSION = "ocr.capacity.v1"
MAX_ENTRY_BYTES = 512
CAPACITY_OBJECT_BUDGET = 256 * 1024
# A lost CAS is re-decided, not forced; exhaustion fails closed `429` (§6.4).
MAX_CAPACITY_ATTEMPTS = 5


class StorageUnavailable(Exception):
    """The object store could not be read or written. Rendered as 503."""


class PreconditionFailed(Exception):
    """A conditional create, replace, or delete lost its race. Rendered per call."""


class CorruptRecord(Exception):
    """A stored record exists but cannot be validated. Fail closed."""


class CapacityExceeded(Exception):
    """Admission could not be recorded. Rendered as `429 capacity_exceeded`.

    `reason` names which fail-closed rule fired — `ceiling`, `corrupt`,
    `budget`, or `cas_exhausted` — for the operator log; it never reaches the
    caller, whose body is the fixed allowlisted message [D20].
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Lease:
    """One execution's claim on a job: its identity and fencing counter (§5.3)."""

    job_id: str
    owner: str
    fence: int
    lease_expires_at: datetime
    heartbeat_at: datetime
    generation: int


class ObjectStore(Protocol):
    """The injected storage client seam: single-object atomicity plus generations.

    `read` returns `(payload, generation)` or `None`. `create` is the
    `ifGenerationMatch=0` write and `replace`/`delete` carry a real generation.
    """

    def read(self, name: str) -> tuple[bytes, int] | None: ...

    def create(self, name: str, payload: bytes) -> int: ...

    def replace(self, name: str, payload: bytes, generation: int) -> int: ...

    def delete(self, name: str, generation: int) -> None: ...

    def list_names(self, prefix: str) -> list[str]: ...


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
    # The rest of the frozen pin of §8/D43. Defaults exist so a record that is
    # not launched (a store-only fixture) stays constructible; anything that
    # launches validates the whole pin with `ocr_pinning.WorkerPin` first.
    worker_job_name: str = ""
    worker_container_name: str = ""
    worker_args: tuple[str, ...] = ()
    worker_env: tuple[tuple[str, str], ...] = ()
    task_timeout_s: int = 0
    task_max_retries: int = 0
    state: str = ACCEPTED
    pages_total: int | None = None
    pages: list[PageResult] = field(default_factory=list)
    attempts: int = 0
    last_attempt_at: datetime | None = None
    execution_ref: str | None = None
    acknowledged: bool = False
    last_error_code: str | None = None
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


def _optional_int(document: dict[str, Any], name: str) -> int | None:
    """An optional integer field: absent or `null` is `None`, anything else fails."""
    value = document.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise CorruptRecord(name)
    return value


def _string_tuple(document: dict[str, Any], name: str) -> tuple[str, ...]:
    value = document.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorruptRecord(name)
    return tuple(value)


def _env_pairs(document: dict[str, Any], name: str) -> tuple[tuple[str, str], ...]:
    value = document.get(name)
    if not isinstance(value, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(item, str) for item in pair)
        for pair in value
    ):
        raise CorruptRecord(name)
    return tuple((pair[0], pair[1]) for pair in value)


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


def _lease_name(team_id: str, job_id: str) -> str:
    return f"{_job_dir(team_id, job_id)}/claim.json"


def _final_name(team_id: str, job_id: str) -> str:
    return f"{_job_dir(team_id, job_id)}/final.json"


def _result_name(team_id: str, job_id: str, fence: int) -> str:
    return f"{_job_dir(team_id, job_id)}/result.{fence}.json"


def _page_name(team_id: str, job_id: str, page_number: int) -> str:
    return f"{_job_dir(team_id, job_id)}/pages/{page_number:05d}.json"


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


def _encode_lease(lease: Lease) -> bytes:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": lease.job_id,
            "owner": lease.owner,
            "fence": lease.fence,
            "lease_expires_at": _iso(lease.lease_expires_at),
            "heartbeat_at": _iso(lease.heartbeat_at),
        }
    ).encode()


def _decode_lease(payload: bytes, name: str, generation: int) -> Lease:
    document = _decode(payload, name)
    return Lease(
        job_id=_field(document, "job_id", str),
        owner=_field(document, "owner", str),
        fence=_field(document, "fence", int),
        lease_expires_at=_parse_iso(_field(document, "lease_expires_at", str)),
        heartbeat_at=_parse_iso(_field(document, "heartbeat_at", str)),
        generation=generation,
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
            "worker_job_name": job.worker_job_name,
            "worker_container_name": job.worker_container_name,
            "worker_args": list(job.worker_args),
            "worker_env": [list(pair) for pair in job.worker_env],
            "task_timeout_s": job.task_timeout_s,
            "task_max_retries": job.task_max_retries,
            # The admission byte scan's page count when it was known; `null` when
            # the page tree was invisible to it. Written once with the record so a
            # refusal can account for the source pages it knows about.
            "pages_total": job.pages_total,
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
        worker_job_name=_field(document, "worker_job_name", str),
        worker_container_name=_field(document, "worker_container_name", str),
        worker_args=_string_tuple(document, "worker_args"),
        worker_env=_env_pairs(document, "worker_env"),
        task_timeout_s=_field(document, "task_timeout_s", int),
        task_max_retries=_field(document, "task_max_retries", int),
        pages_total=_optional_int(document, "pages_total"),
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


def _encode_page(job: JobRecord, page: PageResult) -> bytes:
    """One succeeded page checkpoint, carrying the pin that produced it (D41)."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": job.job_id,
            "page_number": page.page_number,
            "status": page.status,
            "text_source": page.text_source,
            "text": page.text,
            "warnings": page.warnings,
            "engine_ref": job.engine_ref,
        }
    ).encode()


def _decode_page(
    payload: bytes, name: str, job: JobRecord, page_number: int
) -> PageResult | None:
    """A checkpoint accepted only if it matches the name, the pin, and the shape.

    `None` means invalid, which the caller treats as absent: it may redo the page
    and, only conditionally on the generation it read, replace the object (§5.5,
    D41). Never raises for content, so a stale or foreign checkpoint cannot fail a
    whole attempt.
    """
    try:
        document = _decode(payload, name)
        stored_job = _field(document, "job_id", str)
        stored_page = _field(document, "page_number", int)
        stored_engine = _field(document, "engine_ref", str)
        status = _field(document, "status", str)
        text_source = _field(document, "text_source", str)
    except CorruptRecord:
        return None
    text = document.get("text")
    warnings = document.get("warnings")
    if (stored_job, stored_page, stored_engine, status) != (
        job.job_id,
        page_number,
        job.engine_ref,
        SUCCEEDED,
    ):
        return None
    if not isinstance(text, str):
        return None
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        return None
    return PageResult(
        page_number=page_number,
        status=SUCCEEDED,
        text_source=text_source,
        text=text,
        warnings=list(warnings),
    )


def _encode_capacity(entries: dict[str, dict[str, datetime]], now: datetime) -> bytes:
    return json.dumps(
        {
            "schema_version": CAPACITY_SCHEMA_VERSION,
            "updated_at": _iso(now),
            "entries": {
                job_id: {
                    "admitted_at": _iso(entry["admitted_at"]),
                    "deadline_at": _iso(entry["deadline_at"]),
                    "expires_at": _iso(entry["expires_at"]),
                }
                for job_id, entry in entries.items()
            },
        }
    ).encode()


def _decode_capacity(payload: bytes, name: str) -> dict[str, dict[str, datetime]]:
    """The admission set, or `CapacityExceeded` when it cannot be trusted.

    An unparseable, non-UTF-8, or schema-mismatched object fails closed with no
    automatic rebuild: any rebuild that could read the set low would reintroduce
    the undercount the set exists to remove (§6.4, D40).
    """
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityExceeded("corrupt") from exc
    if not isinstance(document, dict) or document.get("schema_version") != CAPACITY_SCHEMA_VERSION:
        raise CapacityExceeded("corrupt")
    raw = document.get("entries")
    if not isinstance(raw, dict):
        raise CapacityExceeded("corrupt")
    entries: dict[str, dict[str, datetime]] = {}
    for job_id, entry in raw.items():
        if not isinstance(job_id, str) or not isinstance(entry, dict):
            raise CapacityExceeded("corrupt")
        try:
            entries[job_id] = {
                "admitted_at": _parse_iso(_field(entry, "admitted_at", str)),
                "deadline_at": _parse_iso(_field(entry, "deadline_at", str)),
                "expires_at": _parse_iso(_field(entry, "expires_at", str)),
            }
        except (CorruptRecord, ValueError) as exc:
            raise CapacityExceeded("corrupt") from exc
    return entries


def _live_entries(
    entries: dict[str, dict[str, datetime]], now: datetime
) -> dict[str, dict[str, datetime]]:
    """Drop every entry whose own expiry has passed (§6.4): pruning only shrinks."""
    return {job_id: entry for job_id, entry in entries.items() if entry["expires_at"] > now}


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
        """One job's durable state, including its published result's pages."""
        return self._read_job(team_id, job_id, with_result=True)

    def get_job_meta(self, team_id: str, job_id: str) -> JobRecord | None:
        """`job.json` + `final.json` + `dispatch.json`, without the result object.

        The expiry gate decides from these create-once records alone, before any
        content object is read (§3.3/D39), so a read arriving while cleanup is
        partway through still denies instead of failing on deleted content.
        """
        return self._read_job(team_id, job_id, with_result=False)

    def read_result(self, job: JobRecord) -> None:
        """Load the pages of the result `final.json` names onto the record.

        Only meaningful after the gate has served: inside the retention window a
        missing published result is corruption, not expiry, and fails closed with
        the API's 500 plus an alert (§2.2, §5.4).
        """
        final_name = _final_name(job.team_id, job.job_id)
        found = self.objects.read(final_name)
        if found is None:
            raise CorruptRecord(final_name)
        result_name = _field(_decode(found[0], final_name), "result_object", str)
        result = self.objects.read(result_name)
        if result is None:
            raise CorruptRecord(result_name)
        job.pages_total, job.pages = _decode_result(result[0], result_name)

    def _read_job(
        self, team_id: str, job_id: str, *, with_result: bool
    ) -> JobRecord | None:
        name = _job_name(team_id, job_id)
        found = self.objects.read(name)
        if found is None:
            return None
        job = _decode_job(found[0], name)

        final_name = _final_name(team_id, job_id)
        final = self.objects.read(final_name)
        if final is not None:
            document = _decode(final[0], final_name)
            _field(document, "result_object", str)
            job.state = _field(document, "state", str)
            if job.state not in FINAL_STATES:
                raise CorruptRecord(final_name)
            reason = document.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise CorruptRecord(final_name)
            job.reason = reason
            job.finalized_at = _parse_iso(_field(document, "finalized_at", str))
            job.expires_at = _parse_iso(_field(document, "expires_at", str))
            if with_result:
                self.read_result(job)

        dispatch_name = _dispatch_name(team_id, job_id)
        dispatch = self.objects.read(dispatch_name)
        if dispatch is not None:
            document = _decode(dispatch[0], dispatch_name)
            attempts = document.get("attempts")
            if not isinstance(attempts, int):
                raise CorruptRecord(dispatch_name)
            last_attempt_at = document.get("last_attempt_at")
            job.attempts = attempts
            job.last_attempt_at = (
                None
                if last_attempt_at is None
                else _parse_iso(_field(document, "last_attempt_at", str))
            )
            job.acknowledged = bool(document.get("acknowledged"))
            job.execution_ref = document.get("execution_ref")
            job.last_error_code = document.get("last_error_code")
        return job

    def mark_running(self, job: JobRecord, now: datetime) -> None:
        """The single permitted later CAS on `job.json`: accepted -> running.

        Refused for a job that is already final or at or past `deadline_at`, so a
        terminated or late worker cannot make it reappear as running (§5.5, D42).
        """
        if now >= job.deadline_at or self.finalized(job):
            return
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

    def finalized(self, job: JobRecord) -> bool:
        """Whether `final.json` exists: the arbiter's authority while it lasts.

        Read at the moment of the decision, so a straggler decides from the record
        and not from a cached copy of the job (§5.4, §5.5).
        """
        return self.objects.read(_final_name(job.team_id, job.job_id)) is not None

    def delete_job(self, job: JobRecord) -> None:
        name = _job_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        self.objects.delete(name, found[1])

    def list_jobs(self) -> list[JobRecord]:
        """Every job of every team, for reconciliation (an operator-side scan).

        A record that cannot be validated raises `CorruptRecord`: the caller
        fails closed for the pass rather than reconciling from an undecidable
        record. Enumeration reads `job.json` only; `get_job` adds dispatch and
        final state.
        """
        jobs: list[JobRecord] = []
        for team_id, job_id in self.list_job_ids():
            job = self.get_job(team_id, job_id)
            if job is not None:
                jobs.append(job)
        return jobs

    def list_job_ids(self) -> list[tuple[str, str]]:
        """Every `(team_id, job_id)` in the bucket, for the cleanup scan (§6.3).

        Enumeration only, so the caller decides each job on its own: one record it
        cannot read fails that job's pass instead of the whole scan.
        """
        ids: list[tuple[str, str]] = []
        for name in self.objects.list_names("jobs/"):
            parts = name.split("/")
            if len(parts) == 4 and parts[3] == "job.json":
                ids.append((parts[1], parts[2]))
        return ids

    # --- lease ---------------------------------------------------------------

    def read_lease(self, job: JobRecord) -> Lease | None:
        name = _lease_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            return None
        return _decode_lease(found[0], name, found[1])

    def live_lease(self, job: JobRecord, now: datetime) -> Lease | None:
        """The job's lease while it is still live, else `None` (contract §5.3)."""
        lease = self.read_lease(job)
        return lease if lease is not None and lease.lease_expires_at > now else None

    def acquire_lease(
        self, job: JobRecord, *, owner: str, now: datetime, ttl: timedelta = LEASE_TTL
    ) -> Lease | None:
        """Claim the job for one execution (contract §5.3).

        Create-once with `fence: 1`; on `412` take over an expired lease by
        CAS-replacing it at the generation read with `fence + 1`, or return
        `None` when another execution still holds a live lease. Only the creator
        of the lease may launch, and a `None` here means a duplicate launch must
        write nothing and exit.
        """
        name = _lease_name(job.team_id, job.job_id)
        fresh = Lease(
            job_id=job.job_id,
            owner=owner,
            fence=1,
            lease_expires_at=now + ttl,
            heartbeat_at=now,
            generation=0,
        )
        try:
            return replace(fresh, generation=self.objects.create(name, _encode_lease(fresh)))
        except PreconditionFailed:
            pass
        current = self.objects.read(name)
        if current is None:
            return None
        held = _decode_lease(current[0], name, current[1])
        if held.lease_expires_at > now:
            return None
        taken = replace(
            held,
            owner=owner,
            fence=held.fence + 1,
            lease_expires_at=now + ttl,
            heartbeat_at=now,
        )
        try:
            generation = self.objects.replace(name, _encode_lease(taken), current[1])
            return replace(taken, generation=generation)
        except PreconditionFailed:
            return None

    def renew_lease(
        self, job: JobRecord, lease: Lease, *, now: datetime, ttl: timedelta = LEASE_TTL
    ) -> Lease | None:
        """Extend a held lease at the generation last read (contract §5.3 step 3).

        The renewal keeps `owner` and `fence`: it is the same execution, not a
        takeover. A `412` means another execution took the lease over, so this
        holder no longer owns the job and must stop writing; the caller maps that
        `None` onto "write nothing and exit non-zero". A missing object is the
        same conclusion, because its lease was released or removed.
        """
        name = _lease_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None or found[1] != lease.generation:
            return None
        held = _decode_lease(found[0], name, found[1])
        if (held.owner, held.fence) != (lease.owner, lease.fence):
            return None
        renewed = replace(held, lease_expires_at=now + ttl, heartbeat_at=now)
        try:
            generation = self.objects.replace(name, _encode_lease(renewed), found[1])
        except PreconditionFailed:
            return None
        return replace(renewed, generation=generation)

    def release_lease(self, job: JobRecord, lease: Lease) -> None:
        """Conditional delete of this execution's own lease, on completion or give-up.

        The delete is conditional on the generation read **now** and on the record
        still naming this holder (`owner`, `fence`), because a renewal (the
        worker's heartbeat) legitimately moves the object past the generation this
        `Lease` was created at. A renamed fence means another execution took the
        lease over, and a `412` means it moved again: both are skipped, never
        forced, so a superseded holder cannot delete the live holder's claim
        (contract §5.3 step 4).
        """
        name = _lease_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            return
        held = _decode_lease(found[0], name, found[1])
        if (held.owner, held.fence) != (lease.owner, lease.fence):
            return
        try:
            self.objects.delete(name, found[1])
        except PreconditionFailed:
            pass

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

    def delete_input_if_present(self, job: JobRecord) -> bool:
        """Delete the input when it is present; `True` means it was deleted now.

        Used both for the prompt deletion after a full success and for the
        compensation path, so it never raises for an absent object. `False` covers
        "nothing was there" and "another actor replaced it since this read": the
        delete is skipped, not forced, and the next pass re-decides (§6.3).
        """
        name = _input_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            return False
        try:
            self.objects.delete(name, found[1])
        except PreconditionFailed:
            return False
        return True

    # --- page checkpoints ----------------------------------------------------

    def read_page_checkpoint(self, job: JobRecord, page_number: int) -> PageResult | None:
        """The job's valid checkpoint for one page, or `None` if absent/invalid."""
        name = _page_name(job.team_id, job.job_id, page_number)
        found = self.objects.read(name)
        if found is None:
            return None
        return _decode_page(found[0], name, job, page_number)

    def put_page_checkpoint(self, job: JobRecord, page: PageResult, now: datetime) -> None:
        """Write one succeeded page create-if-absent; never replace a valid one.

        A `412` means the page is already checkpointed: a valid existing object
        wins, so a duplicate or recovering worker cannot overwrite a page that
        already succeeded. Only an object that fails validation may be replaced,
        conditionally on the generation just read (§5.5, D41).

        A write at or after `deadline_at`, or after the job finalized, is refused
        rather than stored: page work ends at the deadline and the published result
        is never extended by a late worker (§5.5, D42).
        """
        if now >= job.deadline_at or self.finalized(job):
            return
        name = _page_name(job.team_id, job.job_id, page.page_number)
        payload = _encode_page(job, page)
        try:
            self.objects.create(name, payload)
            return
        except PreconditionFailed:
            pass
        found = self.objects.read(name)
        if found is None:
            return
        if _decode_page(found[0], name, job, page.page_number) is not None:
            return
        try:
            self.objects.replace(name, payload, found[1])
        except PreconditionFailed:
            pass

    def read_page_checkpoints(self, job: JobRecord) -> list[PageResult]:
        """Every valid page checkpoint of the job, in page order (§5.5, D41).

        The deadline finalization has no engine, so it can credit only the pages a
        durable checkpoint proves; a page that failed or was never reached is not
        inventable here and is accounted for by the attempt that ran (contract
        §6.2, issue #9).
        """
        prefix = f"{_job_dir(job.team_id, job.job_id)}/pages/"
        pages: list[PageResult] = []
        for name in self.objects.list_names(prefix):
            stem = name[len(prefix):]
            if not stem.endswith(".json") or not stem[:-5].isdigit():
                continue
            found = self.objects.read(name)
            if found is None:
                continue
            page = _decode_page(found[0], name, job, int(stem[:-5]))
            if page is not None:
                pages.append(page)
        return sorted(pages, key=lambda page: page.page_number)

    # --- dispatch intent -----------------------------------------------------

    def create_dispatch(self, job: JobRecord) -> int | None:
        """Record the launch intent. Only the creator may launch (contract §6.1)."""
        document = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": job.job_id,
                "attempts": 1,
                "last_attempt_at": _iso(job.accepted_at),
                "acknowledged": False,
                "execution_ref": None,
                "last_error_code": None,
            }
        ).encode()
        try:
            return self.objects.create(_dispatch_name(job.team_id, job.job_id), document)
        except PreconditionFailed:
            return None

    def record_dispatch_ack(self, job: JobRecord, execution_ref: str | None) -> None:
        """Record the launch outcome. Acknowledgment is diagnostic, not completion.

        `acknowledged` is false for both 'no launch accepted yet' and 'persisted
        after a failed launch' [D31]; OCR completion is observed only from
        `final.json` (contract §6.1).
        """
        name = _dispatch_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        document = _decode(found[0], name)
        document["acknowledged"] = execution_ref is not None
        document["execution_ref"] = execution_ref
        document["last_error_code"] = None
        try:
            self.objects.replace(name, json.dumps(document).encode(), found[1])
        except PreconditionFailed:
            pass

    def record_dispatch_error(self, job: JobRecord, error_code: str) -> None:
        """Record a non-retryable launch error; reconciliation finalizes the job."""
        name = _dispatch_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            raise PreconditionFailed(name)
        document = _decode(found[0], name)
        document["acknowledged"] = False
        document["execution_ref"] = None
        document["last_error_code"] = error_code
        try:
            self.objects.replace(name, json.dumps(document).encode(), found[1])
        except PreconditionFailed:
            pass

    def claim_next_attempt(self, job: JobRecord, now: datetime) -> int | None:
        """Claim one further dispatch attempt, or `None` (contract §6.1 step 4).

        A further attempt needs a non-final job past `DISPATCH_SPACING` since the
        last attempt with attempts left under `DISPATCH_ATTEMPT_CAP`; the lease
        check is the caller's, because only it knows the clock and the job. The
        CAS on the generation read is what stops two reconcilers launching twice.
        """
        name = _dispatch_name(job.team_id, job.job_id)
        found = self.objects.read(name)
        if found is None:
            return None
        document = _decode(found[0], name)
        attempts = document.get("attempts")
        if not isinstance(attempts, int) or attempts >= DISPATCH_ATTEMPT_CAP:
            return None
        last_attempt_at = document.get("last_attempt_at")
        if last_attempt_at is not None:
            if now - _parse_iso(_field(document, "last_attempt_at", str)) < DISPATCH_SPACING:
                return None
        document.update(
            attempts=attempts + 1,
            last_attempt_at=_iso(now),
            acknowledged=False,
            execution_ref=None,
            last_error_code=None,
        )
        try:
            return self.objects.replace(name, json.dumps(document).encode(), found[1])
        except PreconditionFailed:
            return None

    # --- global capacity: one serialized admission set ------------------------

    def acquire_admission(
        self,
        job_id: str,
        *,
        deadline_at: datetime,
        now: datetime,
        ceiling: int,
        per_attempt_timeout: timedelta,
    ) -> None:
        """Ensure the admission set holds this job, or fail closed (§6.4, D40).

        Read, prune, and CAS-write in one conditional write: an entry that is
        already present is an idempotent success (a retry of an admitted job takes
        no second slot), a full set is `CapacityExceeded`, and a lost race is
        re-decided rather than forced. There is no enumerating recount anywhere, so
        no path can read the set low; the only drift the protocol can produce is
        over-reservation.
        """
        for _ in range(MAX_CAPACITY_ATTEMPTS):
            found = self.objects.read(CAPACITY_NAME)
            entries = {} if found is None else _decode_capacity(found[0], CAPACITY_NAME)
            entries = _live_entries(entries, now)
            if job_id in entries:
                return
            if len(entries) >= ceiling:
                raise CapacityExceeded("ceiling")
            entries[job_id] = {
                "admitted_at": now,
                "deadline_at": deadline_at,
                # Self-limiting: past this instant the job can run no page work at
                # all (§5.5), so the reservation expires with it.
                "expires_at": deadline_at + per_attempt_timeout,
            }
            payload = _encode_capacity(entries, now)
            if len(payload) > CAPACITY_OBJECT_BUDGET:
                raise CapacityExceeded("budget")
            try:
                if found is None:
                    self.objects.create(CAPACITY_NAME, payload)
                else:
                    self.objects.replace(CAPACITY_NAME, payload, found[1])
            except PreconditionFailed:
                continue
            return
        raise CapacityExceeded("cas_exhausted")

    def release_admission(self, job_id: str, *, now: datetime) -> None:
        """Remove this job's admission; a lost release is not a failure (§5.4).

        Only the `final.json` winner releases, and a release that cannot be written
        is left to the entry's own expiry or to reconciliation: nothing about a
        job's outcome depends on it. An object that cannot be decoded is never
        rewritten, because rewriting it from a partial read is exactly the
        undercount the set exists to remove.
        """
        for _ in range(MAX_CAPACITY_ATTEMPTS):
            found = self.objects.read(CAPACITY_NAME)
            if found is None:
                return
            try:
                entries = _decode_capacity(found[0], CAPACITY_NAME)
            except CapacityExceeded:
                return
            live = _live_entries(entries, now)
            if job_id not in live and live == entries:
                return
            live.pop(job_id, None)
            try:
                self.objects.replace(CAPACITY_NAME, _encode_capacity(live, now), found[1])
            except PreconditionFailed:
                continue
            return

    # --- publication ---------------------------------------------------------

    def publish(self, job: JobRecord, *, fence: int) -> bool:
        """Write the result, then `final.json`. True only for the final arbiter.

        The result object is named for the writer's fence and created once, so
        each execution publishes its own object and can never rewrite another
        execution's result. `final.json` create-if-absent is the only arbiter and
        names the result object the winner wrote; a loser's object stays
        unpublished until cleanup enumerates it (§5.4, §6.3).
        """
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
        # Only the winner of the `final.json` arbitration may release (§5.4 step 3),
        # and a release that cannot be written is not a job failure: the entry
        # self-expires, and reconciliation removes a final job's entry (§6.2, D40).
        try:
            self.release_admission(job.job_id, now=job.finalized_at or job.deadline_at)
        except StorageUnavailable:
            pass
        return True

    # --- cleanup -------------------------------------------------------------

    def purge_job(self, job: JobRecord) -> bool:
        """Delete one job's objects in the reviewed order (D37); idempotent.

        Content first — `inputs/…`, every `pages/*.json`, and every
        `result.*.json`, including results from attempts that never won
        `final.json` — then `claim.json`, `dispatch.json`, `final.json`, then
        `job.json`, and the `idem` record last. Every delete is conditional on the
        generation read, so one that loses its race is skipped rather than forced
        and the next pass re-decides. `True` only when nothing of the job is left.
        """
        complete = True
        for name in self._job_object_names(job):
            if not self._purge(name):
                complete = False
        claim_gone = self._purge_claim(job)
        return complete and claim_gone

    def _job_object_names(self, job: JobRecord) -> list[str]:
        """Every object to delete for one job, in the order §6.3 requires (D37)."""
        job_dir = _job_dir(job.team_id, job.job_id)
        return [
            _input_name(job.team_id, job.job_id),
            *self.objects.list_names(f"{job_dir}/pages/"),
            *self.objects.list_names(f"{job_dir}/result."),
            _lease_name(job.team_id, job.job_id),
            _dispatch_name(job.team_id, job.job_id),
            _final_name(job.team_id, job.job_id),
            _job_name(job.team_id, job.job_id),
        ]

    def _purge(self, name: str) -> bool:
        """Delete one object at the generation read; `True` when it is gone."""
        found = self.objects.read(name)
        if found is None:
            return True
        try:
            self.objects.delete(name, found[1])
        except PreconditionFailed:
            return False
        return True

    def _purge_claim(self, job: JobRecord) -> bool:
        """Delete the `idem` record only while it still names this job (D44).

        A name-addressed delete would remove a *live* record a later submission
        reused for a *new* job, losing deduplication and stranding that job. The
        delete therefore requires both the generation read and the job id the
        record still names; a mismatch is a skip, not a retry.
        """
        found = self.claim(job.team_id, job.key_hash)
        if found is None:
            return True
        record, generation = found
        if record.job_id != job.job_id:
            return True
        try:
            self.delete_claim(job.team_id, job.key_hash, generation, job.job_id)
        except PreconditionFailed:
            return False
        return True

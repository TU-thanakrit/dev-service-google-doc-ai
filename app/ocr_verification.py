"""Verification of one OCR v1 result body against the contract (§2.5, §2.6).

The deployed service is verified by a caller exercising the public lifecycle, so
the check has to happen on the caller's side of the wire: this module decides
whether a returned result body is exactly the contract's shape and returns the
disagreements as strings a human reads. It is the offline half of
`scripts/verify_ocr_service.py`, and it is deliberately not a parser: nothing here
repairs or fills in a missing field.

`summarize_result_body` returns metadata and per-page status only, never document
text, so a verification report cannot leak a payload by accident. The body is
untrusted: every value a report copies back — warnings, error codes, reasons,
states, job ids — is a closed vocabulary or is redacted, so a malicious or buggy
response cannot smuggle a path, a token, or document text into the report.
"""

import re
from typing import Any

SCHEMA_VERSION = "ocr.v1"
RESULT_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "state",
        "reason",
        "completeness",
        "accuracy",
        "page_count",
        "pages",
        "warnings",
        "timestamps",
    }
)
# Present only when at least one page succeeded (§2.5).
OPTIONAL_RESULT_KEYS = frozenset({"text"})
PAGE_KEYS = frozenset(
    {"page_number", "status", "text_source", "text", "warnings", "error_code"}
)
TIMESTAMP_KEYS = frozenset({"accepted_at", "finalized_at", "expires_at"})
FINAL_STATES = frozenset({"succeeded", "partially_succeeded", "failed"})
REASONS = frozenset(
    {"deadline_exceeded", "engine_unavailable", "input_unreadable", "internal_error"}
)
PAGE_STATES = frozenset({"succeeded", "failed"})
TEXT_SOURCES = frozenset({"embedded", "ocr"})
# §2.6's warning vocabulary and §2.7's page-error vocabulary: the only values a
# report may copy from a response without redacting them.
WARNINGS = frozenset({"no_text", "possible_low_quality"})
PAGE_ERROR_CODES = frozenset({"page_engine_error", "malformed_document"})
# §2.7's HTTP error vocabulary. A rejected submission's code is copied only when
# it is one of these; anything else is a value the report must not echo.
HTTP_ERROR_CODES = frozenset(
    {
        "unsupported_media_type",
        "file_too_large",
        "too_many_pages",
        "empty_document",
        "malformed_document",
        "encrypted_document",
        "invalid_idempotency_key",
        "idempotency_conflict",
        "unauthorized_caller",
        "team_not_authorized",
        "not_found",
        "job_expired",
        "capacity_exceeded",
        "storage_unavailable",
        "deadline_exceeded",
        "page_engine_error",
        "engine_unavailable",
        "internal_error",
    }
)
# What a redacted value is replaced with, so a report says "not copied" rather
# than silently dropping the field and looking complete.
REDACTED = "redacted"
# A job id is a server-generated UUID (§2.4); anything else is untrusted text.
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
# Fields of the retired synchronous expense contract. Their presence would mean the
# old extraction path answered instead of the OCR service (issue #12).
FORBIDDEN_KEYS = ("สิ่งที่อ่าน", "Analysis", "Type of Expense", "engine_ref")


def _observed(value: Any) -> str:
    """A safe description of an untrusted value: its shape, never its text.

    The verifier's error strings are printed and written to the report file, so a
    body that carries a path, a token, or document text in a wrongly-typed field
    must not have that text echoed back. An int, bool, or null is reported as
    itself — those cannot carry text — and anything else by type and size.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return repr(value)
    if isinstance(value, str):
        return f"a string of length {len(value)}"
    if isinstance(value, list):
        return f"a list of {len(value)} entries"
    if isinstance(value, dict):
        return f"an object of {len(value)} keys"
    return type(value).__name__


def _allowed(value: Any, allowed: frozenset[str]) -> Any:
    """`value` when the contract names it, else the redaction marker."""
    return value if value is None or value in allowed else REDACTED


def _allowed_list(values: Any, allowed: frozenset[str]) -> Any:
    """A warning list filtered to the closed vocabulary, order preserved."""
    if not isinstance(values, list):
        return values
    return [item if item in allowed else REDACTED for item in values]


def _verify_page(page, index: int, errors: list[str]) -> str | None:
    """Check one page object and return its status, or `None` if unusable."""
    number = index + 1
    if not isinstance(page, dict):
        errors.append(f"page {number}: not a JSON object")
        return None
    extra = set(page) - PAGE_KEYS
    missing = PAGE_KEYS - set(page)
    if missing:
        errors.append(f"page {number}: missing keys {sorted(missing)}")
    if extra:
        # The unexpected key *names* come from the untrusted body, so only the
        # count is reported; the contract's own missing names are safe to list.
        errors.append(f"page {number}: {len(extra)} unexpected keys")
    if page.get("page_number") != number:
        errors.append(f"page {number}: page_number is {_observed(page.get('page_number'))}")
    if page.get("text_source") not in TEXT_SOURCES:
        errors.append(f"page {number}: text_source is {_observed(page.get('text_source'))}")
    if not isinstance(page.get("warnings"), list):
        errors.append(f"page {number}: warnings is not a list")

    status = page.get("status")
    if status not in PAGE_STATES:
        errors.append(f"page {number}: status is {_observed(status)}")
        return None
    if status == "failed":
        # A failed page is visible, never dropped, and carries no text (§2.6).
        if page.get("text") is not None:
            errors.append(f"page {number}: a failed page must have a null text")
        if not page.get("error_code"):
            errors.append(f"page {number}: a failed page needs an error_code")
    else:
        if not isinstance(page.get("text"), str):
            errors.append(f"page {number}: a succeeded page needs a string text")
        if page.get("error_code") is not None:
            errors.append(f"page {number}: a succeeded page must have a null error_code")
    return status


def verify_result_body(payload) -> list[str]:
    """The result body's disagreements with the contract; empty means it conforms."""
    if not isinstance(payload, dict):
        return ["result body is not a JSON object"]

    errors: list[str] = []
    for key in FORBIDDEN_KEYS:
        if key in payload:
            errors.append(f"retired expense/engine field present: {key}")
    missing = RESULT_KEYS - set(payload)
    if missing:
        errors.append(f"missing result keys: {sorted(missing)}")
    unexpected = set(payload) - RESULT_KEYS - OPTIONAL_RESULT_KEYS
    if unexpected:
        errors.append(f"{len(unexpected)} unexpected result keys")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version is {_observed(payload.get('schema_version'))}")
    if not payload.get("job_id"):
        errors.append("job_id is empty")

    state = payload.get("state")
    if state not in FINAL_STATES:
        errors.append(f"state is not a final state: {_observed(state)}")
    reason = payload.get("reason")
    if reason is not None and reason not in REASONS:
        errors.append(f"reason is not a contract value: {_observed(reason)}")
    if (reason is None) != (state == "succeeded"):
        errors.append("reason must be null exactly when the state is succeeded")
    if payload.get("accuracy") != "not_verified":
        # Execution success is never accuracy (§2.5).
        errors.append(f"accuracy is {_observed(payload.get('accuracy'))}")

    pages = payload.get("pages")
    statuses: list[str | None] = []
    if not isinstance(pages, list):
        errors.append("pages is not a list")
        pages = []
    else:
        statuses = [_verify_page(page, index, errors) for index, page in enumerate(pages)]
    page_count = payload.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool):
        errors.append(f"page_count is not an integer: {_observed(page_count)}")
    elif page_count != len(pages):
        # One accounted-for entry per source page, failed pages included (§2.6).
        errors.append(f"page_count {page_count} does not match {len(pages)} page entries")

    succeeded_texts = [
        page["text"]
        for page, status in zip(pages, statuses, strict=True)
        if status == "succeeded" and isinstance(page, dict) and isinstance(page.get("text"), str)
    ]
    if succeeded_texts:
        expected_text = "\n".join(succeeded_texts)
        if payload.get("text") != expected_text:
            errors.append("text is not the successful pages joined in page order")
    elif "text" in payload:
        errors.append("text is present with no successful page")

    expected_warnings: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            # Already reported by `_verify_page`; nothing to union from it.
            continue
        for warning in page.get("warnings") or []:
            if warning not in expected_warnings:
                expected_warnings.append(warning)
    if payload.get("warnings") != expected_warnings:
        errors.append(f"warnings are not the page warnings in page order: {expected_warnings}")

    complete = bool(pages) and state == "succeeded" and all(
        status == "succeeded" for status in statuses
    )
    if payload.get("completeness") != ("complete" if complete else "partial"):
        errors.append(f"completeness is {payload.get('completeness')!r}")

    timestamps = payload.get("timestamps")
    if not isinstance(timestamps, dict):
        errors.append("timestamps is not a JSON object")
    else:
        missing = TIMESTAMP_KEYS - set(timestamps)
        unexpected = set(timestamps) - TIMESTAMP_KEYS
        if missing:
            errors.append(f"timestamps missing keys {sorted(missing)}")
        if unexpected:
            errors.append(f"{len(unexpected)} unexpected timestamp keys")
        if not all(isinstance(value, str) and value for value in timestamps.values()):
            errors.append("a timestamp is empty or not a string")
    return errors


def summarize_result_body(payload: dict) -> dict:
    """A redacted view of a result body: metadata, counts, and no document text.

    The body is external input. Only a value the contract's closed vocabulary
    names is copied back; anything else becomes `REDACTED`, so this view cannot
    carry a path, a token, or document text into a report that is printed and
    written to a file.
    """
    pages = payload.get("pages")
    pages = pages if isinstance(pages, list) else []
    text = payload.get("text")
    return {
        "schema_version": _allowed(payload.get("schema_version"), frozenset({SCHEMA_VERSION})),
        "job_id": payload.get("job_id")
        if isinstance(payload.get("job_id"), str)
        and UUID_PATTERN.fullmatch(payload["job_id"])
        else REDACTED,
        "state": _allowed(payload.get("state"), FINAL_STATES),
        "reason": _allowed(payload.get("reason"), REASONS),
        "completeness": _allowed(payload.get("completeness"), frozenset({"complete", "partial"})),
        "accuracy": _allowed(payload.get("accuracy"), frozenset({"not_verified"})),
        "page_count": payload.get("page_count")
        if isinstance(payload.get("page_count"), int)
        and not isinstance(payload.get("page_count"), bool)
        else REDACTED,
        "text_length": len(text) if isinstance(text, str) else 0,
        "warnings": _allowed_list(payload.get("warnings"), WARNINGS),
        "timestamps": _summarize_timestamps(payload.get("timestamps")),
        "pages": [
            {
                "page_number": page.get("page_number")
                if isinstance(page.get("page_number"), int)
                and not isinstance(page.get("page_number"), bool)
                else REDACTED,
                "status": _allowed(page.get("status"), PAGE_STATES),
                "text_source": _allowed(page.get("text_source"), TEXT_SOURCES),
                "text_length": len(page.get("text"))
                if isinstance(page.get("text"), str)
                else 0,
                "warnings": _allowed_list(page.get("warnings"), WARNINGS),
                "error_code": _allowed(page.get("error_code"), PAGE_ERROR_CODES),
            }
            for page in pages
            if isinstance(page, dict)
        ],
    }


def _summarize_timestamps(timestamps: Any) -> Any:
    """The three contract timestamps, each kept only when it is an ISO instant."""
    if not isinstance(timestamps, dict):
        return REDACTED
    summary: dict[str, Any] = {}
    for key in TIMESTAMP_KEYS:
        value = timestamps.get(key)
        summary[key] = (
            value
            if isinstance(value, str) and ISO_TIMESTAMP.fullmatch(value)
            else REDACTED
        )
    return summary

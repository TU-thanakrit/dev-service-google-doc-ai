"""Verification of one OCR v1 result body against the contract (§2.5, §2.6).

The deployed service is verified by a caller exercising the public lifecycle, so
the check has to happen on the caller's side of the wire: this module decides
whether a returned result body is exactly the contract's shape and returns the
disagreements as strings a human reads. It is the offline half of
`scripts/verify_ocr_service.py`, and it is deliberately not a parser: nothing here
repairs or fills in a missing field.

`summarize_result_body` returns metadata and per-page status only, never document
text, so a verification report cannot leak a payload by accident.
"""

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
# Fields of the retired synchronous expense contract. Their presence would mean the
# old extraction path answered instead of the OCR service (issue #12).
FORBIDDEN_KEYS = ("สิ่งที่อ่าน", "Analysis", "Type of Expense", "engine_ref")


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
        errors.append(f"page {number}: unexpected keys {sorted(extra)}")
    if page.get("page_number") != number:
        errors.append(f"page {number}: page_number is {page.get('page_number')!r}")
    if page.get("text_source") not in TEXT_SOURCES:
        errors.append(f"page {number}: text_source is {page.get('text_source')!r}")
    if not isinstance(page.get("warnings"), list):
        errors.append(f"page {number}: warnings is not a list")

    status = page.get("status")
    if status not in PAGE_STATES:
        errors.append(f"page {number}: status is {status!r}")
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
        errors.append(f"unexpected result keys: {sorted(unexpected)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version is {payload.get('schema_version')!r}")
    if not payload.get("job_id"):
        errors.append("job_id is empty")

    state = payload.get("state")
    if state not in FINAL_STATES:
        errors.append(f"state is not a final state: {state!r}")
    reason = payload.get("reason")
    if reason is not None and reason not in REASONS:
        errors.append(f"reason is not a contract value: {reason!r}")
    if (reason is None) != (state == "succeeded"):
        errors.append("reason must be null exactly when the state is succeeded")
    if payload.get("accuracy") != "not_verified":
        # Execution success is never accuracy (§2.5).
        errors.append(f"accuracy is {payload.get('accuracy')!r}")

    pages = payload.get("pages")
    statuses: list[str | None] = []
    if not isinstance(pages, list):
        errors.append("pages is not a list")
        pages = []
    else:
        statuses = [_verify_page(page, index, errors) for index, page in enumerate(pages)]
    page_count = payload.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool):
        errors.append(f"page_count is not an integer: {page_count!r}")
    elif page_count != len(pages):
        # One accounted-for entry per source page, failed pages included (§2.6).
        errors.append(f"page_count {page_count} does not match {len(pages)} page entries")

    succeeded_texts = [
        page["text"]
        for page, status in zip(pages, statuses, strict=True)
        if status == "succeeded"
    ]
    if succeeded_texts:
        expected_text = "\n".join(succeeded_texts)
        if payload.get("text") != expected_text:
            errors.append("text is not the successful pages joined in page order")
    elif "text" in payload:
        errors.append("text is present with no successful page")

    expected_warnings: list[str] = []
    for page in pages:
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
        if set(timestamps) != TIMESTAMP_KEYS:
            errors.append(f"timestamps keys are {sorted(timestamps)}")
        if not all(isinstance(value, str) and value for value in timestamps.values()):
            errors.append("a timestamp is empty or not a string")
    return errors


def summarize_result_body(payload: dict) -> dict:
    """A redacted view of a result body: metadata, counts, and no document text."""
    pages = payload.get("pages")
    pages = pages if isinstance(pages, list) else []
    return {
        "schema_version": payload.get("schema_version"),
        "job_id": payload.get("job_id"),
        "state": payload.get("state"),
        "reason": payload.get("reason"),
        "completeness": payload.get("completeness"),
        "accuracy": payload.get("accuracy"),
        "page_count": payload.get("page_count"),
        "text_length": len(payload.get("text") or ""),
        "warnings": payload.get("warnings"),
        "timestamps": payload.get("timestamps"),
        "pages": [
            {
                "page_number": page.get("page_number"),
                "status": page.get("status"),
                "text_source": page.get("text_source"),
                "text_length": len(page.get("text") or ""),
                "warnings": page.get("warnings"),
                "error_code": page.get("error_code"),
            }
            for page in pages
            if isinstance(page, dict)
        ],
    }

#!/usr/bin/env python3
"""Verify a deployed OCR v1 service through its asynchronous lifecycle.

Submit -> poll -> result, per document, against a private Cloud Run service that
requires a Google ID token. The check is the caller's: `app/ocr_verification.py`
decides whether each result body is exactly the contract's shape (result and page
keys, types, page numbering, text join, completeness, warning union), and this
script reports what it observed.

    uv run python -m scripts.verify_ocr_service \
        --base-url "$SERVICE_URL" --file document.pdf --gcloud-auth \
        --impersonate tu-ai-n8n-dev-runner-sa@thaiunion-ai-platform.iam.gserviceaccount.com

Every run spends real OCR work and quota on an approved deployment, so run it only
under an approved document and spend budget. The report holds metadata and key
names only; `--include-responses` adds full OCR text and is for an explicitly
approved local evidence run because that text may be sensitive. `docs/ocr-v1-cutover.md`
records that no such run has been performed.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from app.ocr_verification import (
    HTTP_ERROR_CODES,
    REDACTED,
    summarize_result_body,
    verify_result_body,
)

FINAL_STATES = {"succeeded", "partially_succeeded", "failed"}
# The contract's own hint for a retry while the job is not final (§2.2).
DEFAULT_POLL_INTERVAL_S = 10.0
# Report keys that are never copied from a response without an allowlist check.
SAFE_CONTENT_TYPES = {"application/json"}


def identity_token(base_url: str, *, impersonate: str | None) -> str:
    """A Google ID token whose `aud` is the service URL the app re-verifies.

    `--audiences` is what makes the token match `OCR_API_AUDIENCE`; without it the
    app rejects the token `401`. GCP's `print-identity-token --audiences` needs a
    service account, so a user account must impersonate one that holds
    `roles/run.invoker` (via `roles/iam.serviceAccountTokenCreator` on the caller).
    """
    command = ["gcloud", "auth", "print-identity-token"]
    if impersonate:
        command.append(f"--impersonate-service-account={impersonate}")
    command.append(f"--audiences={base_url}")
    return subprocess.check_output(command, text=True, stderr=sys.stderr).strip()


def submit(client: httpx.Client, base_url: str, path: Path, *, idempotency_key: str):
    with path.open("rb") as handle:
        return client.post(
            f"{base_url}/v1/ocr/jobs",
            headers={"Idempotency-Key": idempotency_key},
            files={"file": (path.name, handle, "application/pdf")},
        )


def poll_until_final(
    client: httpx.Client,
    base_url: str,
    job_id: str,
    *,
    timeout_s: float,
    interval_s: float,
) -> tuple[dict | None, list[int], str | None]:
    """Poll the status route until the job is final; the result route is next."""
    deadline = time.monotonic() + timeout_s
    statuses: list[int] = []
    while True:
        response = client.get(f"{base_url}/v1/ocr/jobs/{job_id}")
        statuses.append(response.status_code)
        if response.status_code != 200:
            return None, statuses, f"status route returned HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return None, statuses, "status route returned a non-JSON body"
        if not isinstance(body, dict):
            return None, statuses, "status route returned a non-object body"
        if body.get("state") in FINAL_STATES:
            return body, statuses, None
        # The service's own hint beats the default interval, bounded by the caller's.
        wait = min(float(response.headers.get("Retry-After") or interval_s), interval_s)
        if time.monotonic() + wait >= deadline:
            return body, statuses, f"job {job_id} was not final within {timeout_s:g}s"
        time.sleep(wait)


def verify_document(
    client: httpx.Client,
    base_url: str,
    path: Path,
    *,
    poll_timeout_s: float,
    poll_interval_s: float,
    include_responses: bool,
) -> dict:
    key = uuid.uuid4().hex
    submission = submit(client, base_url, path, idempotency_key=key)
    result: dict = {
        "file": path.name,
        # The key is a caller credential: the report records only its one-way
        # digest, so a report file or a printed run cannot replay the key.
        "idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "submitted": submission.status_code,
        "errors": [],
    }
    if submission.status_code not in (200, 202):
        result["errors"].append(f"submission returned HTTP {submission.status_code}")
        result["response"] = _body(submission, include_responses)
        return result

    submission_payload = _json_body(submission)
    job_id = submission_payload.get("job_id", "") if isinstance(submission_payload, dict) else ""
    status, statuses, poll_error = poll_until_final(
        client, base_url, job_id, timeout_s=poll_timeout_s, interval_s=poll_interval_s
    )
    result.update({"job_id": job_id, "status_codes": statuses, "status": status})
    if poll_error:
        result["errors"].append(poll_error)
        return result

    retrieval = client.get(f"{base_url}/v1/ocr/jobs/{job_id}/result")
    result["result_status"] = retrieval.status_code
    if retrieval.status_code != 200:
        result["errors"].append(f"result route returned HTTP {retrieval.status_code}")
        result["response"] = _body(retrieval, include_responses)
        return result

    payload = _json_body(retrieval)
    result["errors"] = verify_result_body(payload)
    # A retried submission inside the record's lifetime must return the same job.
    retry = submit(client, base_url, path, idempotency_key=key)
    retry_payload = _json_body(retry)
    result["retry_status"] = retry.status_code
    if retry.status_code != 200 or not (
        isinstance(retry_payload, dict) and retry_payload.get("job_id") == job_id
    ):
        result["errors"].append(
            f"an idempotent resubmission returned HTTP {retry.status_code}"
        )
    if include_responses and isinstance(payload, dict):
        result["response"] = payload
    elif isinstance(payload, dict):
        result["response"] = summarize_result_body(payload)
    else:
        # A non-object result body is a verification failure, not a payload to
        # copy: record its type, status, and length only.
        result["response"] = _body(retrieval, include_responses=False)
    return result


def _json_body(response: httpx.Response):
    """The parsed body, or `None` when it is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _body(response: httpx.Response, include_responses: bool) -> dict:
    """A safe view of a response body.

    A response body is untrusted input: a non-object JSON value or a non-JSON
    body may carry document text, a stack trace, or an internal path. The default
    report therefore records only the status, the declared content type, the
    parsed body's type, and its length, plus the contract's own error code when
    one is present. Raw text is never copied; full bodies require the explicit
    `--include-responses` evidence flag and only for a JSON object.
    """
    parsed = True
    try:
        payload = response.json()
    except ValueError:
        parsed, payload = False, None
    if isinstance(payload, dict):
        body_type = "object"
    elif isinstance(payload, list):
        body_type = "array"
    elif parsed:
        body_type = "scalar"
    else:
        body_type = "not_json"
    if include_responses and isinstance(payload, dict):
        return payload
    safe: dict = {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body_type": body_type,
        "body_length": len(response.content),
    }
    if isinstance(payload, dict):
        safe["schema_version"] = payload.get("schema_version")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            # The code is copied only when §2.7 names it; anything else is a value
            # the untrusted body could use to smuggle text into the report.
            safe["error"] = code if code is None or code in HTTP_ERROR_CODES else REDACTED
        else:
            safe["error"] = None
    return safe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="The deployed service URL.")
    parser.add_argument(
        "--file",
        action="append",
        required=True,
        dest="files",
        help="Document to submit (repeatable).",
    )
    parser.add_argument(
        "--gcloud-auth",
        action="store_true",
        help="Mint an ID token for --base-url from the active gcloud account.",
    )
    parser.add_argument(
        "--impersonate",
        help="Service account whose identity the token carries (needed with --gcloud-auth).",
    )
    parser.add_argument("--poll-timeout", type=float, default=600.0)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    parser.add_argument(
        "--include-responses",
        action="store_true",
        help="Store full OCR text. Only for an explicitly approved local evidence run.",
    )
    parser.add_argument("--output", default="verification/ocr-service-report.json")
    args = parser.parse_args()

    headers = {}
    if args.gcloud_auth:
        # Fails loudly rather than sending an unauthenticated request that would
        # look like a service defect.
        token = identity_token(args.base_url, impersonate=args.impersonate)
        headers["Authorization"] = f"Bearer {token}"

    report = {
        "base_url": args.base_url,
        "authenticated": bool(headers),
        "include_responses": args.include_responses,
        "results": [],
    }
    with httpx.Client(headers=headers, timeout=300) as client:
        for name in args.files:
            report["results"].append(
                verify_document(
                    client,
                    args.base_url.rstrip("/"),
                    Path(name),
                    poll_timeout_s=args.poll_timeout,
                    poll_interval_s=args.poll_interval,
                    include_responses=args.include_responses,
                )
            )
    report["passed"] = all(not result["errors"] for result in report["results"])
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

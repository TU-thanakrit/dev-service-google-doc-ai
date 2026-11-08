"""The async verifier's lifecycle: submit -> poll -> result -> idempotent retry.

Seam: `scripts.verify_ocr_service.verify_document` against a scripted HTTP
transport, so the whole client-side flow is exercised with no network, no token,
and no cloud client. The bodies are written from the contract
(`docs/ocr-v1-contracts.md` §2.2, §2.4, §2.5), not produced by the code under test.
"""

import json

import httpx

from scripts.verify_ocr_service import verify_document

RESULT = {
    "schema_version": "ocr.v1",
    "job_id": "job-1",
    "state": "succeeded",
    "reason": None,
    "completeness": "complete",
    "accuracy": "not_verified",
    "page_count": 1,
    "text": "recognized text",
    "pages": [
        {
            "page_number": 1,
            "status": "succeeded",
            "text_source": "ocr",
            "text": "recognized text",
            "warnings": [],
            "error_code": None,
        }
    ],
    "warnings": [],
    "timestamps": {
        "accepted_at": "2026-11-08T04:31:07Z",
        "finalized_at": "2026-11-08T04:31:10Z",
        "expires_at": "2026-11-09T04:31:10Z",
    },
}


def status_body(state):
    return {
        "schema_version": "ocr.v1",
        "job_id": "job-1",
        "state": state,
        "timestamps": {
            "accepted_at": "2026-11-08T04:31:07Z",
            "deadline_at": "2026-11-09T04:31:07Z",
            "finalized_at": None,
            "expires_at": None,
        },
        "dispatch": {"acknowledged": False, "execution_ref": None},
        "progress": {"pages_total": 1, "pages_done": 0},
    }


class Scripted:
    """A transport that replays the lifecycle and records what was asked."""

    def __init__(self, *, status_states=("running", "succeeded"), retry_status=200,
                 retry_job_id="job-1", result_status=200, result=RESULT):
        self.status_states = list(status_states)
        self.retry_status = retry_status
        self.retry_job_id = retry_job_id
        self.result_status = result_status
        self.result = result
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/ocr/jobs" and request.method == "POST":
            if len([r for r in self.requests if r.method == "POST"]) > 1:
                return httpx.Response(
                    self.retry_status,
                    json={**status_body("succeeded"), "job_id": self.retry_job_id},
                )
            return httpx.Response(202, json={**status_body("accepted"), "job_id": "job-1"})
        if path == "/v1/ocr/jobs/job-1":
            state = (
                self.status_states.pop(0)
                if len(self.status_states) > 1
                else self.status_states[0]
            )
            return httpx.Response(200, json=status_body(state), headers={"Retry-After": "0"})
        if path == "/v1/ocr/jobs/job-1/result":
            return httpx.Response(self.result_status, json=self.result)
        return httpx.Response(
            404, json={"schema_version": "ocr.v1", "error": {"code": "not_found"}}
        )


def verify(scripted, tmp_path, **kwargs):
    with httpx.Client(transport=httpx.MockTransport(scripted.handler)) as client:
        return verify_document(
            client,
            "https://service.example",
            tmp_path / "doc.pdf",
            poll_timeout_s=5.0,
            poll_interval_s=0.01,
            include_responses=False,
            **kwargs,
        )


def test_a_final_result_verifies_and_the_report_keeps_no_text(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    scripted = Scripted()

    report = verify(scripted, tmp_path)

    assert report["errors"] == []
    assert report["result_status"] == 200
    assert report["retry_status"] == 200
    assert report["response"]["state"] == "succeeded"
    # Metadata only: the document text never reaches the report file.
    assert "recognized text" not in json.dumps(report, ensure_ascii=False)
    # Submit, poll, poll, retrieve, idempotent resubmission.
    methods = [request.method for request in scripted.requests]
    assert methods == ["POST", "GET", "GET", "GET", "POST"]


def test_a_non_conformant_result_is_reported(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    # A failed page that was silently dropped from the accounting.
    broken = json.loads(json.dumps(RESULT))
    broken["page_count"] = 2
    scripted = Scripted(result=broken)

    report = verify(scripted, tmp_path)

    assert report["errors"]
    assert any("page_count" in error for error in report["errors"])


def test_a_job_that_never_finalizes_times_out_without_retrieving_a_result(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    scripted = Scripted(status_states=("running",))

    report = verify(scripted, tmp_path)

    assert report["errors"] and "not final" in report["errors"][0]
    assert "result_status" not in report
    assert all(request.url.path != "/v1/ocr/jobs/job-1/result" for request in scripted.requests)


def test_a_replacement_job_on_a_repeated_submission_is_reported(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    # Deduplication was lost: the same key returned a different job.
    scripted = Scripted(retry_job_id="job-2")

    report = verify(scripted, tmp_path)

    assert any("idempotent resubmission" in error for error in report["errors"])


def test_a_rejected_submission_reports_the_error_code_without_the_body(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")

    def handler(request):
        return httpx.Response(
            413,
            json={
                "schema_version": "ocr.v1",
                "error": {"code": "file_too_large", "message": "file exceeds the size limit"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_document(
            client,
            "https://service.example",
            tmp_path / "doc.pdf",
            poll_timeout_s=5.0,
            poll_interval_s=0.01,
            include_responses=False,
        )

    assert report["submitted"] == 413
    assert any("HTTP 413" in error for error in report["errors"])
    expected = httpx.Response(
        413,
        json={
            "schema_version": "ocr.v1",
            "error": {"code": "file_too_large", "message": "file exceeds the size limit"},
        },
    )
    assert report["response"] == {
        "status": 413,
        "content_type": "application/json",
        "body_type": "object",
        "body_length": len(expected.content),
        "schema_version": "ocr.v1",
        "error": "file_too_large",
    }


def test_a_non_object_json_body_is_reported_as_metadata_only(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    # An array is not the contract's body, and its contents may be arbitrary:
    # the default report records its type and size, never the payload.
    scripted = Scripted(result=["recognized text", {"secret": "CONFIDENTIAL"}])

    report = verify(scripted, tmp_path)

    assert report["errors"] == ["result body is not a JSON object"]
    assert report["response"] == {
        "status": 200,
        "content_type": "application/json",
        "body_type": "array",
        "body_length": len(httpx.Response(200, json=scripted.result).content),
    }
    assert "recognized text" not in json.dumps(report)
    assert "CONFIDENTIAL" not in json.dumps(report)


def test_a_non_json_body_is_never_copied_into_the_report(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    secret = "Traceback /home/danny/private/path token=abc"

    def handler(request):
        return httpx.Response(500, text=secret)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_document(
            client,
            "https://service.example",
            tmp_path / "doc.pdf",
            poll_timeout_s=5.0,
            poll_interval_s=0.01,
            include_responses=False,
        )

    assert report["submitted"] == 500
    assert report["response"]["status"] == 500
    assert report["response"]["body_type"] == "not_json"
    assert report["response"]["body_length"] == len(secret.encode())
    assert "private/path" not in json.dumps(report)
    assert "token=abc" not in json.dumps(report)

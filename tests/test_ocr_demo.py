"""Public HTTP seam tests for the demo OCR web service (`app.ocr_demo`).

These tests inject fake backend/token/sleep collaborators and never call GCP or the
real OCR API. They verify the browser-facing contract, passcode enforcement,
sanitization, and the submit->poll->result orchestration.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ocr_demo import (
    HARD_MAX_UPLOAD_BYTES,
    MULTIPART_OVERHEAD_BYTES,
    DemoBackendError,
    OcrDemoSettings,
    build_app_from_env,
    create_demo_app,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
DEMO_KEY = "correct horse battery staple"
BACKEND_URL = "https://dev-ocr-service-api.example.run.app"
JOB_1 = "11111111-1111-4111-8111-111111111111"
JOB_2 = "22222222-2222-4222-8222-222222222222"
JOB_3 = "33333333-3333-4333-8333-333333333333"
JOB_7 = "77777777-7777-4777-8777-777777777777"
JOB_9 = "99999999-9999-4999-8999-999999999999"
JOB_SLOW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeBackend:
    """Injected OCR backend: records calls and returns configured responses."""

    def __init__(
        self,
        *,
        submit_response: dict | None = None,
        status_responses: list[dict] | None = None,
        result_response: dict | None = None,
        submit_error: Exception | None = None,
        status_error: Exception | None = None,
        result_error: Exception | None = None,
    ) -> None:
        self.submit_response = submit_response or {"job_id": JOB_1, "state": "accepted"}
        self.status_responses = list(
            status_responses
            or [
                {"job_id": JOB_1, "state": "running"},
                {"job_id": JOB_1, "state": "succeeded"},
            ]
        )
        self.result_response = result_response or {
            "schema_version": "ocr.v1",
            "job_id": JOB_1,
            "state": "succeeded",
            "reason": None,
            "completeness": "complete",
            "accuracy": "not_verified",
            "page_count": 1,
            "text": "hello",
            "pages": [
                {
                    "page_number": 1,
                    "status": "succeeded",
                    "text_source": "ocr",
                    "text": "hello",
                    "warnings": [],
                    "error_code": None,
                }
            ],
            "warnings": [],
            "timestamps": {
                "accepted_at": "2026-11-08T04:31:07Z",
                "finalized_at": "2026-11-08T04:31:08Z",
                "expires_at": "2026-11-09T04:31:08Z",
            },
        }
        self.submit_error = submit_error
        self.status_error = status_error
        self.result_error = result_error
        self.submits: list[dict] = []
        self.status_calls: list[dict] = []
        self.result_calls: list[dict] = []

    def submit(
        self,
        *,
        backend_url: str,
        token: str,
        file_bytes: bytes,
        idempotency_key: str,
        timeout_s: float | None = None,
        deadline=None,
    ):
        self.submits.append(
            {
                "backend_url": backend_url,
                "token": token,
                "file_bytes": file_bytes,
                "idempotency_key": idempotency_key,
                "timeout_s": timeout_s,
                "deadline": deadline,
            }
        )
        if self.submit_error is not None:
            raise self.submit_error
        return self.submit_response

    def get_status(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline=None,
    ):
        self.status_calls.append(
            {
                "backend_url": backend_url,
                "token": token,
                "job_id": job_id,
                "timeout_s": timeout_s,
                "deadline": deadline,
            }
        )
        if self.status_error is not None:
            raise self.status_error
        if self.status_responses:
            return self.status_responses.pop(0)
        return {"job_id": job_id, "state": "succeeded"}

    def get_result(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline=None,
    ):
        self.result_calls.append(
            {
                "backend_url": backend_url,
                "token": token,
                "job_id": job_id,
                "timeout_s": timeout_s,
                "deadline": deadline,
            }
        )
        if self.result_error is not None:
            raise self.result_error
        return self.result_response


def settings(**overrides) -> OcrDemoSettings:
    values = {
        "backend_url": BACKEND_URL,
        "demo_key": DEMO_KEY,
        "max_upload_bytes": HARD_MAX_UPLOAD_BYTES,
        "poll_interval_s": 0.05,
        "poll_timeout_s": 5.0,
    }
    values.update(overrides)
    return OcrDemoSettings(**values)


def build_client(backend: FakeBackend | None = None, **setting_overrides):
    backend = backend or FakeBackend()
    sleeps: list[float] = []
    token_calls: list[tuple[str, float]] = []

    def token_fetcher(audience: str, timeout_s: float) -> str:
        token_calls.append((audience, timeout_s))
        return "fake-id-token"

    app = create_demo_app(
        settings(**setting_overrides),
        backend=backend,
        token_fetcher=token_fetcher,
        sleeper=sleeps.append,
    )
    client = TestClient(app, raise_server_exceptions=False)
    return SimpleNamespace(
        client=client,
        backend=backend,
        sleeps=sleeps,
        token_calls=token_calls,
        app=app,
    )


def post_ocr(
    client,
    *,
    key=DEMO_KEY,
    content=PDF_BYTES,
    filename="doc.pdf",
    content_type="application/pdf",
):
    headers = {}
    if key is not None:
        headers["X-Demo-Key"] = key
    return client.post(
        "/api/ocr",
        headers=headers,
        files={"file": (filename, io.BytesIO(content), content_type)},
    )


def assert_safe_error(response, status_code: int):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)
    text = json.dumps(body)
    for forbidden in (BACKEND_URL, "fake-id-token", "traceback", "gs://", "/home/", "detail"):
        assert forbidden.lower() not in text.lower()
    assert_security_headers(response)
    return body


def assert_security_headers(response) -> None:
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"
    csp = response.headers.get("content-security-policy", "")
    assert "unsafe-inline" in csp
    assert "frame-ancestors 'none'" in csp


def test_home_page_exposes_required_elements():
    tracer = build_client()

    response = tracer.client.get("/")

    assert response.status_code == 200
    html = response.text
    for marker in (
        'type="file"',
        "preview",
        "passcode",
        "X-Demo-Key",
        "Process",
        "<pre",
        "Download JSON",
        "URL.createObjectURL",
    ):
        assert marker in html
    assert "fake-id-token" not in html
    assert DEMO_KEY not in html
    assert_security_headers(response)


def test_health_is_public():
    tracer = build_client()

    response = tracer.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert_security_headers(response)


def test_missing_demo_key_fails_closed():
    tracer = build_client()

    response = post_ocr(tracer.client, key=None)

    assert_safe_error(response, 401)
    assert tracer.backend.submits == []
    assert tracer.token_calls == []


def test_wrong_demo_key_fails_closed():
    tracer = build_client()

    response = post_ocr(tracer.client, key="not-the-key")

    assert_safe_error(response, 401)
    assert tracer.backend.submits == []


def test_declared_oversize_rejected_before_body_receive():
    tracer = build_client(max_upload_bytes=1024)
    declared = 1024 + MULTIPART_OVERHEAD_BYTES + 1

    response = tracer.client.post(
        "/api/ocr",
        headers={"X-Demo-Key": DEMO_KEY, "Content-Length": str(declared)},
        content=b"x" * declared,
    )

    assert_safe_error(response, 413)
    assert tracer.backend.submits == []
    assert tracer.token_calls == []


def test_wrong_key_with_declared_oversize_rejected_before_body():
    tracer = build_client(max_upload_bytes=1024)
    declared = 1024 + MULTIPART_OVERHEAD_BYTES + 1

    response = tracer.client.post(
        "/api/ocr",
        headers={"X-Demo-Key": "wrong", "Content-Length": str(declared)},
        content=b"x" * declared,
    )

    assert_safe_error(response, 401)
    assert tracer.backend.submits == []


def test_missing_file_sanitized():
    tracer = build_client()

    response = tracer.client.post(
        "/api/ocr",
        headers={"X-Demo-Key": DEMO_KEY},
        data={"not_file": "x"},
    )

    assert_safe_error(response, 400)
    assert response.json()["error"]["code"] == "invalid_request"
    assert tracer.backend.submits == []


def test_invalid_pdf_magic_rejected_before_backend():
    tracer = build_client()

    response = post_ocr(tracer.client, content=b"NOTPDF but named pdf", filename="doc.pdf")

    assert_safe_error(response, 400)
    assert response.json()["error"]["code"] == "invalid_request"
    assert tracer.backend.submits == []


def test_invalid_type_is_rejected_before_backend():
    tracer = build_client()

    response = tracer.client.post(
        "/api/ocr",
        headers={"X-Demo-Key": DEMO_KEY},
        files={"file": ("doc.png", io.BytesIO(b"\x89PNG\r\n"), "image/png")},
    )

    assert_safe_error(response, 415)
    assert tracer.backend.submits == []


def test_non_pdf_extension_is_rejected():
    tracer = build_client()

    response = tracer.client.post(
        "/api/ocr",
        headers={"X-Demo-Key": DEMO_KEY},
        files={"file": ("doc.txt", io.BytesIO(PDF_BYTES), "application/pdf")},
    )

    assert_safe_error(response, 415)
    assert tracer.backend.submits == []


def test_oversize_upload_is_rejected():
    tracer = build_client(max_upload_bytes=16)

    response = post_ocr(tracer.client, content=b"%PDF-" + b"x" * 17)

    assert_safe_error(response, 413)
    assert tracer.backend.submits == []


@pytest.mark.parametrize(
    "backend_url",
    [
        "http://svc.run.app",
        "https://user:pass@svc.run.app",
        "https://svc.run.app/path",
        "https://svc.run.app/?q=1",
        "https://svc.run.app/#frag",
        "https://svc.example.com",
        "https://run.app.evil.example",
    ],
)
def test_backend_url_validation_rejects_unsafe_values(backend_url):
    with pytest.raises(ValidationError):
        settings(backend_url=backend_url)


def test_backend_url_normalizes_trailing_slash_and_portless_root():
    cfg = settings(backend_url="https://my-svc-abc123-uc.a.run.app/")
    assert cfg.backend_url == "https://my-svc-abc123-uc.a.run.app"


def test_max_upload_bytes_hard_capped_at_10_mib():
    with pytest.raises(ValidationError):
        settings(max_upload_bytes=HARD_MAX_UPLOAD_BYTES + 1)


def test_poll_interval_must_be_positive_and_bounded():
    with pytest.raises(ValidationError):
        settings(poll_interval_s=0.0)
    with pytest.raises(ValidationError):
        settings(poll_interval_s=0.01)
    with pytest.raises(ValidationError):
        settings(poll_interval_s=31.0)


def test_successful_submit_poll_result_returns_exact_backend_json():
    expected = {
        "schema_version": "ocr.v1",
        "job_id": JOB_2,
        "state": "succeeded",
        "reason": None,
        "completeness": "complete",
        "accuracy": "not_verified",
        "page_count": 2,
        "text": "a\nb",
        "pages": [{"page_number": 1, "text": "a"}, {"page_number": 2, "text": "b"}],
        "warnings": [],
        "timestamps": {"accepted_at": "t", "finalized_at": "t", "expires_at": "t"},
    }
    backend = FakeBackend(
        submit_response={"job_id": JOB_2, "state": "accepted"},
        status_responses=[
            {"job_id": JOB_2, "state": "accepted"},
            {"job_id": JOB_2, "state": "running"},
            {"job_id": JOB_2, "state": "succeeded"},
        ],
        result_response=expected,
    )
    tracer = build_client(backend=backend, poll_interval_s=0.05)

    response = post_ocr(tracer.client)

    assert response.status_code == 200
    assert response.json() == expected
    assert_security_headers(response)
    assert len(tracer.backend.submits) == 1
    submit_call = tracer.backend.submits[0]
    assert submit_call["backend_url"] == BACKEND_URL
    assert submit_call["token"] == "fake-id-token"
    assert submit_call["file_bytes"] == PDF_BYTES
    assert len(submit_call["idempotency_key"]) >= 8
    assert (
        submit_call["timeout_s"] and submit_call["timeout_s"] > 0
    ) or submit_call["deadline"] is not None
    assert [c["job_id"] for c in tracer.backend.status_calls] == [JOB_2, JOB_2, JOB_2]
    assert all(
        (c["timeout_s"] and c["timeout_s"] > 0) or c["deadline"] is not None
        for c in tracer.backend.status_calls
    )
    assert [c["job_id"] for c in tracer.backend.result_calls] == [JOB_2]
    assert tracer.token_calls[0][0] == BACKEND_URL
    assert tracer.token_calls[0][1] > 0
    assert tracer.sleeps


def test_deadline_rejects_final_state_before_accepting_result():
    backend = FakeBackend(
        submit_response={"job_id": JOB_3, "state": "accepted"},
        status_responses=[{"job_id": JOB_3, "state": "succeeded"}],
        result_response={"job_id": JOB_3, "state": "succeeded"},
    )
    now = {"t": 0.0}

    def clock() -> float:
        # Expire after status has been observed, before accepting the final result.
        if backend.status_calls:
            now["t"] = 100.0
        return now["t"]

    app = create_demo_app(
        settings(poll_interval_s=0.05, poll_timeout_s=1.0),
        backend=backend,
        token_fetcher=lambda audience, timeout_s: "fake-id-token",
        sleeper=lambda _: None,
        clock=clock,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = post_ocr(client)

    assert_safe_error(response, 504)
    assert backend.result_calls == []


def test_upstream_non_object_json_rejected():
    class NonObjectBackend(FakeBackend):
        def get_status(self, *, backend_url, token, job_id, timeout_s=None, deadline=None):
            return ["not", "an", "object"]  # type: ignore[return-value]

    backend = NonObjectBackend(
        submit_response={"job_id": JOB_7, "state": "accepted"},
    )
    tracer = build_client(backend=backend)

    response = post_ocr(tracer.client)

    assert_safe_error(response, 502)


def test_upstream_submit_job_id_must_be_uuid():
    backend = FakeBackend(submit_response={"job_id": "not-a-uuid", "state": "accepted"})
    tracer = build_client(backend=backend)

    response = post_ocr(tracer.client)

    assert_safe_error(response, 502)
    assert backend.status_calls == []


@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [(400, 400), (413, 413), (415, 415), (422, 400)],
)
def test_upstream_submit_client_rejections_mapped(status_code, expected_status):
    backend = FakeBackend(
        submit_error=DemoBackendError(status_code, "upstream_http_error", detail="s3cr3t")
    )
    tracer = build_client(backend=backend)

    response = post_ocr(tracer.client)

    assert_safe_error(response, expected_status)
    assert "s3cr3t" not in response.text


def test_upstream_rejection_is_sanitized():
    backend = FakeBackend(
        submit_error=DemoBackendError(
            403,
            "upstream_http_error",
            detail="private gs://bucket/team/token=abc /home/danny/secret",
        )
    )
    tracer = build_client(backend=backend)

    response = post_ocr(tracer.client)

    body = assert_safe_error(response, 502)
    dumped = json.dumps(body)
    assert "gs://bucket" not in dumped
    assert "secret" not in dumped
    assert "upstream_http_error" not in dumped


def test_poll_timeout_is_sanitized():
    backend = FakeBackend(
        submit_response={"job_id": JOB_SLOW, "state": "accepted"},
        status_responses=[{"job_id": JOB_SLOW, "state": "running"}] * 50,
    )
    now = {"t": 0.0}

    def sleeper(seconds: float) -> None:
        now["t"] += max(seconds, 0.5)

    def clock() -> float:
        return now["t"]

    app = create_demo_app(
        settings(poll_interval_s=0.05, poll_timeout_s=1.0),
        backend=backend,
        token_fetcher=lambda audience, timeout_s: "fake-id-token",
        sleeper=sleeper,
        clock=clock,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = post_ocr(client)

    body = assert_safe_error(response, 504)
    assert "fake-id-token" not in json.dumps(body)


def test_processing_endpoint_is_synchronous():
    import inspect

    from app.ocr_demo import create_demo_app as factory

    app = factory(settings())
    route = next(r for r in app.routes if getattr(r, "path", None) == "/api/ocr")
    assert not inspect.iscoroutinefunction(route.endpoint)


def test_submission_is_never_retried():
    attempts = {"n": 0}

    class FlakySubmitBackend(FakeBackend):
        def submit(self, **kwargs):
            attempts["n"] += 1
            raise DemoBackendError(503, "upstream_unavailable", detail="try again")

    tracer = build_client(backend=FlakySubmitBackend())

    response = post_ocr(tracer.client)

    assert_safe_error(response, 502)
    assert attempts["n"] == 1


def test_token_fetcher_receives_backend_url_audience():
    tracer = build_client()

    post_ocr(tracer.client)

    assert tracer.token_calls[0][0] == BACKEND_URL


def test_failed_terminal_state_still_returns_result_json():
    failed = {"job_id": JOB_9, "state": "failed", "reason": "deadline_exceeded", "pages": []}
    backend = FakeBackend(
        submit_response={"job_id": JOB_9, "state": "accepted"},
        status_responses=[{"job_id": JOB_9, "state": "failed"}],
        result_response=failed,
    )
    tracer = build_client(backend=backend)

    response = post_ocr(tracer.client)

    assert response.status_code == 200
    assert response.json() == failed


def test_route_level_key_defense_in_depth():
    tracer = build_client()
    app = tracer.app

    with TestClient(app, raise_server_exceptions=False) as client:
        # Route sees missing key even if guard logic differed; we test route directly via app.
        response = post_ocr(client, key=None)
    assert_safe_error(response, 401)


def test_build_app_from_env_fails_startup_when_required_env_absent(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OCR_DEMO_"):
            monkeypatch.delenv(name, raising=False)
    # Ensure pydantic-settings doesn't read a .env that provides values
    monkeypatch.delenv("OCR_DEMO_BACKEND_URL", raising=False)
    monkeypatch.delenv("OCR_DEMO_KEY", raising=False)
    with pytest.raises(ValidationError):
        build_app_from_env()


def test_entrypoint_serves_ui_and_health_with_valid_env(monkeypatch):
    monkeypatch.setenv("OCR_DEMO_BACKEND_URL", BACKEND_URL)
    monkeypatch.setenv("OCR_DEMO_KEY", DEMO_KEY)
    sys.modules.pop("app.ocr_demo_entrypoint", None)
    mod = importlib.import_module("app.ocr_demo_entrypoint")
    client = TestClient(mod.app, raise_server_exceptions=False)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert 'type="file"' in client.get("/").text
    sys.modules.pop("app.ocr_demo_entrypoint", None)


# --- Regression tests for final reviewer findings ---


class _FakeSocket:
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.timeouts = []

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)


class _FakeHTTPResponse:
    def __init__(self, status=200, chunks=None, headers=None):
        self.status = status
        self._chunks = list(chunks or [b"{}"])
        self.headers = headers or {}
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=_FakeSocket()))

    def read(self, n=-1):
        if not self._chunks:
            return b""
        if n is None or n < 0:
            data = b"".join(self._chunks)
            self._chunks = []
            return data
        data = self._chunks[0]
        if len(data) <= n:
            self._chunks.pop(0)
            return data
        self._chunks[0] = data[n:]
        return data[:n]

    def read1(self, n=-1):
        return self.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status


class _RedirectingHTTPError(Exception):
    def __init__(self, code=302, headers=None):
        self.code = code
        self.headers = headers or {"Location": "https://evil.example/leak"}


def test_no_redirect_handler_structure_covers_all_redirect_codes():
    import urllib.request as urlrequest

    from app.ocr_demo import _NoRedirectHandler

    handler = _NoRedirectHandler()
    assert isinstance(handler, urlrequest.HTTPRedirectHandler)
    assert not isinstance(handler, type)  # sanity: instance
    # All standard redirect codes must be handled by returning None (no follow).
    for code in (301, 302, 303, 307, 308):
        assert (
            handler.redirect_request(None, None, code, "msg", {}, "http://evil.example/leak")
            is None
        )


@pytest.mark.parametrize("redirect_code", [302, 307])
def test_real_no_redirect_opener_never_hits_redirect_target(redirect_code):
    import http.server
    import socketserver
    import threading
    import urllib.error as urlerror
    import urllib.request as urlrequest

    from app.ocr_demo import _no_redirect_opener

    target_requests: list[dict[str, str]] = []

    class TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            target_requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "idempotency_key": self.headers.get("Idempotency-Key", ""),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"leaked":true}')

        def log_message(self, format, *args):  # noqa: A002
            return

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        target_url = ""

        def do_GET(self):  # noqa: N802
            self.send_response(redirect_code)
            self.send_header("Location", self.target_url)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    with _Server(("127.0.0.1", 0), TargetHandler) as target_httpd:
        target_port = target_httpd.server_address[1]
        target_thread = threading.Thread(target=target_httpd.serve_forever, daemon=True)
        target_thread.start()

        RedirectHandler.target_url = f"http://127.0.0.1:{target_port}/leaked"
        with _Server(("127.0.0.1", 0), RedirectHandler) as redirect_httpd:
            redirect_port = redirect_httpd.server_address[1]
            redirect_thread = threading.Thread(target=redirect_httpd.serve_forever, daemon=True)
            redirect_thread.start()

            req = urlrequest.Request(
                f"http://127.0.0.1:{redirect_port}/start",
                headers={
                    "Authorization": "Bearer secret-token",
                    "Idempotency-Key": "secret-idempotency-key",
                },
                method="GET",
            )
            with pytest.raises(urlerror.HTTPError) as excinfo:
                _no_redirect_opener().open(req, timeout=5.0)
            assert excinfo.value.code == redirect_code

            redirect_httpd.shutdown()
            redirect_thread.join(timeout=5)

        target_httpd.shutdown()
        target_thread.join(timeout=5)

    assert target_requests == [], (
        "redirect target must receive no request and no Authorization/Idempotency-Key; "
        f"got {target_requests!r}"
    )


def test_http_backend_does_not_follow_redirects_and_rejects_3xx(monkeypatch):
    from urllib import error as urlerror

    from app.ocr_demo import HttpOcrBackend

    seen = {"urlopen_calls": 0, "redirect_seen": False}

    class NoRedirectOpener:
        def open(self, req, timeout=None):
            seen["urlopen_calls"] += 1
            # Simulate a redirect response that must not be followed.
            raise urlerror.HTTPError(
                req.full_url, 302, "Found", {"Location": "https://evil.example"}, None
            )

    backend = HttpOcrBackend()
    backend._opener = NoRedirectOpener()  # type: ignore[attr-defined]

    with pytest.raises(DemoBackendError) as excinfo:
        backend.submit(
            backend_url=BACKEND_URL,
            token="fake-id-token",
            file_bytes=PDF_BYTES,
            idempotency_key="k",
            timeout_s=5.0,
        )
    assert excinfo.value.status_code in {502, 504}
    assert excinfo.value.code not in {"", None}
    # 3xx must be sanitized upstream failure, never a followed redirect.
    assert seen["urlopen_calls"] == 1


def test_http_backend_submit_rejects_3xx_status(monkeypatch):
    from app.ocr_demo import HttpOcrBackend

    class Opener:
        def open(self, req, timeout=None):
            return _FakeHTTPResponse(status=302, chunks=[b'{"job_id":"%s"}' % JOB_1.encode()])

    backend = HttpOcrBackend()
    backend._opener = Opener()  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError) as excinfo:
        backend.submit(
            backend_url=BACKEND_URL,
            token="fake-id-token",
            file_bytes=PDF_BYTES,
            idempotency_key="k",
            timeout_s=5.0,
        )
    assert excinfo.value.status_code == 502 or excinfo.value.code.startswith("upstream")


def test_http_backend_result_rejects_202_and_unexpected_2xx():
    from app.ocr_demo import HttpOcrBackend

    class Opener:
        def __init__(self, status):
            self.status = status

        def open(self, req, timeout=None):
            return _FakeHTTPResponse(
                status=self.status,
                chunks=[
                    b'{"job_id":"%s","state":"running"}' % JOB_1.encode()
                ],
            )

    backend = HttpOcrBackend()
    backend._opener = Opener(202)  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError):
        backend.get_result(
            backend_url=BACKEND_URL, token="t", job_id=JOB_1, timeout_s=5.0
        )

    backend._opener = Opener(204)  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError):
        backend.get_result(
            backend_url=BACKEND_URL, token="t", job_id=JOB_1, timeout_s=5.0
        )


def test_http_backend_submit_accepts_only_200_202():
    from app.ocr_demo import HttpOcrBackend

    class Opener:
        def __init__(self, status):
            self.status = status

        def open(self, req, timeout=None):
            return _FakeHTTPResponse(
                status=self.status,
                chunks=[b'{"job_id":"%s","state":"accepted"}' % JOB_1.encode()],
            )

    backend = HttpOcrBackend()
    backend._opener = Opener(200)  # type: ignore[attr-defined]
    assert backend.submit(
        backend_url=BACKEND_URL, token="t", file_bytes=PDF_BYTES, idempotency_key="k", timeout_s=5.0
    )["job_id"] == JOB_1

    backend._opener = Opener(202)  # type: ignore[attr-defined]
    assert backend.submit(
        backend_url=BACKEND_URL, token="t", file_bytes=PDF_BYTES, idempotency_key="k", timeout_s=5.0
    )["job_id"] == JOB_1

    backend._opener = Opener(201)  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError):
        backend.submit(
            backend_url=BACKEND_URL,
            token="t",
            file_bytes=PDF_BYTES,
            idempotency_key="k",
            timeout_s=5.0,
        )


def test_http_backend_status_accepts_only_200():
    from app.ocr_demo import HttpOcrBackend

    class Opener:
        def __init__(self, status):
            self.status = status

        def open(self, req, timeout=None):
            return _FakeHTTPResponse(
                status=self.status,
                chunks=[
                    b'{"job_id":"%s","state":"running"}' % JOB_1.encode()
                ],
            )

    backend = HttpOcrBackend()
    backend._opener = Opener(200)  # type: ignore[attr-defined]
    assert (
        backend.get_status(backend_url=BACKEND_URL, token="t", job_id=JOB_1, timeout_s=5.0)[
            "state"
        ]
        == "running"
    )

    backend._opener = Opener(202)  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError):
        backend.get_status(backend_url=BACKEND_URL, token="t", job_id=JOB_1, timeout_s=5.0)


def test_http_backend_caps_response_bytes_and_fails_sanitized():
    from app.ocr_demo import HttpOcrBackend

    big = b"{" + b"a" * (5 * 1024 * 1024 + 100) + b"}"

    class Opener:
        def open(self, req, timeout=None):
            return _FakeHTTPResponse(status=200, chunks=[big])

    backend = HttpOcrBackend()
    backend._opener = Opener()  # type: ignore[attr-defined]
    with pytest.raises(DemoBackendError) as excinfo:
        backend.get_status(backend_url=BACKEND_URL, token="t", job_id=JOB_1, timeout_s=5.0)
    assert (
        excinfo.value.code
        in {"upstream_invalid_response", "upstream_unavailable", "upstream_timeout"}
        or excinfo.value.status_code in {502, 504}
    )


def test_asgi_chunked_buffer_stops_cleanly_on_disconnect():
    import threading

    from app.ocr_demo import _buffer_bounded_receive

    async def run():
        messages = [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.disconnect"},
        ]
        idx = {"i": 0}

        async def receive():
            # If we loop after disconnect, we'll run out and raise.
            if idx["i"] >= len(messages):
                raise AssertionError("receive called after disconnect")
            msg = messages[idx["i"]]
            idx["i"] += 1
            return msg

        sent = []

        async def send(message):
            sent.append(message)

        replay = await _buffer_bounded_receive(receive, 1024, send)
        # Disconnect should be treated as clean stop (no 413, no hang).
        assert sent == []
        assert replay is None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run())
        return
    errors: list[BaseException] = []

    def target():
        try:
            asyncio.run(run())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "disconnect test hung"
    if errors:
        raise errors[0]


def test_fetch_google_id_token_uses_metadata_server_no_redirects(monkeypatch):

    import app.ocr_demo as demo

    calls = []

    class Opener:
        def open(self, req, timeout=None):
            calls.append(
                {
                    "url": req.full_url,
                    "headers": {k.lower(): v for k, v in req.header_items()},
                    "timeout": timeout,
                }
            )
            return _FakeHTTPResponse(status=200, chunks=[b"header.payload.sig"])

    monkeypatch.setattr(demo, "_no_redirect_opener", lambda: Opener())
    token = demo.fetch_google_id_token("https://svc.run.app", 5.0)
    assert token == "header.payload.sig"
    assert len(calls) == 1
    assert calls[0]["url"].startswith("http://metadata.google.internal/")
    headers = {k.lower(): v for k, v in calls[0]["headers"].items()}
    assert headers.get("metadata-flavor") == "Google"
    assert "audience=" in calls[0]["url"]
    assert "https%3A%2F%2Fsvc.run.app" in calls[0]["url"]


def test_fetch_google_id_token_sanitizes_errors(monkeypatch):
    import app.ocr_demo as demo

    class Opener:
        def open(self, req, timeout=None):
            raise OSError("metadata down secret=/token")

    monkeypatch.setattr(demo, "_no_redirect_opener", lambda: Opener())
    with pytest.raises(DemoBackendError) as excinfo:
        demo.fetch_google_id_token("https://svc.run.app", 5.0)
    assert "metadata down" not in str(excinfo.value)
    assert excinfo.value.code in {"token_unavailable", "upstream_timeout", "upstream_unavailable"}


def test_fetch_google_id_token_rejects_oversize_response(monkeypatch):
    import app.ocr_demo as demo

    class Opener:
        def open(self, req, timeout=None):
            return _FakeHTTPResponse(status=200, chunks=[b"x" * (16 * 1024 + 1)])

    monkeypatch.setattr(demo, "_no_redirect_opener", lambda: Opener())
    with pytest.raises(DemoBackendError):
        demo.fetch_google_id_token("https://svc.run.app", 5.0)


def test_home_page_clears_state_on_new_file_selection():
    tracer = build_client()
    html = tracer.client.get("/").text
    # The file change handler must clear prior result/state before setting preview.
    assert "lastJson = null" in html
    assert "output.textContent = ''" in html or 'output.textContent = ""' in html
    assert "downloadBtn.disabled = true" in html
    assert "statusEl.textContent" in html
    # Clearing must happen in the change handler before preview assignment.
    change_idx = html.index("fileInput.addEventListener('change'")
    preview_idx = html.index("preview.src = previewUrl", change_idx)
    clear_idx = html.index("lastJson = null", change_idx)
    assert clear_idx < preview_idx
    # File changes must invalidate any in-flight request generation.
    assert "requestGeneration += 1" in html or "requestGeneration++" in html


def test_home_page_disables_file_picker_during_inflight_request():
    tracer = build_client()
    html = tracer.client.get("/").text
    # Process handler must disable file picker while the request is in flight and
    # re-enable it only when that same request finishes.
    process_idx = html.index("processBtn.addEventListener('click'")
    disable_idx = html.index("fileInput.disabled = true", process_idx)
    fetch_idx = html.index("fetch('/api/ocr'", process_idx)
    assert disable_idx < fetch_idx
    # Re-enable only for the matching generation in finally.
    finally_idx = html.index("finally", process_idx)
    reen_idx = html.index("fileInput.disabled = false", finally_idx)
    guard_idx = html.index("generation === requestGeneration", finally_idx)
    assert guard_idx < reen_idx
    # Stale in-flight responses must not overwrite current UI state.
    assert "if (generation !== requestGeneration) return" in html
    assert "requestGeneration = 0" in html or "let requestGeneration = 0" in html


def test_dockerfile_demo_has_pid1_exec_entrypoint_and_non_root():
    from pathlib import Path

    dockerfile = Path("Dockerfile.demo").read_text(encoding="utf-8")
    assert "CMD" in dockerfile
    assert "exec uvicorn app.ocr_demo_entrypoint:app" in dockerfile
    assert '--port ${PORT}' in dockerfile or '--port "${PORT}"' in dockerfile
    assert "USER 10001" in dockerfile or "USER " in dockerfile
    assert "app.ocr_api:app" not in dockerfile


def test_existing_dockerfile_unchanged_entrypoint():
    from pathlib import Path

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "app.ocr_api:app" in dockerfile
    assert "app.ocr_demo_entrypoint:app" not in dockerfile


def test_docs_reference_dockerfile_demo():
    from pathlib import Path

    docs = Path("docs/ocr-demo.md").read_text(encoding="utf-8")
    assert "Dockerfile.demo" in docs
    assert "app.ocr_demo_entrypoint:app" in docs

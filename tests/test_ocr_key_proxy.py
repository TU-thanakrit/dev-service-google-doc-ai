"""Public ASGI HTTP tests for the dev-only OCR API-key facade.

Seam: the facade's public HTTP surface (`app.ocr_key_proxy`) and real local HTTP
upstreams it forwards to. No cloud calls and no live metadata/backend: metadata
token acquisition and network clients are exercised via injectable async seams and
local HTTP servers. This proves the facade's own contract without altering OCR core.
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from starlette.testclient import TestClient

import app.ocr_key_proxy as key_proxy
from app.ocr_key_proxy import (
    PRODUCTION_BACKEND_URL,
    build_httpx_client_factory,
    create_app,
    fetch_metadata_id_token,
)

DEMO_KEY = "dev-demo-key-1234567890abcdef1234567890"  # 38 chars, strong
SHORT_KEY = "short-key"
BACKEND_AUDIENCE = "https://ocr-api.example"
JOB_ID = "12345678-1234-1234-1234-123456789abc"
NOT_A_UUID = "not-a-job-id"


class RecordedRequest:
    def __init__(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        self.method = handler.command
        self.path = handler.path
        self.headers = {k.lower(): v for k, v in handler.headers.items()}
        self.body = body


class UpstreamController:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self.response_status = 200
        self.response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
        self.response_body = b'{"ok":true}'
        self.delay_s = 0.0
        self.close_without_response = False
        self.redirect_location: str | None = None

    def record(self, req: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(req)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    @property
    def last(self) -> RecordedRequest:
        with self._lock:
            assert self.requests, "upstream received no request"
            return self.requests[-1]


def start_upstream(controller: UpstreamController):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self._handle()

        def do_POST(self):  # noqa: N802
            self._handle()

        def do_PUT(self):  # noqa: N802
            self._handle()

        def do_DELETE(self):  # noqa: N802
            self._handle()

        def log_message(self, format, *args):  # noqa: A002
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            req = RecordedRequest(self, body)
            controller.record(req)
            if controller.close_without_response:
                self.close_connection = True
                try:
                    self.connection.shutdown(1)
                except Exception:  # noqa: BLE001
                    pass
                return
            if controller.delay_s:
                time.sleep(controller.delay_s)
            if controller.redirect_location:
                self.send_response(302)
                self.send_header("Location", controller.redirect_location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body_bytes = controller.response_body
            self.send_response(controller.response_status)
            for name, value in controller.response_headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body_bytes)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class TrickleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay_per_chunk_s = 0.2
    chunks = 50

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(self.chunks):
            time.sleep(self.delay_per_chunk_s)
            chunk = b'{"x":1}'
            self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
            self.wfile.write(chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, format, *args):  # noqa: A002
        return


def start_trickle_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


@pytest.fixture()
def upstream():
    controller = UpstreamController()
    server, url = start_upstream(controller)
    try:
        yield controller, url
    finally:
        server.shutdown()
        server.server_close()


async def fixed_token_provider(audience: str, client: httpx.AsyncClient) -> str:
    assert audience == BACKEND_AUDIENCE
    return "id-token-for-backend"


def make_client(
    upstream_url: str,
    *,
    token_provider=fixed_token_provider,
    demo_key: str = DEMO_KEY,
    **kwargs,
):
    app = create_app(
        backend_url=upstream_url,
        demo_key=demo_key,
        backend_audience=BACKEND_AUDIENCE,
        token_provider=token_provider,
        allow_http_backend=True,
        **kwargs,
    )
    return TestClient(app, raise_server_exceptions=False)


# --- Authentication (all four routes, including health) -------------------------

ROUTES = [
    ("GET", "/health", None),
    ("POST", "/v1/ocr/jobs", b"hello"),
    ("GET", f"/v1/ocr/jobs/{JOB_ID}", None),
    ("GET", f"/v1/ocr/jobs/{JOB_ID}/result", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_missing_demo_key_is_fixed_401_before_body_read(upstream, method, path, body):
    controller, url = upstream
    with make_client(url) as client:
        payload = b"x" * (21 * 1024 * 1024) if body is not None else None
        headers = {"Content-Type": "application/octet-stream"} if payload is not None else {}
        response = client.request(method, path, content=payload, headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "missing or invalid demo key"}
    }
    assert response.headers.get("cache-control") == "no-store"
    assert controller.requests == []


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_incorrect_demo_key_is_fixed_401(upstream, method, path, body):
    controller, url = upstream
    with make_client(url) as client:
        response = client.request(
            method,
            path,
            content=body,
            headers={"X-Demo-Key": "wrong-key", "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "missing or invalid demo key"}
    }
    assert controller.requests == []


def test_health_requires_demo_key_and_proxies_when_valid(upstream):
    controller, url = upstream
    controller.response_body = b'{"status":"ok"}'
    with make_client(url) as client:
        response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert controller.last.path == "/health"


# --- Route/method/ID validation --------------------------------------------------


def test_get_job_rejects_non_uuid_id(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.get(f"/v1/ocr/jobs/{NOT_A_UUID}", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 400
    assert controller.requests == []


def test_get_result_rejects_non_uuid_id(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.get(
            f"/v1/ocr/jobs/{NOT_A_UUID}/result", headers={"X-Demo-Key": DEMO_KEY}
        )
    assert response.status_code == 400
    assert controller.requests == []


def test_unknown_path_is_404_and_not_proxied(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.get("/v1/unknown", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 404
    assert controller.requests == []


def test_wrong_method_on_known_path_is_405_and_not_proxied(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.get("/v1/ocr/jobs", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 405
    assert controller.requests == []


def test_wrong_method_is_405_and_not_proxied(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.delete(f"/v1/ocr/jobs/{JOB_ID}", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 405
    assert controller.requests == []


# --- Forwarding: real async local upstream multipart/status ---------------------


def test_post_preserves_multipart_body_boundary_and_selected_headers(upstream):
    controller, url = upstream
    boundary = "----WebKitFormBoundaryfixedboundary"
    body = (
        b"------WebKitFormBoundaryfixedboundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        b"%PDF-1.4 content\r\n"
        b"------WebKitFormBoundaryfixedboundary--\r\n"
    )
    headers = {
        "X-Demo-Key": DEMO_KEY,
        "Idempotency-Key": "idem-key-12345678",
        "X-Request-Id": "req-123",
        "Authorization": "Bearer attacker",
        "X-Evil": "drop-me",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    controller.response_status = 202
    controller.response_body = b'{"job_id":"j"}'
    with make_client(url) as client:
        response = client.post("/v1/ocr/jobs", content=body, headers=headers)
    assert response.status_code == 202
    sent = controller.last
    assert sent.method == "POST"
    assert sent.path == "/v1/ocr/jobs"
    assert sent.body == body
    assert sent.headers["content-type"] == f"multipart/form-data; boundary={boundary}"
    assert sent.headers["idempotency-key"] == "idem-key-12345678"
    assert sent.headers["x-request-id"] == "req-123"
    assert sent.headers["authorization"] == "Bearer id-token-for-backend"
    assert "x-demo-key" not in sent.headers
    assert "x-evil" not in sent.headers


def test_valid_async_local_upstream_status_and_body_passthrough(upstream):
    controller, url = upstream
    controller.response_status = 200
    controller.response_headers = [
        ("Content-Type", "application/json"),
        ("X-Request-Id", "corr-9"),
    ]
    controller.response_body = b'{"job_id":"j","state":"succeeded"}'
    with make_client(url) as client:
        r = client.get(f"/v1/ocr/jobs/{JOB_ID}/result", headers={"X-Demo-Key": DEMO_KEY})
    assert r.status_code == 200
    assert r.content == controller.response_body
    assert r.headers["x-request-id"] == "corr-9"


def test_header_injection_in_forwarded_value_is_rejected(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=b"x",
            headers={
                "X-Demo-Key": DEMO_KEY,
                "Idempotency-Key": "good-key\r\nX-Evil: injected",
                "Content-Type": "application/octet-stream",
            },
        )
    assert response.status_code == 400
    assert controller.requests == []


# --- Strict upstream origin and audience ----------------------------------------


def test_rejects_unsafe_backend_url_at_construction():
    for bad in [
        "http://public.example",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?q=1",
        "https://example.com:8443",
    ]:
        with pytest.raises(ValueError):
            create_app(
                backend_url=bad,
                demo_key=DEMO_KEY,
                backend_audience=BACKEND_AUDIENCE,
                token_provider=fixed_token_provider,
                allow_http_backend=False,
            )


def test_production_origin_must_be_exact():
    from app.ocr_key_proxy import validate_production_backend_url

    assert validate_production_backend_url(PRODUCTION_BACKEND_URL) == PRODUCTION_BACKEND_URL
    with pytest.raises(ValueError):
        validate_production_backend_url("https://other.run.app")
    with pytest.raises(ValueError):
        validate_production_backend_url(PRODUCTION_BACKEND_URL + "/extra")


# --- Upstream pass-through -------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,extra_headers",
    [
        (200, b'{"job_id":"j","state":"running"}', []),
        (202, b'{"job_id":"j","state":"accepted"}', [("Retry-After", "10")]),
        (400, b'{"error":"bad"}', []),
        (401, b'{"error":"unauthorized"}', []),
        (403, b'{"error":"forbidden"}', []),
        (404, b'{"error":"not found"}', []),
        (409, b'{"error":"conflict"}', []),
        (410, b'{"error":"gone"}', []),
        (413, b'{"error":"too large"}', []),
        (415, b'{"error":"unsupported"}', []),
        (422, b'{"error":"unprocessable"}', []),
        (429, b'{"error":"rate"}', [("Retry-After", "30")]),
        (503, b'{"error":"unavailable"}', [("Retry-After", "5")]),
    ],
)
def test_passes_through_upstream_status_body_and_safe_headers(
    upstream, status, body, extra_headers
):
    controller, url = upstream
    controller.response_status = status
    controller.response_body = body
    controller.response_headers = [
        ("Content-Type", "application/json"),
        ("X-Request-Id", "corr-1"),
        ("X-Secret-Backend", "must-not-leak"),
        ("Set-Cookie", "a=b"),
    ] + extra_headers
    with make_client(url) as client:
        response = client.get(f"/v1/ocr/jobs/{JOB_ID}", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == status
    assert response.content == body
    assert response.headers.get("content-type", "").startswith("application/json")
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-secret-backend") is None
    assert response.headers.get("set-cookie") is None
    assert response.headers.get("x-request-id") == "corr-1"
    for name, value in extra_headers:
        assert response.headers.get(name.lower()) == value


# --- Error handling and safety ---------------------------------------------------


def test_upstream_transport_error_returns_fixed_safe_502(upstream):
    controller, url = upstream
    controller.close_without_response = True
    with make_client(url, read_timeout_s=0.5, total_timeout_s=1.0) as client:
        response = client.get(f"/v1/ocr/jobs/{JOB_ID}", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 502
    assert response.json() == {
        "error": {"code": "upstream_error", "message": "upstream request failed"}
    }
    assert "id-token-for-backend" not in response.text


def test_metadata_token_failure_returns_fixed_safe_502(upstream):
    controller, url = upstream

    async def broken_provider(audience: str, client: httpx.AsyncClient) -> str:
        raise RuntimeError("metadata down with secret")

    with make_client(url, token_provider=broken_provider) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=b"abc",
            headers={"X-Demo-Key": DEMO_KEY, "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 502
    assert response.json() == {
        "error": {"code": "upstream_error", "message": "upstream request failed"}
    }
    assert controller.requests == []
    assert "secret" not in response.text


def test_does_not_retry_post_on_transport_uncertainty(upstream):
    controller, url = upstream
    controller.close_without_response = True
    with make_client(url, read_timeout_s=0.5, total_timeout_s=1.0) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=b"payload",
            headers={"X-Demo-Key": DEMO_KEY, "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 502
    assert controller.count == 1


# --- Body and response size limits ----------------------------------------------


def test_declared_oversized_body_rejected_before_forward_with_connection_close(upstream):
    controller, url = upstream
    with make_client(url, max_body_bytes=1024) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=b"x" * 2048,
            headers={"X-Demo-Key": DEMO_KEY, "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 413
    assert controller.requests == []
    # HTTP/1.1 reject path requests close
    assert response.headers.get("connection", "").lower() == "close"


def test_chunked_oversized_body_rejected_before_forward(upstream):
    controller, url = upstream
    with make_client(url, max_body_bytes=1024) as client:

        def chunks():
            for _ in range(20):
                yield b"x" * 100

        response = client.post(
            "/v1/ocr/jobs",
            content=chunks(),
            headers={"X-Demo-Key": DEMO_KEY, "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 413
    assert controller.requests == []
    assert response.headers.get("connection", "").lower() == "close"


def test_exact_20mib_declared_body_is_allowed_to_forward(upstream):
    controller, url = upstream
    controller.response_status = 202
    controller.response_body = b'{"ok":true}'
    payload = b"x" * (20 * 1024 * 1024)
    with make_client(url, max_body_bytes=20 * 1024 * 1024) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=payload,
            headers={"X-Demo-Key": DEMO_KEY, "Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 202
    assert controller.last.body == payload


def test_upstream_response_over_16mib_is_capped(upstream):
    controller, url = upstream
    controller.response_body = b"x" * (16 * 1024 * 1024 + 1)
    with make_client(url) as client:
        response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 502
    assert response.json() == {
        "error": {"code": "upstream_error", "message": "upstream request failed"}
    }


def test_query_parameters_are_rejected_and_not_forwarded(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.get("/health?x=1", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 400
    assert controller.requests == []
    assert response.headers.get("connection", "").lower() == "close"


def test_responses_include_no_store(upstream):
    controller, url = upstream
    controller.response_body = b'{"status":"ok"}'
    with make_client(url) as client:
        response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
    assert response.headers.get("cache-control") == "no-store"


# --- Timeouts --------------------------------------------------------------------


def test_slow_metadata_token_times_out(upstream):
    controller, url = upstream

    async def slow_provider(audience: str, client: httpx.AsyncClient) -> str:
        await asyncio.sleep(0.5)
        return "late-token"

    with make_client(
        url, token_provider=slow_provider, total_timeout_s=0.1, metadata_timeout_s=0.1
    ) as client:
        response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 504
    assert response.json() == {
        "error": {"code": "upstream_timeout", "message": "upstream request timed out"}
    }
    assert controller.requests == []


def test_response_trickle_times_out():
    server, url = start_trickle_server()
    try:
        with make_client(url, read_timeout_s=0.2, total_timeout_s=0.3) as client:
            response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
        assert response.status_code == 504
        assert response.json() == {
            "error": {"code": "upstream_timeout", "message": "upstream request timed out"}
        }
    finally:
        server.shutdown()
        server.server_close()


# --- Proxy env inheritance and redirects disabled --------------------------------


def test_client_factory_disables_proxy_env_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    factory = build_httpx_client_factory()
    client = factory()
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        asyncio.run(client.aclose())


def test_backend_redirect_is_not_followed_with_real_local_seam():
    controller = UpstreamController()
    controller.redirect_location = "http://127.0.0.1:9/evil"
    server, url = start_upstream(controller)
    try:
        with make_client(url) as client:
            response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
        # Upstream 302 is passed through as-is; no follow.
        assert response.status_code == 302
        assert controller.count == 1
    finally:
        server.shutdown()
        server.server_close()


def test_metadata_fetch_uses_required_headers_and_rejects_redirect(monkeypatch):
    controller = UpstreamController()
    controller.response_status = 200
    controller.response_headers = [("Content-Type", "text/plain")]
    controller.response_body = b"token-abc"
    server, url = start_upstream(controller)
    original = key_proxy.METADATA_TOKEN_URL
    monkeypatch.setattr(key_proxy, "METADATA_TOKEN_URL", url + "/token")
    try:
        async def run():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                token = await fetch_metadata_id_token("aud", client)
            return token

        token = asyncio.run(run())
        assert token == "token-abc"
        assert controller.last.headers["metadata-flavor"] == "Google"
        assert controller.last.path.startswith("/token?")
        assert "audience=aud" in controller.last.path
        assert "format=full" in controller.last.path

        controller.requests.clear()
        controller.redirect_location = "http://evil/token"
        controller.response_status = 302

        async def run_redirect():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                # redirect not followed => response 302 raise_for_status
                await fetch_metadata_id_token("aud", client)

        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            asyncio.run(run_redirect())
    finally:
        monkeypatch.setattr(key_proxy, "METADATA_TOKEN_URL", original)
        server.shutdown()
        server.server_close()


# --- Concurrency: slow upstream does not block event loop -----------------------


def test_slow_upstream_does_not_block_event_loop():
    controller = UpstreamController()
    controller.delay_s = 0.4
    controller.response_body = b'{"status":"ok"}'
    server, url = start_upstream(controller)
    try:
        app = create_app(
            backend_url=url,
            demo_key=DEMO_KEY,
            backend_audience=BACKEND_AUDIENCE,
            token_provider=fixed_token_provider,
            allow_http_backend=True,
            read_timeout_s=2.0,
            total_timeout_s=3.0,
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                start = time.perf_counter()
                tasks = [
                    asyncio.create_task(
                        client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
                    ),
                    asyncio.create_task(
                        client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
                    ),
                    asyncio.create_task(
                        client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
                    ),
                ]
                results = await asyncio.gather(*tasks)
                elapsed = time.perf_counter() - start
            return results, elapsed

        results, elapsed = asyncio.run(run())
        assert all(r.status_code == 200 for r in results)
        # Three 0.4s upstreams should overlap substantially if event loop not blocked.
        assert elapsed < 1.0
        assert controller.count == 3
    finally:
        server.shutdown()
        server.server_close()


# --- Review regression tests: total deadline, metadata bounds, content-encoding ---


def test_authenticated_trickle_upload_times_out_with_504_and_no_upstream_post(upstream):
    controller, url = upstream
    app = create_app(
        backend_url=url,
        demo_key=DEMO_KEY,
        backend_audience=BACKEND_AUDIENCE,
        token_provider=fixed_token_provider,
        allow_http_backend=True,
        total_timeout_s=0.15,
    )

    async def run():
        sent = []

        async def receive():
            await asyncio.sleep(0.2)
            return {"type": "http.request", "body": b"x" * 10, "more_body": True}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/ocr/jobs",
            "raw_path": b"/v1/ocr/jobs",
            "query_string": b"",
            "headers": [
                (b"x-demo-key", DEMO_KEY.encode("ascii")),
                (b"content-type", b"application/octet-stream"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    assert sent[0]["status"] == 504
    body = sent[1]["body"]
    assert body == (
        b'{"error":{"code":"upstream_timeout","message":"upstream request timed out"}}'
    )
    assert controller.requests == []


def test_metadata_timeout_s_is_nested_and_returns_504(upstream):
    controller, url = upstream

    async def slow_provider(audience: str, client: httpx.AsyncClient) -> str:
        await asyncio.sleep(0.3)
        return "late-token"

    with make_client(
        url,
        token_provider=slow_provider,
        total_timeout_s=1.0,
        metadata_timeout_s=0.05,
    ) as client:
        response = client.get("/health", headers={"X-Demo-Key": DEMO_KEY})
    assert response.status_code == 504
    assert response.json() == {
        "error": {"code": "upstream_timeout", "message": "upstream request timed out"}
    }
    assert controller.requests == []


def test_metadata_token_oversized_stream_is_bounded(monkeypatch):
    controller = UpstreamController()
    controller.response_status = 200
    controller.response_headers = [("Content-Type", "text/plain")]
    controller.response_body = b"x" * 8193
    server, url = start_upstream(controller)
    original = key_proxy.METADATA_TOKEN_URL
    monkeypatch.setattr(key_proxy, "METADATA_TOKEN_URL", url + "/token")
    try:
        async def run():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                await fetch_metadata_id_token("aud", client)

        with pytest.raises(RuntimeError):
            asyncio.run(run())
    finally:
        monkeypatch.setattr(key_proxy, "METADATA_TOKEN_URL", original)
        server.shutdown()
        server.server_close()


def test_preserves_content_encoding_and_exact_bytes(upstream):
    controller, url = upstream
    boundary = "----Boundarygzip"
    body = (
        b"------Boundarygzip\r\n"
        b'Content-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        b"\x1f\x8b\x08\x00raw-gzip-like-bytes\r\n"
        b"------Boundarygzip--\r\n"
    )
    headers = {
        "X-Demo-Key": DEMO_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Encoding": "gzip",
    }
    controller.response_status = 202
    controller.response_body = b'{"ok":true}'
    with make_client(url) as client:
        response = client.post("/v1/ocr/jobs", content=body, headers=headers)
    assert response.status_code == 202
    sent = controller.last
    assert sent.body == body
    assert sent.headers["content-encoding"] == "gzip"
    assert sent.headers["content-type"] == f"multipart/form-data; boundary={boundary}"


def test_content_encoding_header_injection_is_rejected(upstream):
    controller, url = upstream
    with make_client(url) as client:
        response = client.post(
            "/v1/ocr/jobs",
            content=b"x",
            headers={
                "X-Demo-Key": DEMO_KEY,
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "gzip\r\nX-Evil: injected",
            },
        )
    assert response.status_code == 400
    assert controller.requests == []


# --- Entrypoint: strong key and strict origin/audience --------------------------


def test_entrypoint_rejects_short_demo_key(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("OCR_KEY_PROXY_BACKEND_URL", PRODUCTION_BACKEND_URL)
    monkeypatch.setenv("OCR_KEY_PROXY_BACKEND_AUDIENCE", PRODUCTION_BACKEND_URL)
    monkeypatch.setenv("OCR_KEY_PROXY_KEY", "short-key")
    sys.modules.pop("app.ocr_key_proxy_entrypoint", None)
    with pytest.raises(ValueError):
        importlib.import_module("app.ocr_key_proxy_entrypoint")


def test_entrypoint_requires_exact_audience(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("OCR_KEY_PROXY_BACKEND_URL", PRODUCTION_BACKEND_URL)
    monkeypatch.setenv("OCR_KEY_PROXY_BACKEND_AUDIENCE", "https://other.example")
    monkeypatch.setenv("OCR_KEY_PROXY_KEY", DEMO_KEY)
    sys.modules.pop("app.ocr_key_proxy_entrypoint", None)
    with pytest.raises(RuntimeError):
        importlib.import_module("app.ocr_key_proxy_entrypoint")

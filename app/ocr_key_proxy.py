"""Development-only Cloud Run API-key facade for the private OCR v1 API.

A separate, minimal HTTP facade that forwards a fixed set of OCR routes to the
existing private backend. It authenticates callers with a shared `X-Demo-Key`
and exchanges a Google ID token for the backend's exact audience on the
caller's behalf. All callers map to the facade runtime identity (the single
backend principal whose token is minted), not to individual caller identities.

This is a dev-only control with no production-readiness claim, no rate limiting,
and no claim of per-user isolation. It must not be confused with the OCR core
or with any production authentication path.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from urllib import parse

import httpx

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "ocr.key-proxy.v2"
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_PROXY_RESPONSE_BYTES = 16 * 1024 * 1024
MIN_DEMO_KEY_LENGTH = 32
PRODUCTION_BACKEND_URL = "https://dev-ocr-service-api-1022222817897.asia-southeast1.run.app"
METADATA_HOST = "metadata.google.internal"
METADATA_TOKEN_URL = (
    f"http://{METADATA_HOST}/computeMetadata/v1/instance/service-accounts/default/identity"
)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SAFE_HEADER_VALUE_RE = re.compile(r"^[^\r\n\x00]*$")
# Correlation id echoed only when safe and bounded; never used for identity.
SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

FORWARDED_REQUEST_HEADERS = ("idempotency-key", "x-request-id")
PASSTHROUGH_RESPONSE_HEADERS = {"content-type", "retry-after", "x-request-id"}
ALLOWED_ROUTES = {
    ("GET", "/health"): "health",
    ("POST", "/v1/ocr/jobs"): "submit",
    ("GET", "/v1/ocr/jobs/{job_id}"): "status",
    ("GET", "/v1/ocr/jobs/{job_id}/result"): "result",
}

AUTH_ERROR_BODY = {
    "error": {"code": "unauthorized", "message": "missing or invalid demo key"}
}
UPSTREAM_ERROR_BODY = {
    "error": {"code": "upstream_error", "message": "upstream request failed"}
}
UPSTREAM_TIMEOUT_BODY = {
    "error": {"code": "upstream_timeout", "message": "upstream request timed out"}
}

TokenProvider = Callable[[str, httpx.AsyncClient], Awaitable[str]]
ClientFactory = Callable[..., httpx.AsyncClient]


def validate_backend_url(url: str) -> parse.SplitResult:
    parts = parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("OCR_KEY_PROXY_BACKEND_URL must use https")
    if not parts.netloc or parts.username or parts.password:
        raise ValueError(
            "OCR_KEY_PROXY_BACKEND_URL must be a host without credentials"
        )
    if parts.port is not None:
        raise ValueError("OCR_KEY_PROXY_BACKEND_URL must not include a port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            "OCR_KEY_PROXY_BACKEND_URL must not include a path, query, or fragment"
        )
    return parts


def validate_production_backend_url(url: str) -> str:
    """Strict fail-closed production backend origin check."""
    parts = validate_backend_url(url)
    origin = f"https://{parts.netloc}"
    if origin != PRODUCTION_BACKEND_URL:
        raise ValueError(
            "OCR_KEY_PROXY_BACKEND_URL must equal the exact production backend origin"
        )
    return origin


def validate_demo_key(key: str) -> bytes:
    if len(key) < MIN_DEMO_KEY_LENGTH:
        raise ValueError("OCR_KEY_PROXY_KEY must be at least 32 characters")
    if key.strip() != key or not key.isascii():
        raise ValueError(
            "OCR_KEY_PROXY_KEY must be non-whitespace ASCII and not surrounded by whitespace"
        )
    return key.encode("ascii")


def _json_response(
    status: int, body: dict, extra_headers: Iterable[tuple[str, str]] = ()
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    for name, value in extra_headers:
        headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    return status, headers, payload


def _error(status: int, body: dict, extra_headers: Iterable[tuple[str, str]] = ()):  # noqa: ANN201
    return _json_response(status, body, extra_headers)


def _match_route(method: str, path: str) -> tuple[str | None, str | None]:
    if (method, path) in ALLOWED_ROUTES:
        return ALLOWED_ROUTES[(method, path)], path
    if path == "/health":
        return "known-family", None
    if path == "/v1/ocr/jobs":
        return "known-family", None
    prefix = "/v1/ocr/jobs/"
    if path.startswith(prefix):
        rest = path[len(prefix) :]
        if "/" not in rest:
            if method == "GET":
                return "status", rest
            return "known-family", rest
        job_id, tail = rest.split("/", 1)
        if tail == "result":
            if method == "GET":
                return "result", job_id
            return "known-family", job_id
    return None, None


def _is_uuid(value: str) -> bool:
    return UUID_RE.match(value or "") is not None


def _safe_forward_value(name: str, value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if not SAFE_HEADER_VALUE_RE.match(value):
        return None
    if name == "x-request-id" and not SAFE_REQUEST_ID_RE.match(value):
        return None
    return value


def build_httpx_client_factory() -> ClientFactory:
    """Build an AsyncClient factory with proxy env inheritance disabled."""

    def factory(**kwargs):  # noqa: ANN003, ANN202
        kwargs.setdefault("trust_env", False)
        kwargs.setdefault("follow_redirects", False)
        return httpx.AsyncClient(**kwargs)

    return factory


async def fetch_metadata_id_token(audience: str, client: httpx.AsyncClient) -> str:
    """Fetch a Google ID token with an email claim from the metadata server.

    Host is fixed and redirects are disabled by the client configuration. The
    token response body is streamed and capped to 8192 bytes.
    """
    async with client.stream(
        "GET",
        METADATA_TOKEN_URL,
        params={"audience": audience, "format": "full"},
        headers={"Metadata-Flavor": "Google"},
    ) as response:
        response.raise_for_status()
        # Defense in depth: never trust a redirected response.
        expected_host = parse.urlsplit(METADATA_TOKEN_URL).hostname
        if response.history or parse.urlsplit(str(response.url)).hostname != expected_host:
            raise RuntimeError("metadata redirect")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 8192:
                raise RuntimeError("metadata token invalid")
    token = bytes(body).decode("utf-8", errors="strict").strip()
    if not token or len(token) > 8192 or "\n" in token or "\r" in token:
        raise RuntimeError("metadata token invalid")
    return token


class KeyProxyApp:
    def __init__(
        self,
        *,
        backend_url: str,
        demo_key: str,
        backend_audience: str,
        token_provider: TokenProvider | None = None,
        client_factory: ClientFactory | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_response_bytes: int = MAX_PROXY_RESPONSE_BYTES,
        connect_timeout_s: float = 5.0,
        read_timeout_s: float = 60.0,
        total_timeout_s: float = 70.0,
        metadata_timeout_s: float = 5.0,
        allow_http_backend: bool = False,
        require_strong_key: bool = False,
    ) -> None:
        self.demo_key = (
            validate_demo_key(demo_key)
            if require_strong_key
            else demo_key.encode("utf-8")
        )
        if not self.demo_key:
            raise ValueError("OCR_KEY_PROXY_KEY is required")
        self.max_body_bytes = int(max_body_bytes)
        if self.max_body_bytes < 1 or self.max_body_bytes > DEFAULT_MAX_BODY_BYTES:
            raise ValueError("max_body_bytes must be between 1 and 20 MiB")
        self.max_response_bytes = int(max_response_bytes)
        if self.max_response_bytes < 1 or self.max_response_bytes > MAX_PROXY_RESPONSE_BYTES:
            raise ValueError("max_response_bytes must be between 1 and 16 MiB")
        self.connect_timeout_s = float(connect_timeout_s)
        self.read_timeout_s = float(read_timeout_s)
        self.total_timeout_s = float(total_timeout_s)
        self.metadata_timeout_s = float(metadata_timeout_s)
        self.backend_audience = backend_audience
        if allow_http_backend:
            parts = parse.urlsplit(backend_url)
            if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                raise ValueError("backend_url must be an explicit http(s) origin")
            if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
                raise ValueError(
                    "backend_url must not include path, query, fragment, or credentials"
                )
            self.backend_origin = f"{parts.scheme.lower()}://{parts.netloc}"
        else:
            parts = validate_backend_url(backend_url)
            self.backend_origin = f"https://{parts.netloc}"
        self.token_provider = token_provider or fetch_metadata_id_token
        self.client_factory = client_factory or build_httpx_client_factory()

    @asynccontextmanager
    async def _client(self, **kwargs):  # noqa: ANN003, ANN201
        client = self.client_factory(**kwargs)
        try:
            yield client
        finally:
            await client.aclose()

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        try:
            await self._handle_http(scope, receive, send)
        except asyncio.CancelledError:
            # Cancellation must be handled safely and not leak internals.
            raise
        except Exception:  # noqa: BLE001
            logger.warning("ocr_key_proxy internal failure")
            await self._safe_send(
                send, *_error(502, UPSTREAM_ERROR_BODY, self._close_headers(scope))
            )

    async def _handle_http(self, scope, receive, send) -> None:  # noqa: ANN001
        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        req_headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        close = self._close_headers(scope)

        # 1) Auth first for all handled/unhandled facade requests, before any body read.
        supplied = req_headers.get("x-demo-key", "").encode("utf-8")
        if not hmac.compare_digest(supplied, self.demo_key):
            await self._drain_and_send_error(receive, send, *_error(401, AUTH_ERROR_BODY, close))
            return

        if scope.get("query_string"):
            await self._drain_and_send_error(
                receive,
                send,
                *_error(
                    400,
                    {
                        "error": {
                            "code": "invalid_request",
                            "message": "query parameters are not allowed",
                        }
                    },
                    close,
                ),
            )
            return

        # 2) Route and method validation: never proxy unknown paths/methods.
        route, param = _match_route(method, path)
        if route is None:
            await self._drain_and_send_error(
                receive,
                send,
                *_error(404, {"error": {"code": "not_found", "message": "not found"}}, close),
            )
            return
        if route == "known-family":
            await self._drain_and_send_error(
                receive,
                send,
                *_error(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "method not allowed",
                        }
                    },
                    close,
                ),
            )
            return
        if route in ("status", "result") and not _is_uuid(param or ""):
            await self._drain_and_send_error(
                receive,
                send,
                *_error(
                    400,
                    {
                        "error": {
                            "code": "invalid_job_id",
                            "message": "job_id must be a UUID",
                        }
                    },
                    close,
                ),
            )
            return

        # 3) Pre-read checks and body/header validation (after auth/route).
        # On auth/unknown/declared-overlimit rejects we do not read the body
        # (handled above); ingress/platform may still receive bytes and Cloud Run
        # outer HTTP request size limits apply. The key is not protection from
        # bandwidth abuse.
        declared = req_headers.get("content-length")
        if declared is not None:
            try:
                declared_len = int(declared)
            except Exception:  # noqa: BLE001
                await self._drain_and_send_error(
                    receive,
                    send,
                    *_error(
                        400,
                        {
                            "error": {
                                "code": "invalid_request",
                                "message": "invalid content-length",
                            }
                        },
                        close,
                    ),
                )
                return
            if declared_len > self.max_body_bytes:
                await self._drain_and_send_error(
                    receive,
                    send,
                    *_error(
                        413,
                        {
                            "error": {
                                "code": "payload_too_large",
                                "message": "request body too large",
                            }
                        },
                        close,
                    ),
                )
                return

        # 4) Build outbound request headers from safe forwarded values only.
        out_headers: list[tuple[str, str]] = []
        content_type = req_headers.get("content-type")
        if content_type is not None:
            safe_ct = _safe_forward_value("content-type", content_type)
            if safe_ct is None:
                await self._safe_send(
                    send,
                    *_error(
                        400,
                        {
                            "error": {
                                "code": "invalid_header",
                                "message": "invalid header value",
                            }
                        },
                        close,
                    ),
                )
                return
            out_headers.append(("Content-Type", safe_ct))
        content_encoding = req_headers.get("content-encoding")
        if content_encoding is not None:
            safe_ce = _safe_forward_value("content-encoding", content_encoding)
            if safe_ce is None:
                await self._safe_send(
                    send,
                    *_error(
                        400,
                        {
                            "error": {
                                "code": "invalid_header",
                                "message": "invalid header value",
                            }
                        },
                        close,
                    ),
                )
                return
            out_headers.append(("Content-Encoding", safe_ce))
        for name in FORWARDED_REQUEST_HEADERS:
            if name in req_headers:
                safe = _safe_forward_value(name, req_headers[name])
                if safe is None:
                    await self._safe_send(
                        send,
                        *_error(
                            400,
                            {
                                "error": {
                                    "code": "invalid_header",
                                    "message": "invalid header value",
                                }
                            },
                            close,
                        ),
                    )
                    return
                header_name = (
                    "Idempotency-Key" if name == "idempotency-key" else "X-Request-Id"
                )
                out_headers.append((header_name, safe))

        timeout = httpx.Timeout(
            self.total_timeout_s,
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
        )
        upstream_url = self.backend_origin + path
        try:
            # One strict absolute deadline spans inbound body read, metadata token
            # acquisition, and the entire upstream submission/response read.
            # Never retry POST.
            async with asyncio.timeout(self.total_timeout_s):
                body, read_err = await self._read_body(receive, scope=scope)
                if read_err is not None:
                    if read_err == "disconnect":
                        return
                    status, resp_headers, payload = read_err
                    await self._safe_send(
                        send, status, self._with_close(resp_headers, scope), payload
                    )
                    return
                out_headers.append(("Content-Length", str(len(body))))

                async with self._client(timeout=timeout) as client:
                    try:
                        async with asyncio.timeout(self.metadata_timeout_s):
                            token = await self.token_provider(
                                self.backend_audience, client
                            )
                    except (TimeoutError, httpx.TimeoutException):
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("ocr_key_proxy token acquisition failed")
                        raise RuntimeError("metadata token unavailable") from exc
                    if (
                        not isinstance(token, str)
                        or not token
                        or "\n" in token
                        or "\r" in token
                    ):
                        logger.warning(
                            "ocr_key_proxy token acquisition returned invalid token"
                        )
                        raise RuntimeError("metadata token invalid")
                    out_headers.append(("Authorization", f"Bearer {token}"))
                    async with client.stream(
                        method,
                        upstream_url,
                        headers=out_headers,
                        content=body,
                    ) as upstream_resp:
                        resp_body = bytearray()
                        async for chunk in upstream_resp.aiter_bytes():
                            resp_body.extend(chunk)
                            if len(resp_body) > self.max_response_bytes:
                                raise RuntimeError("upstream response too large")
                        status = upstream_resp.status_code
                        u_headers = list(upstream_resp.headers.multi_items())
        except (TimeoutError, httpx.TimeoutException):
            await self._safe_send(send, *_error(504, UPSTREAM_TIMEOUT_BODY, close))
            return
        except Exception:  # noqa: BLE001
            logger.warning("ocr_key_proxy upstream request failed")
            await self._safe_send(send, *_error(502, UPSTREAM_ERROR_BODY, close))
            return

        # 5) Pass through only allowed response metadata.
        resp_headers = [(b"cache-control", b"no-store")]
        content_type_out = None
        for name, value in u_headers:
            lname = name.lower()
            if lname == "set-cookie":
                continue
            if lname in PASSTHROUGH_RESPONSE_HEADERS and SAFE_HEADER_VALUE_RE.match(value or ""):
                if lname == "content-type":
                    content_type_out = value
                resp_headers.append((lname.encode("latin-1"), value.encode("latin-1")))
        if content_type_out is None:
            resp_headers.append((b"content-type", b"application/octet-stream"))
        resp_headers.append((b"content-length", str(len(resp_body)).encode("ascii")))
        await self._safe_send(send, status, resp_headers, bytes(resp_body))

    async def _read_body(self, receive, *, scope):  # noqa: ANN001
        # Enforces actual/chunked body size <= max_body_bytes. Declared overlimit
        # is rejected by caller without reading. On chunked oversize we stop
        # reading app-side and return 413 with Connection: close for HTTP/1.1;
        # this does not claim an application guarantee of ingress draining.
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return b"", "disconnect"
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"") or b""
            body.extend(chunk)
            if len(body) > self.max_body_bytes:
                return b"", _error(
                    413,
                    {
                        "error": {
                            "code": "payload_too_large",
                            "message": "request body too large",
                        }
                    },
                )
            more = bool(message.get("more_body", False))
        return bytes(body), None

    async def _drain_and_send_error(self, receive, send, status, headers, payload):  # noqa: ANN001
        # Auth/validation/declared-overlimit failures must be returned without
        # reading the request body in application logic. We respond immediately;
        # we do not claim ingress draining.
        await self._safe_send(send, status, headers, payload)

    @staticmethod
    def _close_headers(scope) -> list[tuple[str, str]]:  # noqa: ANN001
        if scope.get("http_version", "1.1") == "1.1":
            return [("connection", "close")]
        return []

    @staticmethod
    def _with_close(headers: list[tuple[bytes, bytes]], scope) -> list[tuple[bytes, bytes]]:  # noqa: ANN001
        if scope.get("http_version", "1.1") == "1.1":
            return headers + [(b"connection", b"close")]
        return headers

    async def _safe_send(self, send, status, headers, payload):  # noqa: ANN001
        try:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": payload})
        except Exception:  # noqa: BLE001
            # Client disconnected/cancelled mid-response; nothing safe to do.
            return


def create_app(
    *,
    backend_url: str | None = None,
    demo_key: str | None = None,
    backend_audience: str | None = None,
    token_provider: TokenProvider | None = None,
    client_factory: ClientFactory | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_response_bytes: int = MAX_PROXY_RESPONSE_BYTES,
    connect_timeout_s: float = 5.0,
    read_timeout_s: float = 60.0,
    total_timeout_s: float = 70.0,
    metadata_timeout_s: float = 5.0,
    allow_http_backend: bool = False,
    require_strong_key: bool = False,
):
    """Build the facade ASGI app.

    `allow_http_backend` exists only for offline tests that exercise a real local
    upstream seam; production/configured entrypoints must use an `https://`
    backend origin and leave it false.
    """
    if backend_url is None:
        raise ValueError("OCR_KEY_PROXY_BACKEND_URL is required")
    if demo_key is None:
        raise ValueError("OCR_KEY_PROXY_KEY is required")
    if backend_audience is None:
        raise ValueError("OCR_KEY_PROXY_BACKEND_AUDIENCE is required")
    return KeyProxyApp(
        backend_url=backend_url,
        demo_key=demo_key,
        backend_audience=backend_audience,
        token_provider=token_provider,
        client_factory=client_factory,
        max_body_bytes=max_body_bytes,
        max_response_bytes=max_response_bytes,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
        total_timeout_s=total_timeout_s,
        metadata_timeout_s=metadata_timeout_s,
        allow_http_backend=allow_http_backend,
        require_strong_key=require_strong_key,
    )

"""Public demo web service for the private OCR API (Cloud Run).

This app is intentionally separate from the production OCR API. It serves a small
browser UI and proxies one PDF through the private OCR backend using a Google ID
token obtained by the service's runtime identity. App-level passcode auth is
required for processing; the backend token is never exposed to callers.

Importing this module never requires demo environment variables. Deployments use
`app.ocr_demo_entrypoint:app`, which fails startup when configuration is missing
or invalid. Tests import `create_demo_app` and inject fakes.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

FINAL_STATES = frozenset({"succeeded", "partially_succeeded", "failed"})
PDF_MAGIC = b"%PDF-"
HARD_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 16 * 1024
MIN_POLL_INTERVAL_S = 0.05
MAX_POLL_INTERVAL_S = 30.0
MIN_POLL_TIMEOUT_S = 1.0
MAX_POLL_TIMEOUT_S = 3600.0
MIN_REQUEST_TIMEOUT_S = 0.5
MAX_UPSTREAM_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_METADATA_TOKEN_BYTES = 8 * 1024
METADATA_TOKEN_HOST = "metadata.google.internal"
METADATA_TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/identity"

LOG_CATEGORIES = frozenset(
    {
        "internal_error",
        "token_unavailable",
        "upstream_http_error",
        "upstream_invalid_json",
        "upstream_invalid_response",
        "upstream_missing_job_id",
        "upstream_redirect",
        "upstream_timeout",
        "upstream_unavailable",
        "validation_error",
    }
)

SAFE_ERROR = {"error": {"code": "processing_failed", "message": "OCR processing failed."}}
SAFE_TIMEOUT = {"error": {"code": "timeout", "message": "OCR processing did not complete in time."}}
SAFE_UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "A valid demo key is required."}}
SAFE_BAD_REQUEST = {
    "error": {"code": "invalid_request", "message": "The upload is not a valid PDF."}
}
SAFE_TOO_LARGE = {"error": {"code": "file_too_large", "message": "The PDF exceeds the size limit."}}
SAFE_UNSUPPORTED = {
    "error": {"code": "unsupported_media_type", "message": "Only PDF files are supported."}
}

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "img-src data: blob:; "
        "frame-src blob:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


class OcrDemoSettings(BaseSettings):
    """Environment contract for the demo service."""

    model_config = SettingsConfigDict(env_prefix="OCR_DEMO_", extra="ignore")

    backend_url: str = Field(validation_alias=AliasChoices("OCR_DEMO_BACKEND_URL", "backend_url"))
    demo_key: str = Field(min_length=1, validation_alias=AliasChoices("OCR_DEMO_KEY", "demo_key"))
    max_upload_bytes: int = Field(
        default=HARD_MAX_UPLOAD_BYTES,
        ge=1,
        le=HARD_MAX_UPLOAD_BYTES,
        validation_alias=AliasChoices("OCR_DEMO_MAX_UPLOAD_BYTES", "max_upload_bytes"),
    )
    poll_interval_s: float = Field(
        default=2.0,
        gt=0.0,
        ge=MIN_POLL_INTERVAL_S,
        le=MAX_POLL_INTERVAL_S,
        validation_alias=AliasChoices("OCR_DEMO_POLL_INTERVAL_S", "poll_interval_s"),
    )
    poll_timeout_s: float = Field(
        default=300.0,
        ge=MIN_POLL_TIMEOUT_S,
        le=MAX_POLL_TIMEOUT_S,
        validation_alias=AliasChoices("OCR_DEMO_POLL_TIMEOUT_S", "poll_timeout_s"),
    )

    @field_validator("backend_url", mode="before")
    @classmethod
    def _normalize_backend_url(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.rstrip("/")

    @field_validator("backend_url")
    @classmethod
    def _validate_backend_url(cls, value: str) -> str:
        parsed = urlparse.urlsplit(value)
        if parsed.scheme != "https":
            raise ValueError("backend_url must use HTTPS")
        if not parsed.hostname:
            raise ValueError("backend_url must include a hostname")
        host = parsed.hostname.lower()
        if not host.endswith(".run.app"):
            raise ValueError("backend_url hostname must end with .run.app")
        if parsed.username or parsed.password:
            raise ValueError("backend_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("backend_url must not contain a query or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("backend_url must use the service root path")
        return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


@dataclass(slots=True)
class DemoBackendError(Exception):
    """A backend/proxy failure with an optional diagnostic detail.

    `detail` and `code` are internal diagnostics only and must never be returned
    to clients. Logging uses a fixed allowlist.
    """

    status_code: int
    code: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - never surfaced to clients
        return "backend_error"


class OcrBackend(Protocol):
    def submit(
        self,
        *,
        backend_url: str,
        token: str,
        file_bytes: bytes,
        idempotency_key: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]: ...

    def get_status(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]: ...

    def get_result(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]: ...


TokenFetcher = Callable[[str, float], str]


class _Deadline:
    def __init__(self, total_s: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._deadline = clock() + total_s

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def require(self, minimum_s: float = MIN_REQUEST_TIMEOUT_S) -> float:
        remaining = self.remaining()
        if remaining < minimum_s:
            raise DemoBackendError(504, "upstream_timeout")
        return remaining


def _multipart_file(field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"----ocr-demo-{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    body.extend(data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _parse_json_object(payload: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DemoBackendError(502, "upstream_invalid_json") from None
    if not isinstance(parsed, dict):
        raise DemoBackendError(502, "upstream_invalid_response")
    return parsed


def _parse_job_id(value: Any) -> str:
    if not isinstance(value, str):
        raise DemoBackendError(502, "upstream_invalid_response")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise DemoBackendError(502, "upstream_missing_job_id") from None


def _parse_state(value: Any) -> str:
    if not isinstance(value, str):
        raise DemoBackendError(502, "upstream_invalid_response")
    return value


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Never follow redirects.

    Subclassing HTTPRedirectHandler (not BaseHandler) ensures build_opener treats
    this as the redirect handler and does not also install the default
    HTTPRedirectHandler, which would otherwise forward requests (and headers such
    as Authorization/Idempotency-Key) to the redirect target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _no_redirect_opener() -> urlrequest.OpenerDirector:
    # Do not follow redirects. Authorization/Idempotency-Key must never be forwarded.
    return urlrequest.build_opener(
        urlrequest.ProxyHandler(),
        urlrequest.HTTPHandler(),
        urlrequest.HTTPSHandler(),
        urlrequest.HTTPDefaultErrorHandler(),
        urlrequest.HTTPErrorProcessor(),
        _NoRedirectHandler(),
    )


def _deadline_from(
    timeout_s: float | None, deadline: Any
) -> tuple[Callable[[], float], Callable[[float], float]]:
    if deadline is not None and hasattr(deadline, "remaining") and hasattr(deadline, "require"):
        return deadline.remaining, deadline.require
    if timeout_s is None:
        raise DemoBackendError(504, "upstream_timeout")
    if timeout_s < MIN_REQUEST_TIMEOUT_S:
        raise DemoBackendError(504, "upstream_timeout")
    end = time.monotonic() + timeout_s

    def remaining() -> float:
        return end - time.monotonic()

    def require(minimum_s: float = MIN_REQUEST_TIMEOUT_S) -> float:
        rem = remaining()
        if rem < minimum_s:
            raise DemoBackendError(504, "upstream_timeout")
        return rem

    return remaining, require


def _read_response_body(response: Any, *, remaining: Callable[[], float], max_bytes: int) -> bytes:
    chunks = bytearray()
    total = 0
    sock = None
    try:
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None) if fp is not None else None
        sock = getattr(raw, "_sock", None) if raw is not None else None
    except Exception:  # pragma: no cover - defensive
        sock = None

    while True:
        rem = remaining()
        if rem < MIN_REQUEST_TIMEOUT_S:
            raise DemoBackendError(504, "upstream_timeout")
        if sock is not None:
            try:
                sock.settimeout(min(max(rem, 0.001), 5.0))
            except Exception:  # pragma: no cover - socket behavior varies
                pass
        try:
            if hasattr(response, "read1"):
                chunk = response.read1(64 * 1024)
            else:
                chunk = response.read(64 * 1024)
        except TimeoutError:
            raise DemoBackendError(504, "upstream_timeout") from None
        except (urlerror.URLError, OSError, ValueError):
            raise DemoBackendError(502, "upstream_unavailable") from None
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DemoBackendError(502, "upstream_invalid_response")
        chunks.extend(chunk)
    return bytes(chunks)


def _validate_upstream_status(response: Any, allowed: set[int]) -> None:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode() if hasattr(response, "getcode") else None
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        raise DemoBackendError(502, "upstream_invalid_response") from None
    if status_int in allowed:
        return
    if 300 <= status_int < 400:
        raise DemoBackendError(502, "upstream_redirect")
    if 200 <= status_int < 300:
        raise DemoBackendError(502, "upstream_invalid_response")
    raise DemoBackendError(status_int, "upstream_http_error")


def _open_json_request(
    method: str,
    url: str,
    token: str,
    *,
    timeout_s: float | None = None,
    deadline: Any = None,
    body: bytes | None = None,
    content_type: str | None = None,
    allowed_statuses: set[int],
    opener: urlrequest.OpenerDirector | None = None,
    max_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES,
) -> dict[str, Any]:
    remaining, require = _deadline_from(timeout_s, deadline)
    request_timeout = require()
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urlrequest.Request(url, data=body, headers=headers, method=method)
    opener = opener or _no_redirect_opener()
    try:
        with opener.open(req, timeout=request_timeout) as response:
            _validate_upstream_status(response, allowed_statuses)
            payload = _read_response_body(response, remaining=remaining, max_bytes=max_bytes)
    except DemoBackendError:
        raise
    except urlerror.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise DemoBackendError(502, "upstream_redirect") from None
        raise DemoBackendError(exc.code, "upstream_http_error") from None
    except TimeoutError:
        raise DemoBackendError(504, "upstream_timeout") from None
    except urlerror.URLError:
        raise DemoBackendError(502, "upstream_unavailable") from None
    except OSError:
        raise DemoBackendError(502, "upstream_unavailable") from None
    return _parse_json_object(payload)


class HttpOcrBackend:
    """Production backend using Google ID tokens against the private OCR API."""

    def __init__(self) -> None:
        self._opener = _no_redirect_opener()

    def submit(
        self,
        *,
        backend_url: str,
        token: str,
        file_bytes: bytes,
        idempotency_key: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]:
        body, content_type = _multipart_file("file", "document.pdf", "application/pdf", file_bytes)
        url = f"{backend_url}/v1/ocr/jobs"
        req = urlrequest.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Idempotency-Key", idempotency_key)
        req.add_header("Content-Type", content_type)
        remaining, require = _deadline_from(timeout_s, deadline)
        try:
            with self._opener.open(req, timeout=require()) as response:
                _validate_upstream_status(response, {200, 202})
                payload = _read_response_body(
                    response, remaining=remaining, max_bytes=MAX_UPSTREAM_RESPONSE_BYTES
                )
        except DemoBackendError:
            raise
        except urlerror.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise DemoBackendError(502, "upstream_redirect") from None
            raise DemoBackendError(exc.code, "upstream_http_error") from None
        except TimeoutError:
            raise DemoBackendError(504, "upstream_timeout") from None
        except (urlerror.URLError, OSError):
            raise DemoBackendError(502, "upstream_unavailable") from None
        return _parse_json_object(payload)

    def get_status(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]:
        job_id = _parse_job_id(job_id)
        return _open_json_request(
            "GET",
            f"{backend_url}/v1/ocr/jobs/{job_id}",
            token,
            timeout_s=timeout_s,
            deadline=deadline,
            allowed_statuses={200},
            opener=self._opener,
        )

    def get_result(
        self,
        *,
        backend_url: str,
        token: str,
        job_id: str,
        timeout_s: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]:
        job_id = _parse_job_id(job_id)
        return _open_json_request(
            "GET",
            f"{backend_url}/v1/ocr/jobs/{job_id}/result",
            token,
            timeout_s=timeout_s,
            deadline=deadline,
            allowed_statuses={200},
            opener=self._opener,
        )


def fetch_google_id_token(audience: str, timeout_s: float) -> str:
    """Obtain an ID token from Cloud Run metadata server. Never log the token."""
    if timeout_s < MIN_REQUEST_TIMEOUT_S:
        raise DemoBackendError(504, "upstream_timeout")
    query = urlparse.urlencode({"audience": audience, "format": "full"})
    url = f"http://{METADATA_TOKEN_HOST}{METADATA_TOKEN_PATH}?{query}"
    req = urlrequest.Request(url, headers={"Metadata-Flavor": "Google"}, method="GET")
    opener = _no_redirect_opener()
    end = time.monotonic() + timeout_s

    def remaining() -> float:
        return end - time.monotonic()

    try:
        with opener.open(req, timeout=min(timeout_s, 5.0)) as response:
            _validate_upstream_status(response, {200})
            payload = _read_response_body(
                response,
                remaining=remaining,
                max_bytes=MAX_METADATA_TOKEN_BYTES,
            )
    except DemoBackendError:
        raise
    except urlerror.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise DemoBackendError(502, "upstream_redirect") from None
        raise DemoBackendError(502, "token_unavailable") from None
    except TimeoutError:
        raise DemoBackendError(504, "upstream_timeout") from None
    except (urlerror.URLError, OSError, ValueError):
        raise DemoBackendError(502, "token_unavailable") from None
    try:
        token = payload.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        raise DemoBackendError(502, "token_unavailable") from None
    if not token:
        raise DemoBackendError(502, "token_unavailable")
    return token


def _authorized(presented: str | None, demo_key: str) -> bool:
    if presented is None:
        return False
    return secrets.compare_digest(presented, demo_key)


def _fixed_response(status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
    response_headers = dict(SECURITY_HEADERS)
    if headers:
        response_headers.update(headers)
    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


def _safe_log_category(code: str) -> str:
    return code if code in LOG_CATEGORIES else "internal_error"


def _safe_backend_error(exc: DemoBackendError) -> JSONResponse:
    category = _safe_log_category(exc.code)
    logger.warning("demo backend failure category=%s", category)
    if exc.code == "upstream_timeout" or exc.status_code == 504:
        return _fixed_response(504, SAFE_TIMEOUT)
    return _fixed_response(502, SAFE_ERROR)


def _submit_client_error(exc: DemoBackendError) -> JSONResponse | None:
    if exc.code != "upstream_http_error":
        return None
    if exc.status_code == 413:
        return _fixed_response(413, SAFE_TOO_LARGE)
    if exc.status_code == 415:
        return _fixed_response(415, SAFE_UNSUPPORTED)
    if exc.status_code in {400, 422}:
        return _fixed_response(400, SAFE_BAD_REQUEST)
    return None


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OCR Demo</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#f6f7f9; --card:#fff; --text:#182026; --muted:#5c6b7a;
      --line:#d8dee6; --accent:#0f766e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font: 16px/1.45 system-ui, sans-serif;
      background: var(--bg); color: var(--text);
    }
    main { max-width: 1100px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 12px; padding: 16px;
    }
    h1 { margin: 0 0 8px; font-size: 1.4rem; }
    p { margin: 0 0 12px; color: var(--muted); }
    label { display: block; font-weight: 600; margin: 12px 0 6px; }
    input[type=file], input[type=password], button { font: inherit; }
    input[type=password] {
      width: 100%; max-width: 360px; padding: 10px;
      border: 1px solid var(--line); border-radius: 8px;
    }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
    button { padding: 10px 14px; border: 0; border-radius: 8px; cursor: pointer; }
    #process { background: var(--accent); color: white; }
    #download { background: #334155; color: white; }
    button:disabled { opacity: .55; cursor: default; }
    #status { min-height: 1.5em; margin-top: 12px; color: var(--muted); }
    #preview {
      width: 100%; height: 420px; border: 1px solid var(--line);
      border-radius: 8px; background: #fff;
    }
    pre {
      margin: 0; padding: 12px; overflow: auto; background: #0b1020; color: #e7edf7;
      border-radius: 8px; min-height: 180px; white-space: pre-wrap; word-break: break-word;
    }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1>OCR Demo</h1>
    <p>Select a PDF, enter the demo passcode, then process it through the private
    OCR service. The response is shown as formatted JSON and can be downloaded.
    This page never embeds credentials.</p>
    <div class="row">
      <div>
        <label for="file">PDF file</label>
        <input id="file" name="file" type="file" accept="application/pdf,.pdf">
      </div>
      <div style="flex:1; min-width:240px;">
        <label for="passcode">Demo passcode (sent as X-Demo-Key)</label>
        <input id="passcode" type="password" autocomplete="off" spellcheck="false">
      </div>
      <button id="process" type="button">Process</button>
      <button id="download" type="button" disabled>Download JSON</button>
    </div>
    <div id="status" aria-live="polite"></div>
  </section>
  <section class="grid">
    <div class="card">
      <h2 style="margin-top:0;font-size:1rem;">PDF preview</h2>
      <iframe id="preview" title="PDF preview"></iframe>
    </div>
    <div class="card">
      <h2 style="margin-top:0;font-size:1rem;">OCR JSON response</h2>
      <pre id="output"></pre>
    </div>
  </section>
</main>
<script>
  const fileInput = document.getElementById('file');
  const passcode = document.getElementById('passcode');
  const processBtn = document.getElementById('process');
  const downloadBtn = document.getElementById('download');
  const statusEl = document.getElementById('status');
  const output = document.getElementById('output');
  const preview = document.getElementById('preview');
  let lastJson = null;
  let previewUrl = null;
  let requestGeneration = 0;

  fileInput.addEventListener('change', () => {
    requestGeneration += 1;
    lastJson = null;
    output.textContent = '';
    statusEl.textContent = '';
    downloadBtn.disabled = true;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    const file = fileInput.files[0];
    previewUrl = file ? URL.createObjectURL(file) : null;
    preview.src = previewUrl || 'about:blank';
  });

  processBtn.addEventListener('click', async () => {
    const file = fileInput.files[0];
    if (!file) { statusEl.textContent = 'Select a PDF file first.'; return; }
    if (!passcode.value) { statusEl.textContent = 'Enter the demo passcode.'; return; }
    const generation = ++requestGeneration;
    processBtn.disabled = true;
    fileInput.disabled = true;
    downloadBtn.disabled = true;
    lastJson = null;
    output.textContent = '';
    statusEl.textContent = 'Uploading and processing...';
    try {
      const body = new FormData();
      body.append('file', file);
      const response = await fetch('/api/ocr', {
        method: 'POST',
        headers: { 'X-Demo-Key': passcode.value },
        body
      });
      const json = await response.json();
      if (generation !== requestGeneration) return;
      lastJson = json;
      output.textContent = JSON.stringify(json, null, 2);
      downloadBtn.disabled = false;
      statusEl.textContent = response.ok ? 'Completed.' : 'Request failed.';
    } catch (err) {
      if (generation !== requestGeneration) return;
      output.textContent = JSON.stringify({
        error: { code: 'client_error', message: 'The browser request failed.' }
      }, null, 2);
      statusEl.textContent = 'Request failed.';
    } finally {
      if (generation === requestGeneration) {
        processBtn.disabled = false;
        fileInput.disabled = false;
      }
    }
  });

  downloadBtn.addEventListener('click', () => {
    if (!lastJson) return;
    const blob = new Blob([JSON.stringify(lastJson, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'ocr-response.json';
    a.click();
    URL.revokeObjectURL(url);
  });
</script>
</body>
</html>
"""


class _BodySizeLimitExceeded(Exception):
    pass


class _BodySizeLimitMiddleware:
    """ASGI guard for /api/ocr: key check before body, bounded body replay."""

    def __init__(self, app, *, settings: OcrDemoSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        is_protected = method == "POST" and path == "/api/ocr"
        if is_protected:
            headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
            if not _authorized(headers.get("x-demo-key"), self.settings.demo_key):
                await _send_json(send, 401, SAFE_UNAUTHORIZED)
                return

            max_body = self.settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
            content_length = headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    await _send_json(send, 400, SAFE_BAD_REQUEST)
                    return
                if declared < 0 or declared > max_body:
                    await _send_json(send, 413, SAFE_TOO_LARGE)
                    return
                receive = _bounded_receive(receive, max_body)
            else:
                receive = await _buffer_bounded_receive(receive, max_body, send)
                if receive is None:
                    return

        await self.app(scope, receive, _with_security_headers(send))


async def _send_json(send, status_code: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    headers = [(k.encode("latin1"), v.encode("latin1")) for k, v in SECURITY_HEADERS.items()]
    headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(payload)).encode("latin1")))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


def _with_security_headers(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            existing = {k.lower() for k, _ in headers}
            for name, value in SECURITY_HEADERS.items():
                key = name.encode("latin1").lower()
                if key not in existing:
                    headers.append((name.encode("latin1"), value.encode("latin1")))
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


def _bounded_receive(receive, max_body: int):
    seen = 0

    async def bounded():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            seen += len(body)
            if seen > max_body:
                raise _BodySizeLimitExceeded
        return message

    return bounded


async def _buffer_bounded_receive(receive, max_body: int, send):
    chunks: list[dict[str, Any]] = []
    seen = 0
    more = True
    while more:
        message = await receive()
        msg_type = message.get("type")
        if msg_type == "http.disconnect":
            # Client disconnected while buffering; stop cleanly.
            return None
        if msg_type != "http.request":
            chunks.append(message)
            continue
        body = message.get("body", b"")
        seen += len(body)
        if seen > max_body:
            await _send_json(send, 413, SAFE_TOO_LARGE)
            return None
        more = bool(message.get("more_body", False))
        chunks.append(message)

    iterator = iter(chunks)

    async def replay():
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    return replay


def create_demo_app(
    settings: OcrDemoSettings,
    *,
    backend: OcrBackend | None = None,
    token_fetcher: TokenFetcher | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    backend = backend or HttpOcrBackend()
    token_fetcher = token_fetcher or fetch_google_id_token
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic

    app = FastAPI(title="OCR demo service", version="0.1.0")
    app.state.demo_settings = settings

    @app.exception_handler(DemoBackendError)
    async def handle_backend_error(request: Request, exc: DemoBackendError) -> JSONResponse:
        return _safe_backend_error(exc)

    @app.exception_handler(_BodySizeLimitExceeded)
    async def handle_body_too_large(request: Request, exc: _BodySizeLimitExceeded) -> JSONResponse:
        return _fixed_response(413, SAFE_TOO_LARGE)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("demo backend failure category=%s", "validation_error")
        return _fixed_response(400, SAFE_BAD_REQUEST)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("demo backend failure category=%s", "internal_error")
        return _fixed_response(500, SAFE_ERROR)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        return HTMLResponse(HTML_PAGE, headers=SECURITY_HEADERS)

    @app.post("/api/ocr")
    def process_ocr(
        request: Request,
        file: Annotated[UploadFile | None, File()] = None,
        x_demo_key: Annotated[str | None, Header(alias="X-Demo-Key")] = None,
    ) -> JSONResponse:
        # Defense-in-depth: ASGI guard checks first, route verifies again.
        if not _authorized(x_demo_key, settings.demo_key):
            return _fixed_response(401, SAFE_UNAUTHORIZED)
        if file is None:
            return _fixed_response(400, SAFE_BAD_REQUEST)

        filename = (file.filename or "").lower()
        content_type = (file.content_type or "").split(";")[0].strip().lower()
        if not filename.endswith(".pdf") or content_type != "application/pdf":
            return _fixed_response(415, SAFE_UNSUPPORTED)

        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            return _fixed_response(413, SAFE_TOO_LARGE)
        if not data:
            return _fixed_response(400, SAFE_BAD_REQUEST)
        if not data.startswith(PDF_MAGIC):
            return _fixed_response(400, SAFE_BAD_REQUEST)

        deadline = _Deadline(settings.poll_timeout_s, clock)
        token = token_fetcher(settings.backend_url, deadline.require())
        idempotency_key = str(uuid.uuid4())
        try:
            submitted = backend.submit(
                backend_url=settings.backend_url,
                token=token,
                file_bytes=data,
                idempotency_key=idempotency_key,
                deadline=deadline,
            )
        except DemoBackendError as exc:
            mapped = _submit_client_error(exc)
            if mapped is not None:
                return mapped
            raise
        if not isinstance(submitted, dict):
            raise DemoBackendError(502, "upstream_invalid_response")

        job_id = _parse_job_id(submitted.get("job_id"))

        while True:
            deadline.require()
            status = backend.get_status(
                backend_url=settings.backend_url,
                token=token,
                job_id=job_id,
                deadline=deadline,
            )
            if not isinstance(status, dict):
                raise DemoBackendError(502, "upstream_invalid_response")
            state = _parse_state(status.get("state"))
            if state in FINAL_STATES:
                deadline.require()
                result = backend.get_result(
                    backend_url=settings.backend_url,
                    token=token,
                    job_id=job_id,
                    deadline=deadline,
                )
                if not isinstance(result, dict):
                    raise DemoBackendError(502, "upstream_invalid_response")
                return _fixed_response(200, result)
            remaining = deadline.remaining()
            if remaining < MIN_REQUEST_TIMEOUT_S:
                return _fixed_response(504, SAFE_TIMEOUT)
            sleeper(min(settings.poll_interval_s, max(0.0, remaining - MIN_REQUEST_TIMEOUT_S)))

    guarded = _BodySizeLimitMiddleware(app, settings=settings)
    # Preserve FastAPI attributes used by tests and tooling.
    guarded.state = app.state
    guarded.routes = app.routes
    guarded.router = app.router
    guarded.exception_handlers = app.exception_handlers
    guarded.openapi = app.openapi  # type: ignore[method-assign]
    return guarded  # type: ignore[return-value]


def build_app_from_env() -> FastAPI:
    return create_demo_app(OcrDemoSettings())

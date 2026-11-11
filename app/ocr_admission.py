"""Input and admission bounds for the OCR v1 API (issue #10).

Two bounds, both fail-closed and both applied before any work exists:

- **Bytes decide, never the caller's declaration** (contract §2.3): the container
  is sniffed from magic, then checked structurally — trailer, page/frame count,
  encryption, image dimensions — before a job is created or a page recognized.
- **The request is bounded before parsing** (§10/D38). An endpoint-side
  `file.read()` limit is too late to be the whole admission design: the multipart
  parser spools the body first, so `RequestSizeLimitMiddleware` refuses an
  oversized `Content-Length` up front and reads a request that declares nothing in
  bounded chunks instead of handing an unlimited stream downstream.

Every number here is a DEV-safe default, not a capacity or throughput claim. The
byte ceiling is the reviewed 20 MiB provisional value (§10/D38), the page limit is
the longest document actually measured (`docs/ocr-v1-benchmark-report.md` §2), and
the global ceiling is the contract's [B3]-gated placeholder. See
`docs/ocr-v1-admission.md`.

This is deliberately not a PDF or image parser: it is a bound, not a validator of
decoded content, so a container it accepts may still fail in the engine — which the
worker reports as `input_unreadable` rather than crashing on.
"""

import json
import re
import struct
from dataclasses import dataclass
from datetime import timedelta

from app.ocr_store import CAPACITY_OBJECT_BUDGET, MAX_ENTRY_BYTES, SCHEMA_VERSION

# Contract §10/D38: Cloud Run caps an HTTP/1 request at 32 MiB and it cannot be
# raised, so the upload ceiling is bounded by construction, not by preference.
CLOUD_RUN_HTTP1_MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# The longest labelled document measured on this project is 5 pages
# (docs/ocr-v1-benchmark-report.md §2). Provisional: a real >=20-page document is
# part of the outstanding representative-corpus gap [B3], and this default must not
# be read as a claim that longer documents work.
MAX_PAGES = 5
# No measured evidence exists for a pixel bound; A4 at 600 dpi is ~35 MP, so this
# provisional ceiling admits that and refuses a decompression bomb. Benchmark-gated
# [B3] like every other number here.
MAX_IMAGE_PIXELS = 40_000_000
# [B3]-gated placeholder (contract §6.4/D40): one job in flight globally. Small on
# purpose — a DEV-safe admission bound, never a throughput claim.
DEFAULT_CAPACITY_CEILING = 1
# The reviewed provisional per-attempt timeout (§10), which is also how long an
# admission entry may outlive its job before the next writer prunes it (§6.4).
PER_ATTEMPT_TIMEOUT = timedelta(minutes=30)

FILE_TOO_LARGE_MESSAGE = "file exceeds the size limit"

# `X-Request-Id` is caller-supplied and only ever echoed in a body or logged
# (contract §2.3), so it must be a bounded, safe token: a value outside this shape
# (a path, a fragment, a control character) is dropped rather than passed through,
# so no raw caller token or path can reach a response or a log line.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:+-]{1,128}")


def safe_request_id(value: str | None) -> str | None:
    """The caller's correlation id when it is a bounded safe token, else `None`.

    `X-Request-Id` is optional and never used for identity, dedupe, or storage; it
    is echoed in error bodies and logged. An unusable value is omitted (its field
    is `null`) instead of being echoed or logged verbatim.
    """
    return value if value and REQUEST_ID_PATTERN.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    """The configurable bounds one API instance enforces (see the module docstring)."""

    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_pages: int = MAX_PAGES
    max_image_pixels: int = MAX_IMAGE_PIXELS
    capacity_ceiling: int = DEFAULT_CAPACITY_CEILING
    per_attempt_timeout: timedelta = PER_ATTEMPT_TIMEOUT


@dataclass(frozen=True, slots=True)
class AdmittedDocument:
    """A submission the input boundary accepted: its container and its page count.

    `page_count` is what the byte scan could actually see, or `None` when it could
    not (a PDF page tree inside an object stream is invisible here). It is
    persisted with the job so a worker that refuses the container can still
    account for one page per known source page instead of inventing a count.
    """

    mime_type: str
    page_count: int | None


class InputRejected(Exception):
    """A submission refused on its bytes; rendered as the allowlisted error body."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True, slots=True)
class _Structure:
    """What the bytes themselves carry: pages, largest frame, encryption, damage."""

    pages: int | None = None
    pixels: int = 0
    encrypted: bool = False
    malformed: bool = False


def assert_limits(limits: AdmissionLimits) -> None:
    """Startup invariants for the configured bounds (contract §10, D38, D40).

    A misconfigured deployment fails at startup rather than admitting one
    oversized request or an unbounded admission set.
    """
    if not 1 <= limits.max_upload_bytes <= CLOUD_RUN_HTTP1_MAX_REQUEST_BYTES:
        raise ValueError("max_upload_bytes is outside the Cloud Run HTTP/1 request ceiling")
    if limits.max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if limits.max_image_pixels < 1:
        raise ValueError("max_image_pixels must be at least 1")
    if limits.capacity_ceiling < 1:
        raise ValueError("capacity_ceiling must be at least 1")
    if limits.capacity_ceiling * MAX_ENTRY_BYTES > CAPACITY_OBJECT_BUDGET:
        raise ValueError("ceiling x max_entry_bytes exceeds the capacity object budget")


# Container gate: bytes decide, never the declared type (§2.3). Archive and Office
# magic are absent on purpose, so they are unsupported without being enumerated.
CONTAINER_MAGIC = (
    ("application/pdf", b"%PDF-"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/tiff", b"II*\x00"),
    ("image/tiff", b"MM\x00*"),
)


def sniff_container(content: bytes) -> str | None:
    """The container these bytes are, or `None` when it is not one of the four."""
    for mime_type, magic in CONTAINER_MAGIC:
        if content.startswith(magic):
            return mime_type
    return None


def validate_document(
    content: bytes, declared_type: str, limits: AdmissionLimits
) -> AdmittedDocument:
    """Check one submission's bytes; return its container and known page count.

    Order follows contract §2.3 — empty, container/mismatch, byte length, page or
    frame count, encryption, structural damage — so an input that breaks two rules
    reports the same code as an input that breaks only the earlier one.
    """
    if not content:
        raise InputRejected(422, "empty_document")
    mime_type = sniff_container(content)
    if mime_type is None or mime_type != declared_type:
        raise InputRejected(415, "unsupported_media_type")
    if len(content) > limits.max_upload_bytes:
        raise InputRejected(413, "file_too_large")

    structure = _STRUCTURE_READERS[mime_type](content, limits)
    if structure.pages is not None and structure.pages > limits.max_pages:
        raise InputRejected(422, "too_many_pages")
    if structure.encrypted:
        raise InputRejected(422, "encrypted_document")
    if structure.malformed:
        raise InputRejected(422, "malformed_document")
    if structure.pixels > limits.max_image_pixels:
        # §2.7's vocabulary is closed and has no dimension code; an over-limit
        # raster is reported as the over-limit input it is rather than by
        # inventing a code that §11.1 would otherwise reject.
        raise InputRejected(413, "file_too_large")
    # Zero visible pages means "the byte scan cannot see the page tree", never
    # "this document has no pages": the count stays unknown rather than wrong.
    return AdmittedDocument(mime_type, structure.pages or None)


# --- structural readers (bytes only; no decoding, no rendering) ------------------

_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![s])")


def _pdf_structure(content: bytes, limits: AdmissionLimits) -> _Structure:
    # A truncated PDF never wrote its trailer; trailing bytes after `%%EOF` are
    # common in files that were appended to, so the marker is searched, not anchored.
    if b"%%EOF" not in content:
        return _Structure(malformed=True)
    pages = len(_PDF_PAGE.findall(content))
    # §2.3: a header with neither a page object nor a trailer is not a document.
    # shortcut: a page tree stored inside an object stream is invisible to this
    # byte scan, so such a PDF undercounts here; the engine's own page count is the
    # second gate (the worker refuses an over-limit document before recognizing it).
    malformed = pages == 0 and b"trailer" not in content
    return _Structure(pages=pages, encrypted=b"/Encrypt" in content, malformed=malformed)


def _png_structure(content: bytes, limits: AdmissionLimits) -> _Structure:
    if len(content) < 33 or content[12:16] != b"IHDR":
        return _Structure(malformed=True)
    width, height = struct.unpack(">II", content[16:24])
    if b"IEND" not in content[8:]:
        return _Structure(malformed=True)
    return _Structure(pages=1, pixels=width * height)


_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                               0xCD, 0xCE, 0xCF})


def _jpeg_structure(content: bytes, limits: AdmissionLimits) -> _Structure:
    if not content.endswith(b"\xff\xd9"):
        return _Structure(malformed=True)
    dimensions = _jpeg_dimensions(content)
    if dimensions is None:
        return _Structure(malformed=True)
    width, height = dimensions
    return _Structure(pages=1, pixels=width * height)


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    """Walk the marker segments to the frame header that carries width/height."""
    offset = 2
    while offset + 4 <= len(content):
        if content[offset] != 0xFF:
            return None
        if content[offset + 1] in _JPEG_SOF_MARKERS:
            if offset + 9 > len(content):
                return None
            height = int.from_bytes(content[offset + 5 : offset + 7], "big")
            width = int.from_bytes(content[offset + 7 : offset + 9], "big")
            return (width, height) if width and height else None
        length = int.from_bytes(content[offset + 2 : offset + 4], "big")
        if length < 2:
            return None
        offset += 2 + length
    return None


def _tiff_structure(content: bytes, limits: AdmissionLimits) -> _Structure:
    """Count IFDs (one frame each) and take the largest frame's dimensions.

    The walk stops one frame past the configured limit, so an enormous TIFF costs
    a bounded number of steps while its count still exceeds the limit.
    """
    if len(content) < 8:
        return _Structure(malformed=True)
    order = "little" if content[:2] == b"II" else "big"
    offset = int.from_bytes(content[4:8], order)
    frames = 0
    pixels = 0
    seen: set[int] = set()
    while offset:
        if offset in seen or frames > limits.max_pages:
            break
        seen.add(offset)
        if offset + 2 > len(content):
            return _Structure(malformed=True)
        entries = int.from_bytes(content[offset : offset + 2], order)
        end = offset + 2 + 12 * entries
        if end + 4 > len(content):
            return _Structure(malformed=True)
        width = height = 0
        for index in range(entries):
            entry = offset + 2 + 12 * index
            tag = int.from_bytes(content[entry : entry + 2], order)
            kind = int.from_bytes(content[entry + 2 : entry + 4], order)
            if tag in (256, 257) and kind in (3, 4):
                size = 2 if kind == 3 else 4
                value = int.from_bytes(content[entry + 8 : entry + 8 + size], order)
                if tag == 256:
                    width = value
                else:
                    height = value
        if width and height:
            pixels = max(pixels, width * height)
        frames += 1
        offset = int.from_bytes(content[end : end + 4], order)
    if frames == 0:
        return _Structure(malformed=True)
    return _Structure(pages=frames, pixels=pixels)


def _unsupported_structure(content: bytes, limits: AdmissionLimits) -> _Structure:
    return _Structure(malformed=True)


_STRUCTURE_READERS = {
    "application/pdf": _pdf_structure,
    "image/jpeg": _jpeg_structure,
    "image/png": _png_structure,
    "image/tiff": _tiff_structure,
}


# --- the request-size bound, ahead of multipart parsing (§10/D38) ----------------


class RequestSizeLimitMiddleware:
    """Bound the request body before any parser or spooler sees it.

    A declared `Content-Length` over the ceiling is refused without reading the
    body at all. A request that declares nothing (chunked, or HTTP/2 without the
    header) is read here in bounded chunks and replayed to the application, so a
    multipart parser can never spool more than `max_bytes` to memory or disk.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _send_too_large(scope, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnected client: nothing was parsed, so nothing is spooled.
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await _send_too_large(scope, send)
                return
            if not message.get("more_body"):
                break

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def _declared_length(scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _send_too_large(scope, send) -> None:
    """The same allowlisted error body the endpoint would have produced."""
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "error": {
                "code": "file_too_large",
                "message": FILE_TOO_LARGE_MESSAGE,
                "request_id": safe_request_id(_header(scope, b"x-request-id")),
            },
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})

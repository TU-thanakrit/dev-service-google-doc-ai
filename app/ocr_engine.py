"""Real CPU recognition engine behind the application-owned `OcrEngine` seam (issue #7).

One document in, one numbered page out: `page_count` decodes the container for
its source page count and `recognize_page` recognizes exactly one page, so a
restart re-issues only the pages without a valid checkpoint (§5.5/D41).

Per page a PDF prefers its own **usable** text layer and only rasterises when
that layer fails the reviewed check; every other container is rendered/decoded
and OCR'd. Text source and warnings are published per page (§2.6/D11/D23), and
every executable failure becomes a page error from the contract's closed
vocabulary (§2.7) instead of an exception the worker cannot classify. No
classification, structured extraction, or model call is made, and **no document
text, path, or process output is ever logged** (D20).

The DEV engine is the measured, provisional Tesseract `tha`+`eng` selection
(`docs/ocr-v1-benchmark-report.md` §6); production selection stays blocked on a
representative corpus and approved thresholds. An adapter declares its own
operator-only `engine_ref` (§8), and `EngineRegistry` is how a worker resolves the
engine recorded in an accepted job rather than the one this deployment happens to
be configured with (`docs/ocr-v1-pinning.md`). Every external process is injected
as a `CommandRunner`, so the adapter is exercised with a fake runner in tests and
with the real binaries only in the env-gated integration test.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.ocr_config import OcrServiceSettings
from app.ocr_pinning import PinError
from app.ocr_tracer import EnginePage, EnginePageError, OcrEngine

logger = logging.getLogger(__name__)

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"
TIFF = "image/tiff"

# JPEG and PNG are single-page; a TIFF may hold one frame per source page (D24).
SINGLE_PAGE_TYPES = (JPEG, PNG)
IMAGE_SUFFIX = {JPEG: ".jpg", PNG: ".png"}

# Published page fields (§2.6/D11/D23): the two text sources and the one warning
# this adapter can decide from evidence it actually has.
EMBEDDED = "embedded"
OCR = "ocr"
NO_TEXT = "no_text"

# Page-error vocabulary (§2.7) as reviewed for this slice: the transient
# category may be retried by the application, the permanent one never is, and
# the application — never the engine — owns that decision.
TRANSIENT_ERROR = "page_engine_error"
PERMANENT_ERROR = "malformed_document"

# The provisional DEV engine/model. `tha+eng` is the measured selection; the
# language string is data, not a claim about accuracy.
DEFAULT_LANG = "tha+eng"
DEFAULT_PSM = 3
DEFAULT_RASTER_DPI = 300

# Strict per-call ceilings so one hung binary cannot stall a worker. The
# deadline-scoped page timeout of §5.5/D42 is ticket #11's number.
DEFAULT_DECODE_TIMEOUT_S = 30.0
DEFAULT_RENDER_TIMEOUT_S = 120.0
DEFAULT_OCR_TIMEOUT_S = 120.0

_PAGES_LINE = re.compile(rb"^Pages:\s*(\d+)\s*$", re.MULTILINE)
REPLACEMENT_CHARACTER = "\ufffd"

# ImageMagick `convert`/`identify` read and split image containers. poppler and
# Tesseract cannot select one frame of a multi-frame TIFF, so a TIFF is decoded
# through ImageMagick and OCR'd as a single PNG frame.
IMAGE_CONVERT = "convert"
IMAGE_IDENTIFY = "identify"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandError(Exception):
    """The command could not be executed at all (typically a missing binary)."""


class CommandTimeout(Exception):
    """The command exceeded its strict timeout and was killed."""


class CommandRunner(Protocol):
    """The injected process seam: one argv, a strict timeout, no shell."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """Real runner: `subprocess.run` on an argv list, bytes captured, no shell.

    A timeout is raised as `CommandTimeout` and a spawn failure as
    `CommandError`, so the adapter maps them to a closed page code instead of
    letting a `subprocess` exception escape.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        child_env = None if env is None else {**os.environ, **env}
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                input=stdin,
                timeout=timeout,
                env=child_env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeout(_program(argv)) from exc
        except OSError as exc:
            raise CommandError(_program(argv)) from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CommandOcrEngine:
    """Per-page recognition over poppler, Tesseract, and (for TIFF) ImageMagick.

    Satisfies the `OcrEngine` seam (`docs/ocr-v1-contracts.md` §11.2 S3). All
    external programs and their ceilings are constructor-injected, so the class
    holds no module-level process state and a test can drive it without a binary.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        engine_ref: str = "",
        pdfinfo: str = "pdfinfo",
        pdftotext: str = "pdftotext",
        pdftoppm: str = "pdftoppm",
        tesseract: str = "tesseract",
        image_convert: str = IMAGE_CONVERT,
        image_identify: str = IMAGE_IDENTIFY,
        lang: str = DEFAULT_LANG,
        psm: int = DEFAULT_PSM,
        raster_dpi: int = DEFAULT_RASTER_DPI,
        tessdata_prefix: str | None = None,
        decode_timeout_s: float = DEFAULT_DECODE_TIMEOUT_S,
        render_timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
        ocr_timeout_s: float = DEFAULT_OCR_TIMEOUT_S,
        workdir: str | Path | None = None,
    ) -> None:
        self._runner = runner
        self.engine_ref = engine_ref
        self._pdfinfo = pdfinfo
        self._pdftotext = pdftotext
        self._pdftoppm = pdftoppm
        self._tesseract = tesseract
        self._image_convert = image_convert
        self._image_identify = image_identify
        self._lang = lang
        self._psm = psm
        self._raster_dpi = raster_dpi
        self._tessdata_prefix = tessdata_prefix
        self._decode_timeout = decode_timeout_s
        self._render_timeout = render_timeout_s
        self._ocr_timeout = ocr_timeout_s
        self._workdir = None if workdir is None else str(workdir)

    # --- OcrEngine seam ------------------------------------------------------

    def page_count(self, content: bytes, mime_type: str) -> int:
        """Source page/frame count, without recognizing anything (§2.6/D24)."""
        if mime_type == PDF:
            return self._pdf_page_count(content)
        if mime_type in SINGLE_PAGE_TYPES:
            return 1
        if mime_type == TIFF:
            return self._tiff_frame_count(content)
        raise self._error(0, PERMANENT_ERROR)

    def recognize_page(self, content: bytes, mime_type: str, page_number: int) -> EnginePage:
        """Recognize one page, preferring a usable embedded PDF text layer (D11)."""
        if page_number < 1:
            raise self._error(page_number, PERMANENT_ERROR)
        if mime_type == PDF:
            return self._pdf_page(content, page_number)
        if mime_type in SINGLE_PAGE_TYPES:
            if page_number != 1:
                raise self._error(page_number, PERMANENT_ERROR)
            return self._ocr_file(content, IMAGE_SUFFIX[mime_type], page_number)
        if mime_type == TIFF:
            return self._tiff_frame(content, page_number)
        raise self._error(page_number, PERMANENT_ERROR)

    # --- containers ----------------------------------------------------------

    def _pdf_page_count(self, content: bytes) -> int:
        with self._scratch(content, ".pdf") as path:
            try:
                result = self._runner.run([self._pdfinfo, str(path)], timeout=self._decode_timeout)
            except (CommandTimeout, CommandError):
                raise self._error(0, TRANSIENT_ERROR) from None
        count = _pages_line(result.stdout) if result.returncode == 0 else None
        if count is None or count < 1:
            raise self._error(0, PERMANENT_ERROR)
        return count

    def _tiff_frame_count(self, content: bytes) -> int:
        with self._scratch(content, ".tiff") as path:
            try:
                # `identify` prints the format once per frame, so the format ends
                # in a newline and the first line carries the count.
                result = self._runner.run(
                    [self._image_identify, "-format", "%n\n", str(path)],
                    timeout=self._decode_timeout,
                )
            except (CommandTimeout, CommandError):
                raise self._error(0, TRANSIENT_ERROR) from None
        frames = _frame_count(result.stdout) if result.returncode == 0 else None
        if frames is None:
            raise self._error(0, PERMANENT_ERROR)
        return frames

    # --- one page ------------------------------------------------------------

    def _pdf_page(self, content: bytes, page_number: int) -> EnginePage:
        with self._scratch(content, ".pdf") as path:
            embedded = self._embedded_text(path, page_number)
            if embedded is not None:
                return EnginePage(text=embedded, text_source=EMBEDDED)
            return self._ocr_image(self._render_page(path, page_number), page_number)

    def _embedded_text(self, path: Path, page_number: int) -> str | None:
        """The page's usable text layer, or `None` when the page must be OCR'd.

        Every unusable outcome — a non-zero exit, an unreadable layer, a
        whitespace-only or replacement-character layer, and even a decode failure
        — falls back to OCR rather than failing the page, because a text layer is
        an optimisation and never the only path to a result (D11).
        """
        argv = [
            self._pdftotext,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            str(path),
            "-",
        ]
        try:
            result = self._runner.run(argv, timeout=self._decode_timeout)
        except (CommandTimeout, CommandError):
            return None
        if result.returncode != 0:
            return None
        return _usable_embedded(result.stdout)

    def _render_page(self, path: Path, page_number: int) -> Path:
        prefix = path.parent / "page"
        argv = [
            self._pdftoppm,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            str(self._raster_dpi),
            "-png",
            str(path),
            str(prefix),
        ]
        try:
            result = self._runner.run(argv, timeout=self._render_timeout)
        except (CommandTimeout, CommandError):
            raise self._error(page_number, TRANSIENT_ERROR) from None
        if result.returncode != 0:
            raise self._error(page_number, PERMANENT_ERROR)
        rendered = sorted(path.parent.glob(f"{prefix.name}*.png"))
        if len(rendered) != 1:
            # `-f`/`-l` pin exactly one page, so anything else means the page
            # could not be decoded (no image) or was not pinned (several images).
            raise self._error(page_number, PERMANENT_ERROR)
        return rendered[0]

    def _tiff_frame(self, content: bytes, page_number: int) -> EnginePage:
        with self._scratch(content, ".tiff") as path:
            target = path.parent / "frame.png"
            argv = [self._image_convert, f"{path}[{page_number - 1}]", str(target)]
            try:
                result = self._runner.run(argv, timeout=self._render_timeout)
            except (CommandTimeout, CommandError):
                raise self._error(page_number, TRANSIENT_ERROR) from None
            if result.returncode != 0 or not target.exists():
                raise self._error(page_number, PERMANENT_ERROR)
            return self._ocr_image(target, page_number)

    def _ocr_file(self, content: bytes, suffix: str, page_number: int) -> EnginePage:
        with self._scratch(content, suffix) as path:
            return self._ocr_image(path, page_number)

    def _ocr_image(self, path: Path, page_number: int) -> EnginePage:
        argv = [
            self._tesseract,
            str(path),
            "stdout",
            "-l",
            self._lang,
            "--psm",
            str(self._psm),
        ]
        env = {"TESSDATA_PREFIX": self._tessdata_prefix} if self._tessdata_prefix else None
        try:
            result = self._runner.run(argv, timeout=self._ocr_timeout, env=env)
        except (CommandTimeout, CommandError):
            raise self._error(page_number, TRANSIENT_ERROR) from None
        if result.returncode != 0:
            # Tesseract exiting non-zero is a recognition hiccup, not proof that
            # the input is undecodable, so it stays retryable (PAGE_RETRIES).
            raise self._error(page_number, TRANSIENT_ERROR)
        return _page_from_text(result.stdout)

    # --- helpers -------------------------------------------------------------

    @contextmanager
    def _scratch(self, content: bytes, suffix: str) -> Iterator[Path]:
        """One per-call scratch directory holding the input, removed on return.

        Temporary page artifacts never outlive the call and never live inside the
        repository (organization-controlled processing, §10).
        """
        with tempfile.TemporaryDirectory(prefix="ocr-engine-", dir=self._workdir) as scratch:
            path = Path(scratch) / f"input{suffix}"
            path.write_bytes(content)
            yield path

    def _error(self, page_number: int, code: str) -> EnginePageError:
        # Allowlisted fields only: never a path, an argument, or any output (D20).
        logger.error("ocr_engine failure error_code=%s page_number=%s", code, page_number)
        return EnginePageError(code)


def build_engine(settings: OcrServiceSettings) -> CommandOcrEngine:
    """The recognition engine this deployment's `engine_ref` names (issue #7).

    Reads only the engine settings; the binary names default to `PATH`. The
    declared `engine_ref` is the operator-only identity of §8 and is what the
    worker matches against the job's recorded pin: a deployment whose settings
    resolve to another configuration cannot serve a job pinned to this one, and
    says so instead of publishing text under the wrong pin.
    """
    return CommandOcrEngine(
        SubprocessRunner(),
        engine_ref=settings.resolved_engine_ref(),
        tesseract=settings.engine_tesseract,
        lang=settings.engine_lang,
        raster_dpi=settings.engine_raster_dpi,
        tessdata_prefix=settings.engine_tessdata_prefix,
    )


class UnknownEngineRef(PinError):
    """No builder is registered for an `engine_ref` (§8).

    Not a transient condition and never a fallback: a job whose engine is no
    longer registered is refused, because the only other engines available are
    the ones this deployment happens to be configured with now.
    """


class EngineRegistry:
    """The engines one deployment can still serve, keyed by `engine_ref` (§8).

    Operator-wired: register every `engine_ref` a *live* job may still carry, not
    just the current one, because an accepted job keeps the engine it was
    accepted under for its whole lifetime (`docs/ocr-v1-pinning.md`). The worker
    resolves through this registry using the ref stored in the job, so a job
    accepted before an operator change keeps its own engine, and a ref with no
    registered builder is refused rather than silently substituted.
    """

    def __init__(self, builders: Mapping[str, Callable[[], OcrEngine]]) -> None:
        self._builders = dict(builders)

    def build(self, engine_ref: str) -> OcrEngine:
        """The engine registered for `engine_ref`, validated against its own claim."""
        builder = self._builders.get(engine_ref)
        if builder is None:
            raise UnknownEngineRef(f"no engine is registered for {engine_ref}")
        engine = builder()
        if engine.engine_ref != engine_ref:
            # A builder that answers with another engine is a wiring defect: D16
            # makes the resolved engine/config disagreement a refusal, not a
            # warning, and this is the only place the two are compared before any
            # page is recognized.
            raise PinError(
                f"the engine registered for {engine_ref} declares {engine.engine_ref}"
            )
        return engine


def _program(argv: Sequence[str]) -> str:
    return Path(argv[0]).name


def _pages_line(stdout: bytes) -> int | None:
    match = _PAGES_LINE.search(stdout)
    return int(match.group(1)) if match else None


def _frame_count(stdout: bytes) -> int | None:
    """The first reported frame count; `identify` emits one line per frame."""
    for line in stdout.decode("ascii", "replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = int(stripped)
        except ValueError:
            return None
        return value if value >= 1 else None
    return None


def _usable_embedded(stdout: bytes) -> str | None:
    """The NFC-normalised text layer, or `None` when it is not usable (D11).

    Usable means at least one non-whitespace character after normalization and no
    replacement character; anything else is OCR'd instead.
    """
    text = unicodedata.normalize("NFC", stdout.decode("utf-8", "replace"))
    if REPLACEMENT_CHARACTER in text or not text.strip():
        return None
    return text


def _page_from_text(stdout: bytes) -> EnginePage:
    """One recognized page; a blank result is a success with the `no_text` warning.

    No confidence value is invented and no quality signal is guessed, so
    `possible_low_quality` is deliberately never emitted ([B3]).
    """
    text = stdout.decode("utf-8", "replace")
    warnings = () if text.strip() else (NO_TEXT,)
    return EnginePage(text=text, text_source=OCR, warnings=warnings)

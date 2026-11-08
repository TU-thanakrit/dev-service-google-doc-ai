"""Unit tests for the real CPU OCR engine adapter (issue #7).

Fake command runner only: no system binary is executed, no cloud client is
constructed, and no document is uploaded. The runner stands in for an external
process *including the files it writes*, so the adapter's post-conditions
(pdftoppm's rendered `<prefix>-NN.png`, `convert`'s named output file) are
observed exactly as they are in production.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ocr_config import OcrServiceSettings
from app.ocr_engine import (
    EMBEDDED,
    NO_TEXT,
    OCR,
    PERMANENT_ERROR,
    TRANSIENT_ERROR,
    CommandOcrEngine,
    CommandResult,
    CommandTimeout,
    ProvisionalEngineNotApproved,
    build_engine,
)
from app.ocr_tracer import (
    EnginePageError,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
)
from app.ocr_tracer_fakes import (
    FAKE_ENGINE_REF,
    ControllableDispatcher,
    FakeTokenVerifier,
    InMemoryObjectStore,
    InMemoryOcrStore,
    fake_engine_registry,
)
from app.ocr_worker import run_worker

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"
TIFF = "image/tiff"

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
TIFF_BYTES = b"II*\x00"
TEAM = "team-a"
EMAIL = "analyst-a@example.com"
TOKEN = "tok-a"


def ok(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


class ScriptedRunner:
    """`CommandRunner` double keyed by program name.

    A handler receives the argv and returns a `CommandResult`; it may also write
    the files the real program would write. Programs listed in `timeout` raise
    `CommandTimeout` instead, so a strict-timeout path is exercised.
    """

    def __init__(self, handlers=None, *, timeout=()):
        self.handlers = dict(handlers or {})
        self.timeout = set(timeout)
        self.calls: list[tuple[list[str], float, dict[str, str] | None]] = []

    def run(self, argv, *, timeout, stdin=None, env=None):
        argv = list(argv)
        self.calls.append((argv, timeout, env))
        program = Path(argv[0]).name
        if program in self.timeout:
            raise CommandTimeout(program)
        handler = self.handlers.get(program)
        if handler is None:
            raise AssertionError(f"unexpected command: {argv}")
        return handler(argv)

    def programs(self) -> list[str]:
        return [Path(argv[0]).name for argv, _, _ in self.calls]


class FakeMonotonic:
    """An explicit monotonic source: the test moves it, so elapsed time is exact."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


class ElapsingRunner(ScriptedRunner):
    """A runner whose every command advances the injected monotonic by `step`."""

    def __init__(self, clock, *, step, handlers=None, timeout=()):
        super().__init__(handlers, timeout=timeout)
        self._clock = clock
        self._step = step

    def run(self, argv, *, timeout, stdin=None, env=None):
        result = super().run(argv, timeout=timeout, stdin=stdin, env=env)
        self._clock.value += self._step
        return result


def render(argv) -> CommandResult:
    """Emulate pdftoppm writing one PNG at `<prefix>-1.png`."""
    prefix = Path(argv[-1])
    (prefix.parent / f"{prefix.name}-1.png").write_bytes(PNG_BYTES)
    return ok()


def frame(argv) -> CommandResult:
    """Emulate ImageMagick `convert` writing the named output file."""
    Path(argv[-1]).write_bytes(PNG_BYTES)
    return ok()


def pdf_ocr_runner(text: bytes = b"ocr text\n", *, timeout=()) -> ScriptedRunner:
    """A runner whose PDF pages have no usable text layer, so OCR is required."""
    return ScriptedRunner(
        {
            "pdftotext": lambda argv: ok(b"\n \n"),
            "pdftoppm": render,
            "tesseract": lambda argv: ok(text),
        },
        timeout=timeout,
    )


# --- page_count --------------------------------------------------------------


def test_page_count_reads_the_pdf_page_count_from_pdfinfo():
    runner = ScriptedRunner({"pdfinfo": lambda argv: ok(b"Title: x\nPages:   4\n")})

    assert CommandOcrEngine(runner).page_count(PDF_BYTES, PDF) == 4

    argv, timeout, env = runner.calls[0]
    assert argv[0] == "pdfinfo"
    assert argv[1].endswith(".pdf")
    assert timeout == 30.0
    assert env is None


def test_page_count_is_one_for_single_page_images_without_running_a_command():
    runner = ScriptedRunner()
    engine = CommandOcrEngine(runner)

    assert engine.page_count(b"\xff\xd8\xff", JPEG) == 1
    assert engine.page_count(PNG_BYTES, PNG) == 1
    assert runner.calls == []


def test_page_count_counts_tiff_frames_with_identify():
    # `identify` prints its format once per frame; the first line is the count.
    runner = ScriptedRunner({"identify": lambda argv: ok(b"3\n3\n3\n")})

    assert CommandOcrEngine(runner).page_count(TIFF_BYTES, TIFF) == 3

    argv, timeout, _ = runner.calls[0]
    assert argv[0] == "identify"
    assert argv[1:3] == ["-format", "%n\n"]
    assert argv[3].endswith(".tiff")
    assert timeout == 30.0


def test_page_count_fails_closed_on_an_undecodable_pdf():
    for result in (ok(b"Syntax Error\n", returncode=1), ok(b"no page count here\n")):
        runner = ScriptedRunner({"pdfinfo": lambda argv, result=result: result})
        with pytest.raises(EnginePageError) as excinfo:
            CommandOcrEngine(runner).page_count(PDF_BYTES, PDF)
        assert excinfo.value.code == PERMANENT_ERROR


def test_page_count_unsupported_media_type_fails_closed():
    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(ScriptedRunner()).page_count(b"PK\x03\x04", "application/zip")
    assert excinfo.value.code == PERMANENT_ERROR


def test_a_decode_timeout_is_the_transient_engine_error():
    runner = ScriptedRunner(timeout={"pdfinfo"})

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner).page_count(PDF_BYTES, PDF)

    assert excinfo.value.code == TRANSIENT_ERROR


# --- embedded PDF text (D11) -------------------------------------------------


def test_a_usable_embedded_pdf_text_layer_is_used_without_running_ocr():
    runner = ScriptedRunner({"pdftotext": lambda argv: ok("ใบเสร็จรับเงิน\n".encode())})

    page = CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 1)

    assert page.text_source == EMBEDDED
    assert page.text.strip() == "ใบเสร็จรับเงิน"
    assert page.warnings == ()
    assert runner.programs() == ["pdftotext"]

    argv, timeout, _ = runner.calls[0]
    assert argv[:5] == ["pdftotext", "-f", "1", "-l", "1"]
    assert argv[-1] == "-"
    assert timeout == 30.0


def test_embedded_text_is_nfc_normalised_before_it_is_published():
    # A decomposed Latin sequence in the text layer is published in NFC form.
    # (NFC does NOT recompose SARA AM -- U+0E33 is a composition exclusion -- so
    # a decomposed Thai layer is passed through unchanged, never folded.)
    runner = ScriptedRunner({"pdftotext": lambda argv: ok("cafe\u0301 ใบเสร็จ\n".encode())})

    page = CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 1)

    assert page.text_source == EMBEDDED
    assert page.text.strip() == "caf\u00e9 ใบเสร็จ"


@pytest.mark.parametrize("layer", [b"   \n\t\n", "receipt \ufffd text\n".encode()])
def test_unusable_embedded_text_falls_back_to_ocr(layer):
    runner = ScriptedRunner(
        {
            "pdftotext": lambda argv, layer=layer: ok(layer),
            "pdftoppm": render,
            "tesseract": lambda argv: ok(b"ocr text\n"),
        }
    )

    page = CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 2)

    assert page.text_source == OCR
    assert page.text.strip() == "ocr text"
    assert runner.programs() == ["pdftotext", "pdftoppm", "tesseract"]
    # The rendered page is OCR'd, so the render must be pinned to that page.
    pdftoppm_argv = runner.calls[1][0]
    assert pdftoppm_argv[:7] == ["pdftoppm", "-f", "2", "-l", "2", "-r", "300"]
    assert pdftoppm_argv[7] == "-png"
    tesseract_argv = runner.calls[2][0]
    assert tesseract_argv[2:] == ["stdout", "-l", "tha+eng", "--psm", "3"]


def test_a_pdf_page_that_renders_no_image_fails_closed():
    runner = ScriptedRunner(
        {"pdftotext": lambda argv: ok(b"\n"), "pdftoppm": lambda argv: ok()}
    )

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 99)

    assert excinfo.value.code == PERMANENT_ERROR


# --- OCR failures ------------------------------------------------------------


def test_a_failed_ocr_call_is_the_transient_page_error():
    runner = pdf_ocr_runner(**{"timeout": {"tesseract"}})

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 1)

    assert excinfo.value.code == TRANSIENT_ERROR


def test_a_failed_render_is_never_a_silent_empty_page():
    runner = ScriptedRunner(
        {
            "pdftotext": lambda argv: ok(b"\n"),
            "pdftoppm": lambda argv: ok(b"", returncode=1),
            "tesseract": lambda argv: ok(b"unreachable\n"),
        }
    )

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 1)

    assert excinfo.value.code == PERMANENT_ERROR
    assert runner.programs() == ["pdftotext", "pdftoppm"]


# --- JPEG / PNG / TIFF -------------------------------------------------------


def test_a_jpeg_is_recognized_directly():
    runner = ScriptedRunner({"tesseract": lambda argv: ok(b"hello\n")})

    page = CommandOcrEngine(runner).recognize_page(b"\xff\xd8\xff", JPEG, 1)

    assert (page.text_source, page.text.strip()) == (OCR, "hello")
    argv, timeout, _ = runner.calls[0]
    assert argv[0] == "tesseract"
    assert argv[1].endswith(".jpg")
    assert timeout == 120.0


def test_a_png_page_number_beyond_its_single_page_fails_closed():
    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(ScriptedRunner()).recognize_page(PNG_BYTES, PNG, 2)
    assert excinfo.value.code == PERMANENT_ERROR


def test_a_tiff_frame_is_extracted_before_it_is_recognized():
    runner = ScriptedRunner(
        {
            "convert": frame,
            "tesseract": lambda argv: ok(b"frame two\n"),
        }
    )

    page = CommandOcrEngine(runner).recognize_page(TIFF_BYTES, TIFF, 2)

    assert (page.text_source, page.text.strip()) == (OCR, "frame two")
    assert runner.programs() == ["convert", "tesseract"]
    convert_argv = runner.calls[0][0]
    assert convert_argv[0] == "convert"
    assert convert_argv[1].endswith(".tiff[1]")  # zero-based frame index


def test_a_tiff_frame_that_cannot_be_extracted_fails_closed():
    runner = ScriptedRunner({"convert": lambda argv: ok(b"", returncode=1)})

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner).recognize_page(TIFF_BYTES, TIFF, 9)

    assert excinfo.value.code == PERMANENT_ERROR


# --- warnings, environment, logging ------------------------------------------


def test_a_page_with_no_recognized_text_succeeds_with_the_no_text_warning():
    runner = pdf_ocr_runner(text=b"\n \n")

    page = CommandOcrEngine(runner).recognize_page(PDF_BYTES, PDF, 1)

    assert page.text_source == OCR
    assert page.text.strip() == ""
    assert page.warnings == (NO_TEXT,)
    # Execution succeeded; no image-quality signal was invented.
    assert "possible_low_quality" not in page.warnings


def test_the_tessdata_prefix_is_passed_as_child_environment():
    runner = ScriptedRunner({"tesseract": lambda argv: ok(b"x\n")})

    CommandOcrEngine(runner, tessdata_prefix="/opt/tessdata").recognize_page(
        b"\xff\xd8\xff", JPEG, 1
    )

    assert runner.calls[0][2] == {"TESSDATA_PREFIX": "/opt/tessdata"}


def test_a_configured_binary_path_is_used_verbatim():
    runner = ScriptedRunner({"tesseract": lambda argv: ok(b"x\n")})

    CommandOcrEngine(runner, tesseract="/opt/tesseract/bin/tesseract").recognize_page(
        b"\xff\xd8\xff", JPEG, 1
    )

    assert runner.calls[0][0][0] == "/opt/tesseract/bin/tesseract"


def test_a_failure_logs_only_allowlisted_fields(caplog):
    secret = "ลับสุดยอด CONFIDENTIAL-TOKEN"
    runner = ScriptedRunner(
        {"tesseract": lambda argv: ok(secret.encode(), secret.encode(), returncode=1)}
    )
    engine = CommandOcrEngine(runner, tesseract="/private/path/tesseract")

    with caplog.at_level(logging.ERROR, logger="app.ocr_engine"):
        with pytest.raises(EnginePageError):
            engine.recognize_page(b"\xff\xd8\xff", JPEG, 1)

    assert "error_code=page_engine_error" in caplog.text
    assert "page_number=1" in caplog.text
    assert secret not in caplog.text
    assert "/private/path" not in caplog.text
    assert "tesseract" not in caplog.text


# --- page accounting and order through the worker (S1/S3) --------------------


class FrozenClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


def build_tracer(engine):
    store = InMemoryOcrStore(InMemoryObjectStore())
    clock = FrozenClock(datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC))
    dispatcher = ControllableDispatcher(
        store=store, engine=engine, clock=clock, acknowledge=True
    )
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            dispatcher=dispatcher,
            clock=clock,
            verifier=FakeTokenVerifier({TOKEN: Principal(email=EMAIL, subject="sub-a")}),
            directory=MappingTeamDirectory({f"{EMAIL}|sub-a": TEAM}),
        ),
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )
    return SimpleNamespace(client=client, store=store, engine=engine, clock=clock)


def submit(tracer, *, key="engine-key-0001"):
    return tracer.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("doc.pdf", PDF_BYTES, PDF)},
    )


def run(tracer, job_id):
    return run_worker(
        store=tracer.store,
        engines=fake_engine_registry(tracer.engine),
        clock=tracer.clock,
        team_id=TEAM,
        job_id=job_id,
        owner="executions/exec-1",
    )


def test_the_worker_accounts_for_every_source_page_in_order_with_its_text_source():
    runner = ScriptedRunner(
        {
            "pdfinfo": lambda argv: ok(b"Pages: 3\n"),
            "pdftotext": lambda argv: ok(f"หน้า {argv[2]}\n".encode()),
        }
    )
    # A declared identity: the worker resolves its engine through the registry by
    # the ref the accepted job recorded (§8).
    tracer = build_tracer(CommandOcrEngine(runner, engine_ref=FAKE_ENGINE_REF))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert body["state"] == "succeeded"
    assert body["completeness"] == "complete"
    assert body["accuracy"] == "not_verified"
    assert body["page_count"] == 3
    assert [page["page_number"] for page in body["pages"]] == [1, 2, 3]
    assert [page["text_source"] for page in body["pages"]] == [EMBEDDED] * 3
    assert body["text"] == "หน้า 1\nหน้า 2\nหน้า 3"


def test_an_undecodable_container_fails_the_job_as_unreadable_input():
    runner = ScriptedRunner({"pdfinfo": lambda argv: ok(b"not a pdf\n", returncode=1)})
    tracer = build_tracer(CommandOcrEngine(runner, engine_ref=FAKE_ENGINE_REF))
    job_id = submit(tracer).json()["job_id"]

    assert run(tracer, job_id) == 0

    body = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result").json()
    assert body["state"] == "failed"
    assert body["reason"] == "input_unreadable"
    # This container's page tree is invisible to the admission byte scan (no page
    # object in the bytes) and the decoder cannot read it, so the source page
    # count is unknown: no page record is invented and the failure is carried by
    # state/reason alone (§2.6, §9.14).
    assert body["page_count"] == 0
    assert body["pages"] == []
    assert "text" not in body


# --- deadline-scoped command bounds (review: protocol finding) ---------------


def test_every_command_of_one_page_is_bounded_by_the_page_budget():
    runner = pdf_ocr_runner()

    # The page has no usable text layer, so the call issues pdftotext, pdftoppm,
    # and tesseract; each must be capped by the tighter deadline-derived budget.
    # The monotonic source is frozen, so no time passes between the commands.
    CommandOcrEngine(runner, monotonic=FakeMonotonic()).recognize_page(
        PDF_BYTES, PDF, 1, timeout_s=2.5
    )

    assert [timeout for _, timeout, _ in runner.calls] == [2.5, 2.5, 2.5]


def test_a_page_budget_never_widens_the_configured_ceiling():
    runner = pdf_ocr_runner()

    CommandOcrEngine(runner, ocr_timeout_s=90.0, monotonic=FakeMonotonic()).recognize_page(
        PDF_BYTES, PDF, 1, timeout_s=600.0
    )

    # The budget is an upper bound, so the configured strict ceiling still wins.
    assert [timeout for _, timeout, _ in runner.calls] == [30.0, 120.0, 90.0]


def test_the_page_count_decode_is_bounded_by_its_budget():
    runner = ScriptedRunner({"pdfinfo": lambda argv: ok(b"Pages: 2\n")})

    assert (
        CommandOcrEngine(runner, monotonic=FakeMonotonic()).page_count(
            PDF_BYTES, PDF, timeout_s=1.25
        )
        == 2
    )

    assert runner.calls[0][1] == 1.25


def test_each_command_of_one_page_sees_only_the_remaining_page_budget():
    # One page's allowance is spent down across its commands, not handed out afresh
    # to each: pdftotext, the render, and tesseract draw from the same shrinking
    # budget, so their timeouts are 3.0, then 2.25, then 1.5.
    clock = FakeMonotonic()
    runner = ElapsingRunner(
        clock,
        step=0.75,
        handlers={
            "pdftotext": lambda argv: ok(b"\n"),
            "pdftoppm": render,
            "tesseract": lambda argv: ok(b"ocr text\n"),
        },
    )

    CommandOcrEngine(runner, monotonic=clock).recognize_page(
        PDF_BYTES, PDF, 1, timeout_s=3.0
    )

    assert runner.programs() == ["pdftotext", "pdftoppm", "tesseract"]
    assert [timeout for _, timeout, _ in runner.calls] == [3.0, 2.25, 1.5]


def test_a_page_command_arriving_after_the_budget_is_spent_is_refused():
    # The first command consumes the whole allowance, so the render must never be
    # issued with a fresh full-length timeout: the page fails as a transient error.
    clock = FakeMonotonic()
    runner = ElapsingRunner(
        clock,
        step=2.0,
        handlers={
            "pdftotext": lambda argv: ok(b"\n"),
            "pdftoppm": render,
            "tesseract": lambda argv: ok(b"unreachable\n"),
        },
    )

    with pytest.raises(EnginePageError) as excinfo:
        CommandOcrEngine(runner, monotonic=clock).recognize_page(
            PDF_BYTES, PDF, 1, timeout_s=2.0
        )

    assert excinfo.value.code == TRANSIENT_ERROR
    assert runner.programs() == ["pdftotext"]


def test_the_provisional_engine_requires_an_explicit_dev_opt_in(monkeypatch):
    monkeypatch.delenv("OCR_ALLOW_PROVISIONAL_ENGINE", raising=False)
    settings = OcrServiceSettings(project="thaiunion-ai-platform", bucket="b")

    assert settings.allow_provisional_engine is False
    # No deployed worker may build the measured-but-unapproved Tesseract selection
    # by default: an explicit dev opt-in is required (D5, benchmark report §6).
    with pytest.raises(ProvisionalEngineNotApproved):
        build_engine(settings)


def test_the_dev_opt_in_allows_building_the_provisional_engine(monkeypatch):
    monkeypatch.setenv("OCR_ALLOW_PROVISIONAL_ENGINE", "true")
    settings = OcrServiceSettings(project="thaiunion-ai-platform", bucket="b")

    assert settings.allow_provisional_engine is True
    assert isinstance(build_engine(settings), CommandOcrEngine)


def test_the_dev_opt_in_is_documented_where_operators_look():
    repo_root = Path(__file__).resolve().parents[1]
    env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
    adapter_doc = (repo_root / "docs/ocr-v1-engine-adapter.md").read_text(encoding="utf-8")

    assert "OCR_ALLOW_PROVISIONAL_ENGINE" in env_example
    assert "OCR_ALLOW_PROVISIONAL_ENGINE" in adapter_doc

# --- configuration surface ---------------------------------------------------


def test_engine_configuration_defaults_to_the_measured_dev_selection(monkeypatch):
    # Ambient OCR_ENGINE_* overrides (a real local run) must not change a default.
    for name in (
        "OCR_ENGINE_LANG",
        "OCR_ENGINE_RASTER_DPI",
        "OCR_ENGINE_TESSERACT",
        "OCR_ENGINE_TESSDATA_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = OcrServiceSettings(project="thaiunion-ai-platform", bucket="b")

    assert settings.engine_lang == "tha+eng"
    assert settings.engine_raster_dpi == 300
    assert settings.engine_tesseract == "tesseract"
    assert settings.engine_tessdata_prefix is None


def test_engine_configuration_is_read_from_the_ocr_environment(monkeypatch):
    monkeypatch.setenv("OCR_PROJECT", "thaiunion-ai-platform")
    monkeypatch.setenv("OCR_BUCKET", "dev-ocr-service-bucket")
    monkeypatch.setenv("OCR_ENGINE_LANG", "tha")
    monkeypatch.setenv("OCR_ENGINE_TESSERACT", "/opt/tesseract")
    monkeypatch.setenv("OCR_ENGINE_TESSDATA_PREFIX", "/opt/tessdata")
    monkeypatch.setenv("OCR_ENGINE_RASTER_DPI", "200")
    # The provisional DEV engine is only buildable under the explicit opt-in.
    monkeypatch.setenv("OCR_ALLOW_PROVISIONAL_ENGINE", "true")

    settings = OcrServiceSettings()
    assert (settings.engine_lang, settings.engine_tesseract) == ("tha", "/opt/tesseract")
    assert (settings.engine_tessdata_prefix, settings.engine_raster_dpi) == (
        "/opt/tessdata",
        200,
    )

    engine = build_engine(settings)
    assert isinstance(engine, CommandOcrEngine)

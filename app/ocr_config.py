"""Environment configuration for the deployed OCR v1 roles (contract §8).

One settings object, read explicitly from `OCR_*` variables; no ambient default
beyond the values here. Deployment is blocked (docs/ocr-v1-dispatch.md), so these
describe the shape a deployment will use - none of them is a verified cloud fact.
"""

from datetime import timedelta
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ocr_admission import (
    DEFAULT_CAPACITY_CEILING,
    MAX_IMAGE_PIXELS,
    MAX_PAGES,
    MAX_UPLOAD_BYTES,
    AdmissionLimits,
)
from app.ocr_pinning import WorkerPin
from app.ocr_tracer import parse_team_key


class OcrServiceSettings(BaseSettings):
    """`OCR_*` environment settings for the API, worker, and reconcile roles."""

    model_config = SettingsConfigDict(env_prefix="OCR_", env_file=".env", extra="ignore")

    project: str
    bucket: str
    # The bucket may live in another approved project than the service; unset
    # means the bucket is in the service project.
    bucket_project: str = ""
    region: str = "asia-southeast1"

    # The operator's pin for newly accepted jobs (§8, D18/D43): the worker image by
    # immutable digest, the versioned job name for that revision, and everything
    # the launch sends. Written once into `job.json` and frozen for the job and
    # its retries, so changing any of this changes *new* jobs only. The image and
    # digest are supplied by the deployment, never chosen by a caller.
    worker_job_name: str = ""
    worker_container_name: str = "worker"
    worker_revision_digest: str = ""
    worker_args: tuple[str, ...] = ()
    worker_env: tuple[tuple[str, str], ...] = ()
    task_timeout_s: int = 1800
    task_max_retries: int = 0
    request_timeout_s: float = 60.0
    # The cleanup job's own schedule period, asserted against `task_timeout_s` at
    # startup (D32). The Cloud Scheduler trigger itself is deployment config.
    cleanup_schedule_period_s: int = 900

    # API role: the ID-token audience and the verified-principal -> team allowlist.
    api_audience: str = ""
    # Keyed by canonical `email|subject`, so a binding carries *both* verified
    # claims (§7.3); `_team_bindings_are_strict` rejects anything else at startup.
    teams: dict[str, str] = Field(default_factory=dict)

    # Admission and input bounds (issue #10; contract §2.3, §6.4, §10/D38/D40).
    # DEV-safe defaults, not a capacity or throughput claim: the byte ceiling is the
    # reviewed 20 MiB provisional value, the page limit is the longest document
    # actually measured, and the global ceiling is the [B3]-gated placeholder.
    # `docs/ocr-v1-admission.md` states what each number rests on.
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_pages: int = MAX_PAGES
    max_image_pixels: int = MAX_IMAGE_PIXELS
    capacity_ceiling: int = DEFAULT_CAPACITY_CEILING

    # Worker role: the one job this execution was launched for, from the dispatch
    # override (contract §6.1). A worker runs exactly the job it was given.
    job_team_id: str = ""
    job_id: str = ""

    # Engine role (issue #7). The provisional DEV selection is Tesseract with the
    # distro `tha`+`eng` models (`docs/ocr-v1-benchmark-report.md` §6); production
    # selection stays blocked. The binary names resolve on `PATH`, and
    # `tessdata_prefix` is only needed when the models do not sit in Tesseract's
    # compile-time directory.
    engine_lang: str = "tha+eng"
    engine_raster_dpi: int = 300
    engine_tesseract: str = "tesseract"
    engine_tessdata_prefix: str | None = None
    # `engine_ref` names the engine, models, and parameters that produce the
    # output bytes (§8). Leave it unset and it is derived from the settings above;
    # set it when the engine has an identity of its own (a model id or version).
    engine_ref: str = ""

    # The DEV engine is the measured-but-unapproved Tesseract selection, so a
    # deployed worker builds it only under an explicit operator opt-in. Default
    # false: an approval cannot be inherited by deploying.
    allow_provisional_engine: bool = False

    def resolved_engine_ref(self) -> str:
        """The engine identity for new jobs (§8, D19).

        Derived from the settings that change output bytes when no explicit
        identity is configured, so an operator who changes the language or the
        raster resolution cannot silently keep pinning checkpoints to the old
        configuration. Only what the adapter itself reads is in the ref.
        """
        if self.engine_ref:
            return self.engine_ref
        binary = Path(self.engine_tesseract).name
        return f"tesseract:{binary}:{self.engine_lang}:{self.engine_raster_dpi}dpi"

    def pin(self) -> WorkerPin:
        """The pin this deployment writes into each newly accepted job (§8)."""
        return WorkerPin.build(
            engine_ref=self.resolved_engine_ref(),
            image=self.worker_revision_digest,
            job_name=self.worker_job_name,
            container_name=self.worker_container_name,
            args=self.worker_args,
            env=self.worker_env,
            task_timeout_s=self.task_timeout_s,
            task_max_retries=self.task_max_retries,
        )

    def admission_limits(self) -> AdmissionLimits:
        """This deployment's input and capacity bounds.

        An admission entry self-expires at `deadline_at + per_attempt_timeout`
        (§6.4), so that value follows the deployment's own task timeout.
        """
        return AdmissionLimits(
            max_upload_bytes=self.max_upload_bytes,
            max_pages=self.max_pages,
            max_image_pixels=self.max_image_pixels,
            capacity_ceiling=self.capacity_ceiling,
            per_attempt_timeout=timedelta(seconds=self.task_timeout_s),
        )

    @model_validator(mode="after")
    def _bucket_project_defaults_to_the_service_project(self) -> "OcrServiceSettings":
        if not self.bucket_project:
            self.bucket_project = self.project
        return self

    @model_validator(mode="after")
    def _team_bindings_are_strict(self) -> "OcrServiceSettings":
        """Every allowlist key must carry a verified `email|subject` pair (§7.3).

        A malformed key would either authorize an email without its subject — the
        forgery a reused address would defeat — or silently authorize nobody, so
        it is a startup failure rather than a warning.
        """
        for key, team_id in self.teams.items():
            email, _subject = parse_team_key(key)
            if "@" not in email or team_id.strip() != team_id or not team_id:
                raise ValueError(
                    "a team binding needs an email address and a non-empty team id"
                )
        return self

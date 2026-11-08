"""Environment configuration for the deployed OCR v1 roles (contract §8).

One settings object, read explicitly from `OCR_*` variables; no ambient default
beyond the values here. Deployment is blocked (docs/ocr-v1-dispatch.md), so these
describe the shape a deployment will use - none of them is a verified cloud fact.
"""

from datetime import timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ocr_admission import (
    DEFAULT_CAPACITY_CEILING,
    MAX_IMAGE_PIXELS,
    MAX_PAGES,
    MAX_UPLOAD_BYTES,
    AdmissionLimits,
)


class OcrServiceSettings(BaseSettings):
    """`OCR_*` environment settings for the API, worker, and reconcile roles."""

    model_config = SettingsConfigDict(env_prefix="OCR_", env_file=".env", extra="ignore")

    project: str
    bucket: str
    region: str = "asia-southeast1"

    # The pinned worker identity written into every job (§8). The image revision
    # is supplied by the deployment, not chosen here.
    worker_job_name: str = ""
    worker_container_name: str = "worker"
    worker_revision_digest: str = ""
    engine_ref: str = "fake-ocr-engine:v1"
    task_timeout_s: int = 1800
    request_timeout_s: float = 60.0
    # The cleanup job's own schedule period, asserted against `task_timeout_s` at
    # startup (D32). The Cloud Scheduler trigger itself is deployment config.
    cleanup_schedule_period_s: int = 900

    # API role: the ID-token audience and the verified-principal -> team allowlist.
    api_audience: str = ""
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

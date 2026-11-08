"""Environment configuration for the deployed OCR v1 roles (contract §8).

One settings object, read explicitly from `OCR_*` variables; no ambient default
beyond the values here. Deployment is blocked (docs/ocr-v1-dispatch.md), so these
describe the shape a deployment will use - none of them is a verified cloud fact.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OcrServiceSettings(BaseSettings):
    """`OCR_*` environment settings for the API, worker, and reconcile roles."""

    model_config = SettingsConfigDict(env_prefix="OCR_", env_file=".env", extra="ignore")

    project: str
    bucket: str
    # The bucket may live in another approved project than the service; unset
    # means the bucket is in the service project.
    bucket_project: str = ""
    region: str = "asia-southeast1"

    # The pinned worker identity written into every job (§8). The image revision
    # is supplied by the deployment, not chosen here.
    worker_job_name: str = ""
    worker_container_name: str = "worker"
    worker_revision_digest: str = ""
    engine_ref: str = "fake-ocr-engine:v1"
    task_timeout_s: int = 1800
    request_timeout_s: float = 60.0

    # API role: the ID-token audience and the verified-principal -> team allowlist.
    api_audience: str = ""
    teams: dict[str, str] = Field(default_factory=dict)

    # Worker role: the one job this execution was launched for, from the dispatch
    # override (contract §6.1). A worker runs exactly the job it was given.
    job_team_id: str = ""
    job_id: str = ""

    @model_validator(mode="after")
    def _bucket_project_defaults_to_the_service_project(self) -> "OcrServiceSettings":
        if not self.bucket_project:
            self.bucket_project = self.project
        return self

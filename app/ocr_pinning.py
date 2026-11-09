"""The per-job engine and worker pin (contract §8, D18/D19/D43).

One accepted job freezes its engine identity, its worker image, and everything
the launch sends, in its own `job.json`; every later retry, redispatch, and page
checkpoint is judged against that record rather than against whatever the
deployment is currently configured with. This module is the pin as a value
object: the operator's settings produce one for new jobs, the job record carries
it, and the launch and the worker validate it again before using it.

Two properties are load-bearing and are enforced here, not assumed by the
callers: the worker image is normalized to an **immutable digest** (a tag is
mutable, so a tagged image fails closed, D18), and the Cloud Run job name is
**version-qualified by that digest** (one job per revision, so a shared job name
that could be re-pointed under an accepted job is refused, §8).

`engine_ref` is operator-only: it names the engine, model, and the engine
configuration that affects output bytes, and it is never returned to a caller
(D19). Nothing here imports a cloud client or reads the environment, so the rules
are exercised without either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DIGEST_ALGORITHM = "sha256"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# `dev-ocr-service-worker-<slug>`: the deployment's naming convention for the one
# Cloud Run Job that runs one worker revision (§8, `docs/ocr-v1-dispatch.md`).
WORKER_JOB_PREFIX = "dev-ocr-service-worker"
WORKER_JOB_SLUG_LENGTH = 12


class PinError(ValueError):
    """A pin that cannot be used as configured.

    Fail closed: every caller maps it onto the contract's non-retryable
    `failed`/`engine_unavailable` conclusion or a non-zero worker exit, never onto
    a fallback to the current setting (§6.1 step 5, §8). The message carries a pin
    value, which is operator-only metadata and never document content (D19/D20).
    """

    code = "engine_unavailable"


def normalize_image_digest(reference: str) -> str:
    """The immutable `sha256:<64 hex>` digest in `reference`; anything else fails.

    Accepts a bare digest or a full `repo@sha256:...` reference, because a
    deployment often has the latter to hand. A tag - even a plausible one such as
    `worker:v1` - is refused: only the digest identifies the exact bytes, and the
    pin is compared against the template at every launch (D18).
    """
    candidate = reference.strip().rpartition("@")[2]
    if not DIGEST_PATTERN.fullmatch(candidate):
        raise PinError(f"not an immutable image digest: {reference.strip()!r}")
    return candidate


def worker_job_slug(digest: str) -> str:
    """The digest's short slug in a versioned Cloud Run job name."""
    return digest.split(":", 1)[1][:WORKER_JOB_SLUG_LENGTH]


def versioned_worker_job_name(digest: str, prefix: str = WORKER_JOB_PREFIX) -> str:
    """The versioned job name for one image digest: one job per revision (§8)."""
    return f"{prefix}-{worker_job_slug(digest)}"


def _is_version_qualified(job_name: str, digest: str) -> bool:
    return job_name.endswith(f"-{worker_job_slug(digest)}") and len(job_name) > (
        WORKER_JOB_SLUG_LENGTH + 1
    )


def _as_args(args) -> tuple[str, ...]:
    arguments = tuple(args)
    if not all(isinstance(argument, str) for argument in arguments):
        raise PinError("a pinned worker argument is not a string")
    return arguments


def _as_env(env) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for pair in env:
        candidate = tuple(pair)
        if len(candidate) != 2 or not all(isinstance(value, str) for value in candidate):
            raise PinError("a pinned worker env entry is not a name/value pair")
        pairs.append((candidate[0], candidate[1]))
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class WorkerPin:
    """One job's frozen engine and worker revision (contract §8, D43).

    Construct it with `build()` (operator settings, normalized) or `from_job()`
    (an accepted record, validated again). Both paths run the same checks; a
    directly constructed pin that is not already normalized is refused by
    `__post_init__`, so no caller can smuggle a tag or a shared job name in.
    """

    engine_ref: str
    worker_revision_digest: str
    worker_job_name: str
    worker_container_name: str
    worker_args: tuple[str, ...] = ()
    worker_env: tuple[tuple[str, str], ...] = ()
    task_timeout_s: int = 0
    task_max_retries: int = 0

    def __post_init__(self) -> None:
        if not self.engine_ref or self.engine_ref != self.engine_ref.strip():
            raise PinError("the engine_ref pin is empty or padded")
        try:
            normalized = normalize_image_digest(self.worker_revision_digest)
        except PinError as exc:
            raise PinError(f"the pinned worker image is unusable: {exc}") from None
        if normalized != self.worker_revision_digest:
            raise PinError("the pinned worker image is not normalized to its digest")
        if not _is_version_qualified(self.worker_job_name, self.worker_revision_digest):
            raise PinError(f"worker job name is not version-qualified: {self.worker_job_name!r}")
        if not self.worker_container_name or self.worker_container_name != (
            self.worker_container_name.strip()
        ):
            raise PinError("the pinned worker container name is empty or padded")
        if self.task_timeout_s <= 0:
            raise PinError("the pinned task timeout must be positive")
        if self.task_max_retries < 0:
            raise PinError("the pinned task retry bound must not be negative")
        if not all(isinstance(argument, str) for argument in self.worker_args):
            raise PinError("a pinned worker argument is not a string")
        if not all(
            isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(v, str) for v in pair)
            for pair in self.worker_env
        ):
            raise PinError("a pinned worker env entry is not a name/value pair")

    @classmethod
    def build(
        cls,
        *,
        engine_ref: str,
        image: str,
        job_name: str = "",
        container_name: str = "worker",
        args: tuple[str, ...] = (),
        env: tuple[tuple[str, str], ...] = (),
        task_timeout_s: int,
        task_max_retries: int = 0,
    ) -> WorkerPin:
        """The operator's pin for new jobs: normalize the image, derive the job name.

        An empty `job_name` becomes the versioned name for this digest. A
        configured name is kept as given and then validated, so an operator's own
        prefix is allowed while a name that could serve more than one revision is
        refused.
        """
        digest = normalize_image_digest(image)
        return cls(
            engine_ref=engine_ref,
            worker_revision_digest=digest,
            worker_job_name=job_name or versioned_worker_job_name(digest),
            worker_container_name=container_name,
            worker_args=_as_args(args),
            worker_env=_as_env(env),
            task_timeout_s=task_timeout_s,
            task_max_retries=task_max_retries,
        )

    def job_fields(self) -> dict[str, Any]:
        """The pin fields as a new `job.json` records them (§5.7, D43).

        One method names what is frozen into a job, so the record and the pin
        cannot drift apart field by field.
        """
        return {
            "engine_ref": self.engine_ref,
            "worker_revision_digest": self.worker_revision_digest,
            "worker_job_name": self.worker_job_name,
            "worker_container_name": self.worker_container_name,
            "worker_args": self.worker_args,
            "worker_env": self.worker_env,
            "task_timeout_s": self.task_timeout_s,
            "task_max_retries": self.task_max_retries,
        }

    @classmethod
    def from_job(cls, job) -> WorkerPin:
        """The pin a job was accepted under, re-validated before it is used.

        Nothing is re-derived from the current settings here: an accepted record
        is the authority for its own retries, and a record whose pin no longer
        validates is refused rather than repaired (§6.1 step 2).
        """
        return cls(
            engine_ref=job.engine_ref,
            worker_revision_digest=job.worker_revision_digest,
            worker_job_name=job.worker_job_name,
            worker_container_name=job.worker_container_name,
            worker_args=_as_args(job.worker_args),
            worker_env=_as_env(job.worker_env),
            task_timeout_s=job.task_timeout_s,
            task_max_retries=job.task_max_retries,
        )

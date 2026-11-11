"""Build-independent container liveness and shutdown smoke check."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import urllib.request
import uuid

FAKE_DIGEST = "sha256:" + "0" * 64


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=capture)


def _runtime(requested: str) -> str:
    if requested != "auto":
        if shutil.which(requested) is None:
            raise SystemExit(f"OCI runtime not found: {requested}")
        return requested
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    raise SystemExit("neither podman nor docker is available")


def _wait_for_health(url: str, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = json.load(response)
                if response.status == 200 and body == {"status": "ok"}:
                    return body
        except Exception as exc:  # the container may still be starting
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"health check did not pass: {type(last_error).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", choices=("auto", "podman", "docker"), default="auto")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    runtime = _runtime(args.runtime)
    name = f"ocr-container-smoke-{uuid.uuid4().hex}"
    created = False
    client_url = f"http://127.0.0.1:{args.port}"
    environment = {
        "OCR_PROJECT": "ci",
        "OCR_BUCKET": "dev-ocr-service-ci",
        "OCR_BUCKET_PROJECT": "ci",
        "OCR_REGION": "asia-southeast1",
        "OCR_WORKER_JOB_NAME": "dev-ocr-service-worker-000000000000",
        "OCR_WORKER_CONTAINER_NAME": "worker",
        "OCR_WORKER_REVISION_DIGEST": FAKE_DIGEST,
        "OCR_API_AUDIENCE": client_url,
        "OCR_TEAMS": '{"ci@example.invalid|123":"team-ci"}',
    }
    command = [
        runtime,
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        f"127.0.0.1:{args.port}:8080",
    ]
    for key, value in environment.items():
        command.extend(("--env", f"{key}={value}"))
    command.append(args.image)

    try:
        _run(command)
        created = True
        _wait_for_health(f"{client_url}/health", args.timeout)
        started = time.monotonic()
        _run([runtime, "stop", "--time", "10", name])
        shutdown_s = time.monotonic() - started
        inspected = _run(
            [runtime, "inspect", "--format", "{{.State.ExitCode}}", name], capture=True
        )
        exit_code = int(inspected.stdout.strip())
        if exit_code != 0 or shutdown_s > 10:
            raise RuntimeError(f"unclean shutdown: exit={exit_code}, seconds={shutdown_s:.3f}")
        print(
            json.dumps(
                {
                    "health": 200,
                    "runtime": runtime,
                    "shutdown_seconds": round(shutdown_s, 3),
                    "exit_code": exit_code,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if created:
            subprocess.run(
                [runtime, "rm", "--force", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    raise SystemExit(main())

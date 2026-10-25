#!/usr/bin/env python3
import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.verification import summarize_api_response, verify_api_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--manifest", default="examples/manifest.json")
    parser.add_argument(
        "--gcloud-auth",
        action="store_true",
        help="Obtain a short-lived identity token from the active gcloud account.",
    )
    parser.add_argument(
        "--include-responses",
        action="store_true",
        help=(
            "Persist full OCR/extraction responses. Off by default because "
            "documents may be sensitive."
        ),
    )
    parser.add_argument("--output", default="verification/e2e-report.json")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    headers = {}
    if args.gcloud_auth:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            text=True,
        ).strip()
        headers["Authorization"] = f"Bearer {token}"
    report: dict[str, Any] = {"base_url": args.base_url, "results": []}

    with httpx.Client(timeout=300) as client:
        for example in manifest["examples"]:
            file_path = manifest_path.parent / example["file"]
            with file_path.open("rb") as file_handle:
                response = client.post(
                    f"{args.base_url.rstrip('/')}/v1/documents:process",
                    headers=headers,
                    files={"file": (file_path.name, file_handle, "application/pdf")},
                )
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw_response": response.text}

            errors = []
            if response.status_code != 200:
                errors.append(f"HTTP status {response.status_code}")
            elif isinstance(payload, dict):
                errors.extend(verify_api_result(example["expected_type"], payload))
            else:
                errors.append("API response is not a JSON object")

            report["results"].append(
                {
                    "file": example["file"],
                    "source_url": example["source_url"],
                    "expected_type": example["expected_type"],
                    "status_code": response.status_code,
                    "passed": not errors,
                    "errors": errors,
                    "response": (
                        payload
                        if args.include_responses
                        else summarize_api_response(payload)
                    ),
                }
            )

    report["passed"] = all(result["passed"] for result in report["results"])
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

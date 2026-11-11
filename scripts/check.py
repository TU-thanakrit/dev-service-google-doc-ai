"""Run the repository's deterministic local quality gate and report one summary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def parse_junit(path: Path) -> dict[str, int]:
    """Return authoritative aggregate counts from pytest's JUnit XML."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))

    def total(name: str) -> int:
        aggregate = root.get(name)
        if aggregate is not None:
            return int(aggregate)
        return sum(int(suite.get(name, "0")) for suite in suites)

    return {
        "tests": total("tests"),
        "failures": total("failures"),
        "errors": total("errors"),
        "skipped": total("skipped"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junitxml", type=Path)
    args = parser.parse_args(argv)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.junitxml is None:
        temporary = tempfile.TemporaryDirectory(prefix="ocr-check-")
        junit = Path(temporary.name) / "pytest.xml"
    else:
        junit = args.junitxml
        junit.parent.mkdir(parents=True, exist_ok=True)

    try:
        _run(["uv", "lock", "--check"])
        _run(["ruff", "check", "."])
        _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-q",
                f"--junitxml={junit}",
            ]
        )
        summary = parse_junit(junit)
        summary["passed"] = (
            summary["tests"]
            - summary["failures"]
            - summary["errors"]
            - summary["skipped"]
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

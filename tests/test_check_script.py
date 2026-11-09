from pathlib import Path

from scripts.check import parse_junit


def test_parse_junit_uses_the_aggregate_suite_counts(tmp_path: Path):
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites tests="5" failures="1" errors="1" skipped="1">'
        '<testsuite name="first" tests="2" failures="1" errors="0" skipped="0" />'
        '<testsuite name="second" tests="3" failures="0" errors="1" skipped="1" />'
        "</testsuites>",
        encoding="utf-8",
    )

    assert parse_junit(report) == {
        "tests": 5,
        "failures": 1,
        "errors": 1,
        "skipped": 1,
    }


def test_parse_junit_sums_child_suites_when_the_root_has_no_counts(tmp_path: Path):
    report = tmp_path / "pytest.xml"
    report.write_text(
        "<testsuites>"
        '<testsuite name="first" tests="2" failures="0" errors="0" skipped="1" />'
        '<testsuite name="second" tests="3" failures="1" errors="0" skipped="0" />'
        "</testsuites>",
        encoding="utf-8",
    )

    assert parse_junit(report) == {
        "tests": 5,
        "failures": 1,
        "errors": 0,
        "skipped": 1,
    }

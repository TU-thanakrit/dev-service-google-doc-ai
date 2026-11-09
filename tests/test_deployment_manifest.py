import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
MANIFEST = REPO_ROOT / "deploy" / "ocr-v1" / "sandbox.toml"


def _manifest() -> dict:
    with MANIFEST.open("rb") as stream:
        return tomllib.load(stream)


def test_deployment_manifest_keeps_the_live_pin_and_identity_facts_together():
    manifest = _manifest()
    digest = manifest["build"]["digest"]

    assert manifest["status"] == "deployed"
    assert manifest["scope"]["project"] == "thaiunion-shared-sandbox"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source"]["commit"])
    assert manifest["build"]["image"].endswith(f"@{digest}")
    assert manifest["worker"]["name"].endswith(digest.removeprefix("sha256:")[:12])
    assert manifest["worker"]["pin_verified"] is True
    assert manifest["service"]["audience"] == manifest["service"]["client_url"]
    assert manifest["service"]["private"] is True
    assert manifest["security"]["production_ready"] is False
    assert manifest["security"]["runtime_identity_least_privilege"] is False
    assert manifest["identity"]["runtime"] != manifest["identity"]["n8n_caller_email"]
    assert manifest["acceptance"]["deployed_source_total_tests"] == 423
    assert manifest["acceptance"]["deployed_source_tests_passed"] == 415
    assert manifest["acceptance"]["paid_ocr_requests"] == 0


def test_deployment_evidence_matches_the_manifest_identity_and_pin():
    manifest = _manifest()
    evidence = (REPO_ROOT / manifest["evidence"]["report"]).read_text(encoding="utf-8")

    expected = (
        manifest["source"]["commit"],
        manifest["source"]["build_context_sha256"],
        manifest["build"]["cloud_build_id"],
        manifest["build"]["image"],
        manifest["service"]["ready_revision"],
        manifest["worker"]["name"],
    )
    for value in expected:
        assert value in evidence


def test_agent_and_human_entrypoints_point_to_the_manifest_instead_of_stale_facts():
    pointed_files = (
        "AGENTS.md",
        "README.md",
        ".env.example",
        "docs/ocr-service-spec.md",
        "docs/ocr-v1-contracts.md",
        "docs/ocr-v1-cutover.md",
        "docs/ocr-v1-dispatch.md",
        "docs/ocr-v1-durability.md",
        "docs/ocr-v1-engine-adapter.md",
        "docs/ocr-v1-lifecycle.md",
        "docs/ocr-v1-pinning.md",
        "docs/ocr-v1-technical-design.md",
        "verification/summary.md",
    )
    contents = {
        path: (REPO_ROOT / path).read_text(encoding="utf-8") for path in pointed_files
    }

    for path, content in contents.items():
        assert "deploy/ocr-v1/sandbox.toml" in content, path
    assert "Status: not deployed" not in contents["README.md"]
    assert "Development target: project `thaiunion-ai-platform`" not in contents["AGENTS.md"]

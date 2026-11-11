"""The exported n8n workflow artifact: shape, and that it carries no secret.

The artifact is a definition a human imports, not something this repository runs,
so the guard is on its content: it must not be active, it must describe the
upload -> poll -> result flow against the OCR routes, and it must contain no
credential, token, or key — an exported workflow is evidence, and evidence in Git
never carries secrets.

The import itself is unverified: no n8n instance was contacted, and node type
versions may need re-selecting on import (`docs/ocr-v1-n8n-workflow.md`).
"""

import json
from pathlib import Path

import pytest

ARTIFACT = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "n8n"
    / "ocr-upload-poll-result.workflow.json"
)
SECRET_MARKERS = (
    "private_key",
    "client_email",
    "refresh_token",
    "BEGIN PRIVATE KEY",
    "AIza",
    "eyJhbGciOi",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def http_nodes(workflow) -> list[dict]:
    return [node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]


def test_the_artifact_is_inactive_and_has_a_manual_start(workflow):
    assert workflow["active"] is False
    assert workflow["settings"]["executionOrder"] == "v1"
    assert [node["type"] for node in workflow["nodes"] if node["type"].endswith("manualTrigger")]


def test_it_submits_polls_and_retrieves_a_result(workflow):
    types = {node["type"] for node in workflow["nodes"]}
    by_name = {node["name"]: node["parameters"] for node in http_nodes(workflow)}

    assert by_name["Submit document"]["method"] == "POST"
    assert by_name["Submit document"]["url"].endswith("'/v1/ocr/jobs' }}")
    status_url = by_name["Get status"]["url"]
    assert status_url.endswith("$('Submit document').item.json.job_id }}")
    assert by_name["Get result"]["url"].endswith("+ '/result' }}")
    assert "n8n-nodes-base.wait" in types
    assert "n8n-nodes-base.if" in types
    assert "n8n-nodes-base.readWriteFile" in types


def test_the_token_comes_from_the_metadata_server(workflow):
    token_nodes = [
        node for node in http_nodes(workflow) if "computeMetadata" in node["parameters"]["url"]
    ]

    assert len(token_nodes) == 1, "one node must acquire the identity token"
    headers = token_nodes[0]["parameters"]["headerParameters"]["parameters"]
    assert {"name": "Metadata-Flavor", "value": "Google"} in headers


def test_no_credential_token_or_key_is_embedded(workflow):
    rendered = json.dumps(workflow, ensure_ascii=False)

    assert not any(marker in rendered for marker in SECRET_MARKERS)
    for node in workflow["nodes"]:
        assert "credentials" not in node, node["name"]
        if node["type"] != "n8n-nodes-base.httpRequest":
            continue
        for header in node["parameters"].get("headerParameters", {}).get("parameters", []):
            if header["name"].lower() == "authorization":
                # An expression reference, never a literal bearer token.
                assert header["value"].startswith("=Bearer {{"), header
                assert "Get identity token" in header["value"]


def test_every_node_is_reachable_from_the_trigger(workflow):
    graph = {name: [] for name in [node["name"] for node in workflow["nodes"]]}
    for source, outputs in workflow["connections"].items():
        assert source in graph, source
        for branch in outputs["main"]:
            for connection in branch or []:
                assert connection["node"] in graph, connection
                graph[source].append(connection["node"])

    seen, pending = set(), ["Manual Trigger"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(graph[name])

    assert seen == set(graph)

"""Fake-mode execution command for the local OCR lifecycle tracer (issue #3).

Dev/test only. Runs the whole submit -> manual dispatch -> poll -> result
lifecycle in one process against the injected fakes, prints each observable
step, and fails if any cloud client module is imported on the way.

    uv run python -m scripts.ocr_tracer_demo

Nothing here contacts GCP, so no quota is spent and no deployment is implied.
"""

import sys

from fastapi.testclient import TestClient

from app.ocr_tracer import EnginePage, SystemClock, create_ocr_tracer_app
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    InMemoryOcrStore,
    StaticIdentity,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
IDEMPOTENCY_KEY = "demo-key-0001"

# Modules that would mean a real cloud client had been constructed or imported.
CLOUD_MODULES = ("app.factory", "app.integrations")


def main() -> int:
    store = InMemoryOcrStore()
    engine = FakeOcrEngine(
        [EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="Receipt page two")]
    )
    clock = SystemClock()
    dispatcher = ControllableDispatcher(store=store, engine=engine, clock=clock)
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            engine=engine,
            dispatcher=dispatcher,
            clock=clock,
            identity=StaticIdentity("demo-team"),
        ),
        raise_server_exceptions=False,
    )

    health = client.get("/health")
    print(f"GET  /health                        -> {health.status_code} {health.json()}")
    assert health.status_code == 200

    submitted = client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
        files={"file": ("receipt.pdf", PDF_BYTES, "application/pdf")},
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    print(f"POST /v1/ocr/jobs                   -> {submitted.status_code} job_id={job_id}")
    print(f"     state={submitted.json()['state']} dispatch={submitted.json()['dispatch']}")
    assert engine.calls == [], "recognition must not run during submission"

    pending = client.get(f"/v1/ocr/jobs/{job_id}/result")
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {pending.status_code} (not final yet)")
    assert pending.status_code == 202

    print(f"     manual dispatch of {len(dispatcher.pending)} job(s)")
    dispatcher.run_pending()

    status = client.get(f"/v1/ocr/jobs/{job_id}")
    print(f"GET  /v1/ocr/jobs/{{id}}             -> {status.status_code} {status.json()['state']}")
    print(f"     progress={status.json()['progress']}")

    result = client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    body = result.json()
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {result.status_code}")
    print(f"     page_count={body['page_count']} accuracy={body['accuracy']}")
    for page in body["pages"]:
        print(
            f"     page {page['page_number']}: [{page['text_source']}] {page['text']!r}"
        )
    print(f"     combined text={body['text']!r}")

    duplicate = client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
        files={"file": ("receipt.pdf", PDF_BYTES, "application/pdf")},
    )
    print(f"POST /v1/ocr/jobs (same key+bytes)  -> {duplicate.status_code} same job_id")
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == job_id

    imported = sorted(
        name
        for name in sys.modules
        if name.split(".")[0] == "google" or name in CLOUD_MODULES
    )
    assert not imported, f"cloud client modules were imported: {imported}"
    print("fake-mode lifecycle ok: no cloud clients imported, no GCP calls made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

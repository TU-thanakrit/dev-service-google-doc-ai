"""Fake-mode execution command for the local OCR lifecycle tracer (issue #3).

Dev/test only. Runs the whole submit -> manual dispatch -> poll -> result
lifecycle in one process against the injected fakes, shows the deadline path,
prints each observable step, and fails if any cloud client module is imported on
the way.

    uv run python -m scripts.ocr_tracer_demo

Nothing here contacts GCP, so no quota is spent and no deployment is implied.
"""

import sys
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.ocr_tracer import JOB_LIFETIME, EnginePage, create_ocr_tracer_app
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    InMemoryOcrStore,
    StaticIdentity,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
IDEMPOTENCY_KEY = "demo-key-0001"
LATE_IDEMPOTENCY_KEY = "demo-key-late-01"
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)

# Modules that would mean a real cloud client had been constructed or imported.
CLOUD_MODULES = ("app.factory", "app.integrations")


class DemoClock:
    """Mutable clock: the demo has to reach a job's deadline without waiting."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def submit(client: TestClient, *, key: str):
    return client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("receipt.pdf", PDF_BYTES, "application/pdf")},
    )


def main() -> int:
    store = InMemoryOcrStore()
    engine = FakeOcrEngine(
        [EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="Receipt page two")]
    )
    clock = DemoClock(START)
    dispatcher = ControllableDispatcher(store=store, engine=engine, clock=clock)
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            dispatcher=dispatcher,
            clock=clock,
            identity=StaticIdentity("demo-team"),
        ),
        raise_server_exceptions=False,
    )

    health = client.get("/health")
    print(f"GET  /health                        -> {health.status_code} {health.json()}")
    assert health.status_code == 200

    submitted = submit(client, key=IDEMPOTENCY_KEY)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    print(f"POST /v1/ocr/jobs                   -> {submitted.status_code} job_id={job_id}")
    print(
        f"     state={submitted.json()['state']} dispatch={submitted.json()['dispatch']}"
        f" worker_revision={dispatcher.worker_revision_digest}"
    )
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

    duplicate = submit(client, key=IDEMPOTENCY_KEY)
    print(f"POST /v1/ocr/jobs (same key+bytes)  -> {duplicate.status_code} same job_id")
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == job_id
    assert dispatcher.pending == [], "a same-key retry must not enqueue a second dispatch"

    # Deadline path: a worker that reaches a job at or after its deadline must not
    # process it, and the job finalizes deadline_exceeded (D42).
    late = submit(client, key=LATE_IDEMPOTENCY_KEY)
    late_job_id = late.json()["job_id"]
    assert late.status_code == 202
    clock.advance(JOB_LIFETIME)
    dispatcher.run_pending()
    late_result = client.get(f"/v1/ocr/jobs/{late_job_id}/result")
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {late_result.status_code} (at deadline)")
    print(
        f"     state={late_result.json()['state']} reason={late_result.json()['reason']}"
    )
    assert late_result.json()["state"] == "failed"
    assert late_result.json()["reason"] == "deadline_exceeded"
    assert len(engine.calls) == 1, "a worker at the deadline must not process"

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

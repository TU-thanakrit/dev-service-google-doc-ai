"""Fake-mode execution command for the local OCR lifecycle tracer (issue #3).

Dev/test only. Runs the submit -> manual dispatch -> poll -> result lifecycle in
one process against the injected fakes, then the recovery paths added by issue
#8: a partial page result, a restart that resumes from page checkpoints, and the
deadline path. Prints each observable step and fails if any cloud client module
is imported on the way.

    uv run python -m scripts.ocr_tracer_demo

Nothing here contacts GCP, so no quota is spent and no deployment is implied.
"""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.ocr_reconcile import reconcile
from app.ocr_tracer import (
    JOB_LIFETIME,
    EnginePage,
    EnginePageError,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
    run_job,
)
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    FakeTokenVerifier,
    InMemoryObjectStore,
    InMemoryOcrStore,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
DEMO_TOKEN = "demo-verified-id-token"
DEMO_EMAIL = "demo-caller@thaiunion-ai-platform.iam.gserviceaccount.com"
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
# A crash stands for anything that kills a worker mid-document.
CRASH = RuntimeError("gs://private-bucket/engine-crashed")

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


def build(*, engine, objects=None):
    """One tracer app over the injected fakes, with the handles the demo needs."""
    objects = objects if objects is not None else InMemoryObjectStore()
    store = InMemoryOcrStore(objects)
    clock = DemoClock(START)
    dispatcher = ControllableDispatcher(store=store, engine=engine, clock=clock)
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            dispatcher=dispatcher,
            clock=clock,
            verifier=FakeTokenVerifier(
                {DEMO_TOKEN: Principal(email=DEMO_EMAIL, subject="demo-subject")}
            ),
            directory=MappingTeamDirectory({DEMO_EMAIL: "demo-team"}),
        ),
        headers={"Authorization": f"Bearer {DEMO_TOKEN}"},
        raise_server_exceptions=False,
    )
    return SimpleNamespace(
        store=store, engine=engine, clock=clock, dispatcher=dispatcher, client=client
    )


def submit(client: TestClient, *, key: str):
    return client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("receipt.pdf", PDF_BYTES, "application/pdf")},
    )


def main() -> int:
    tracer = build(
        engine=FakeOcrEngine(
            [EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="Receipt page two")]
        )
    )

    health = tracer.client.get("/health")
    print(f"GET  /health                        -> {health.status_code} {health.json()}")
    assert health.status_code == 200

    submitted = submit(tracer.client, key="demo-key-0001")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    print(f"POST /v1/ocr/jobs                   -> {submitted.status_code} job_id={job_id}")
    print(
        f"     state={submitted.json()['state']} dispatch={submitted.json()['dispatch']}"
        f" worker_revision={tracer.dispatcher.worker_revision_digest}"
    )
    assert tracer.engine.calls == [], "recognition must not run during submission"

    pending = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {pending.status_code} (not final yet)")
    assert pending.status_code == 202

    print(f"     manual dispatch of {len(tracer.dispatcher.pending)} job(s)")
    tracer.dispatcher.run_pending()

    status = tracer.client.get(f"/v1/ocr/jobs/{job_id}")
    print(f"GET  /v1/ocr/jobs/{{id}}             -> {status.status_code} {status.json()['state']}")
    print(f"     progress={status.json()['progress']}")

    result = tracer.client.get(f"/v1/ocr/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    body = result.json()
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {result.status_code}")
    print(f"     page_count={body['page_count']} accuracy={body['accuracy']}")
    for page in body["pages"]:
        print(f"     page {page['page_number']}: [{page['text_source']}] {page['text']!r}")
    print(f"     combined text={body['text']!r}")

    duplicate = submit(tracer.client, key="demo-key-0001")
    print(f"POST /v1/ocr/jobs (same key+bytes)  -> {duplicate.status_code} same job_id")
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == job_id
    assert tracer.dispatcher.pending == [], "a same-key retry must not enqueue a dispatch"

    # Partial page result: page 2 fails, page 1 survives and is reported honestly.
    partial = build(
        engine=FakeOcrEngine(
            [EnginePage(text="page one"), EnginePageError("malformed_document")]
        )
    )
    partial_id = submit(partial.client, key="demo-key-part-1").json()["job_id"]
    partial.dispatcher.run_pending()
    partial_body = partial.client.get(f"/v1/ocr/jobs/{partial_id}/result").json()
    print(
        f"GET  /v1/ocr/jobs/{{id}}/result      -> {partial_body['state']}"
        f" completeness={partial_body['completeness']} reason={partial_body['reason']}"
    )
    print(f"     failed page 2 error_code={partial_body['pages'][1]['error_code']}")
    assert (partial_body["state"], partial_body["completeness"]) == (
        "partially_succeeded",
        "partial",
    )
    assert partial_body["pages"][1]["status"] == "failed"
    assert partial_body["pages"][1]["error_code"] == "malformed_document"
    assert partial_body["text"] == "page one"

    # Recovery: a crash mid-document is resumed from the pages already checkpointed.
    objects = InMemoryObjectStore()
    crashed = build(
        engine=FakeOcrEngine(
            [EnginePage(text="first"), EnginePage(text="second"), CRASH]
        ),
        objects=objects,
    )
    crashed_id = submit(crashed.client, key="demo-key-cras1").json()["job_id"]
    try:
        crashed.dispatcher.run_pending()
    except RuntimeError:
        pass
    print(f"POST /v1/ocr/jobs (worker crashes)  -> job_id={crashed_id} (not final)")

    restarted = build(
        engine=FakeOcrEngine(
            [EnginePage(text="DIFFERENT"), EnginePage(text="DIFFERENT"), EnginePage(text="third")]
        ),
        objects=objects,
    )
    run_job(
        store=restarted.store,
        engine=restarted.engine,
        clock=restarted.clock,
        team_id="demo-team",
        job_id=crashed_id,
    )
    resumed = restarted.client.get(f"/v1/ocr/jobs/{crashed_id}/result").json()
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {resumed['state']} text={resumed['text']!r}")
    assert resumed["text"] == "first\nsecond\nthird"
    assert restarted.engine.calls == [3], "only the page without a checkpoint is recomputed"

    # Deadline path: a worker that reaches a job at or after its deadline writes
    # nothing; reconciliation finalizes it deadline_exceeded (§5.5, §6.2, D42).
    late = submit(tracer.client, key="demo-key-late-01")
    late_job_id = late.json()["job_id"]
    assert late.status_code == 202
    processed = len(tracer.engine.calls)
    tracer.clock.advance(JOB_LIFETIME)
    tracer.dispatcher.run_pending()
    denied = tracer.client.get(f"/v1/ocr/jobs/{late_job_id}/result")
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {denied.status_code} (at deadline)")
    assert denied.status_code == 410
    assert len(tracer.engine.calls) == processed, "a worker at the deadline must not process"
    reconcile(store=tracer.store, dispatcher=tracer.dispatcher, clock=tracer.clock)
    late_result = tracer.client.get(f"/v1/ocr/jobs/{late_job_id}/result")
    print(
        f"     after reconcile state={late_result.json()['state']}"
        f" reason={late_result.json()['reason']}"
    )
    assert late_result.json()["state"] == "failed"
    assert late_result.json()["reason"] == "deadline_exceeded"

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

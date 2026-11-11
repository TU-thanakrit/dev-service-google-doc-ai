"""Fake-mode execution command for the local OCR lifecycle tracer (issue #3).

Dev/test only. Runs the submit -> manual dispatch -> poll -> result lifecycle in
one process against the injected fakes, then the recovery paths added by issue
#8: a partial page result, a restart that resumes from page checkpoints, and the
deadline path, and the deadline/expiry/cleanup paths added by issue #9: prompt
input deletion, access cut off at expiry while records remain, an idempotent
physical cleanup pass, and the overdue signal. Prints each observable step and
fails if any cloud client module is imported on the way.

    uv run python -m scripts.ocr_tracer_demo

Nothing here contacts GCP, so no quota is spent and no deployment is implied.
"""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.ocr_cleanup import CLEANUP_TARGET, cleanup, cleanup_guard
from app.ocr_reconcile import reconcile
from app.ocr_tracer import (
    JOB_LIFETIME,
    RETENTION,
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
# Structurally readable but encrypted: refused before a job exists (issue #10).
ENCRYPTED_PDF = (
    b"%PDF-1.7\n1 0 obj\n<< /Type /Page >>\nendobj\ntrailer\n<< /Encrypt 9 0 R >>\n%%EOF\n"
)
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
            directory=MappingTeamDirectory({f"{DEMO_EMAIL}|demo-subject": "demo-team"}),
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
        f" worker_revision={tracer.dispatcher.pin.worker_revision_digest}"
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

    # A full success deletes its input at once, and access ends at expiry whether
    # or not a cleanup pass has run.
    retained = build(engine=FakeOcrEngine([EnginePage(text="kept")]))
    retained_id = submit(retained.client, key="demo-key-ret-1").json()["job_id"]
    retained.dispatcher.run_pending()
    input_name = f"inputs/demo-team/{retained_id}"
    input_gone = retained.store.objects.read(input_name) is None
    print(f"POST /v1/ocr/jobs (full success)    -> input deleted: {input_gone}")
    assert retained.store.objects.read(input_name) is None
    retained.clock.advance(RETENTION)
    expired = retained.client.get(f"/v1/ocr/jobs/{retained_id}/result")
    print(f"GET  /v1/ocr/jobs/{{id}}/result      -> {expired.status_code} (at expires_at)")
    assert expired.status_code == 410
    job_record = f"jobs/demo-team/{retained_id}/job.json"
    records_kept = retained.store.objects.read(job_record) is not None
    print(f"     records still present: job.json={records_kept}")

    guard = cleanup_guard(retained.store.get_job("demo-team", retained_id))
    retained.clock.advance(guard - retained.clock.now())
    actions = cleanup(store=retained.store, clock=retained.clock)
    print(f"     cleanup pass at the guard         -> {actions}")
    assert actions == [f"cleaned:demo-team/{retained_id}"]
    gone = retained.client.get(f"/v1/ocr/jobs/{retained_id}")
    print(f"GET  /v1/ocr/jobs/{{id}}             -> {gone.status_code} (after cleanup)")
    assert gone.status_code == 404
    assert cleanup(store=retained.store, clock=retained.clock) == []

    # A pass that finds a job past its cleanup target reports it overdue and then
    # still deletes it, so the job log carries the missed target as a signal.
    overdue = build(engine=FakeOcrEngine([EnginePage(text="late")]))
    overdue_id = submit(overdue.client, key="demo-key-ovd-1").json()["job_id"]
    overdue.dispatcher.run_pending()
    overdue.clock.advance(RETENTION + CLEANUP_TARGET)
    actions = cleanup(store=overdue.store, clock=overdue.clock)
    print(f"     cleanup pass past the target      -> {actions}")
    assert actions == [f"overdue:demo-team/{overdue_id}", f"cleaned:demo-team/{overdue_id}"]

    # Input boundary: an encrypted document is rejected on its bytes, before a job
    # or a dispatch exists (issue #10, contract §2.3).
    bounded = build(engine=FakeOcrEngine([EnginePage(text="bounded")]))
    encrypted = bounded.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": "demo-key-enc-1"},
        files={"file": ("locked.pdf", ENCRYPTED_PDF, "application/pdf")},
    )
    print(
        f"POST /v1/ocr/jobs (encrypted pdf)   -> {encrypted.status_code}"
        f" {encrypted.json()['error']['code']}"
    )
    assert encrypted.status_code == 422

    # Admission bound: the global set holds one job, and a second submission is
    # refused with a retryable 429 instead of being queued without a bound (§6.4/D40).
    admitted = submit(bounded.client, key="demo-key-cap-1")
    refused = submit(bounded.client, key="demo-key-cap-2")
    print(
        f"POST /v1/ocr/jobs (capacity full)   -> {refused.status_code}"
        f" {refused.json()['error']['code']}"
        f" Retry-After={refused.headers['Retry-After']}"
    )
    assert admitted.status_code == 202
    assert refused.status_code == 429

    bounded.dispatcher.run_pending()
    released = submit(bounded.client, key="demo-key-cap-3")
    print(f"POST /v1/ocr/jobs (slot released)   -> {released.status_code}")
    assert released.status_code == 202

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

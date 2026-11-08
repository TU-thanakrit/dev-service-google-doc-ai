"""Fake-mode dispatch -> worker -> reconcile demo for the OCR v1 slice (issue #6).

Runs the whole asynchronous lifecycle in one process against the injected fakes:
submit (launch acknowledgment), the worker entrypoint publishing the result, and
the reconciliation recovery paths (lost acknowledgment, non-retryable launch
failure). It fails if any cloud client module gets imported on the way.

    uv run python -m scripts.ocr_worker_demo

Nothing here contacts GCP, so no quota is spent and no deployment is implied.
"""

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.ocr_reconcile import reconcile
from app.ocr_store import DISPATCH_SPACING
from app.ocr_tracer import (
    EnginePage,
    MappingTeamDirectory,
    Principal,
    create_ocr_tracer_app,
)
from app.ocr_tracer_fakes import (
    ControllableDispatcher,
    FakeOcrEngine,
    FakeTokenVerifier,
    InMemoryObjectStore,
    InMemoryOcrStore,
    fake_engine_registry,
)
from app.ocr_worker import run_worker

TEAM = "demo-team"
EMAIL = "demo-caller@example.com"
TOKEN = "demo-verified-id-token"
START = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PAGES = (EnginePage(text="ใบเสร็จรับเงิน"), EnginePage(text="Receipt page two"))

# Modules that would mean a real cloud client had been constructed or imported.
CLOUD_MODULES = ("app.factory", "app.integrations", "app.ocr_auth", "app.ocr_api")


class DemoClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta) -> None:
        self._now += delta


def build(*, acknowledge=True, launch_error=None):
    store = InMemoryOcrStore(InMemoryObjectStore())
    engine = FakeOcrEngine(PAGES)
    clock = DemoClock(START)
    dispatcher = ControllableDispatcher(
        store=store, engine=engine, clock=clock, acknowledge=acknowledge, launch_error=launch_error
    )
    client = TestClient(
        create_ocr_tracer_app(
            store=store,
            dispatcher=dispatcher,
            clock=clock,
            verifier=FakeTokenVerifier({TOKEN: Principal(email=EMAIL, subject="demo-sub")}),
            directory=MappingTeamDirectory({EMAIL: TEAM}),
        ),
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )
    return SimpleNamespace(
        client=client, store=store, engine=engine, clock=clock, dispatcher=dispatcher
    )


def submit(demo, *, key):
    return demo.client.post(
        "/v1/ocr/jobs",
        headers={"Idempotency-Key": key},
        files={"file": ("receipt.pdf", PDF_BYTES, "application/pdf")},
    )


def work(demo, job_id):
    return run_worker(
        store=demo.store,
        engines=fake_engine_registry(demo.engine),
        clock=demo.clock,
        team_id=TEAM,
        job_id=job_id,
        owner="executions/demo",
    )


def main() -> int:
    demo = build()
    submitted = submit(demo, key="demo-key-0001")
    job_id = submitted.json()["job_id"]
    print(f"POST /v1/ocr/jobs                     -> {submitted.status_code} job_id={job_id}")
    print(f"     launch {submitted.json()['dispatch']} state={submitted.json()['state']}")

    # Acknowledged is not complete: the API still reports the job as unfinished.
    pending = demo.client.get(f"/v1/ocr/jobs/{job_id}/result")
    print(f"GET  result (after ack, before worker)-> {pending.status_code} (not final)")
    assert pending.status_code == 202

    print(f"     worker claims the lease and runs {len(demo.dispatcher.pending)} job(s)")
    assert work(demo, job_id) == 0
    result = demo.client.get(f"/v1/ocr/jobs/{job_id}/result")
    print(f"GET  result (after the worker)        -> {result.status_code}")
    print(f"     text={result.json()['text']!r}")
    assert result.status_code == 200

    # Lost acknowledgment: the job is accepted, unfinished, and re-dispatched by
    # reconciliation once the attempt spacing elapses.
    lost = build(acknowledge=False)
    lost_id = submit(lost, key="demo-key-lost1").json()["job_id"]
    lost.dispatcher.pending.clear()
    print(f"POST /v1/ocr/jobs (lost ack)          -> accepted, acknowledged=False job_id={lost_id}")
    first_pass = reconcile(store=lost.store, dispatcher=lost.dispatcher, clock=lost.clock)
    print(f"     reconcile before the spacing      -> {first_pass}")
    assert first_pass == []
    lost.clock.advance(DISPATCH_SPACING)
    redispatch = reconcile(store=lost.store, dispatcher=lost.dispatcher, clock=lost.clock)
    print(f"     reconcile after the spacing       -> {redispatch}")
    assert redispatch == [f"redispatched:{TEAM}/{lost_id}"]
    assert work(lost, lost_id) == 0
    assert lost.client.get(f"/v1/ocr/jobs/{lost_id}/result").status_code == 200

    # Non-retryable launch failure: two attempts, then finalize `engine_unavailable`.
    broken = build(launch_error="engine_unavailable")
    broken_id = submit(broken, key="demo-key-brok1").json()["job_id"]
    broken.dispatcher.pending.clear()
    broken.clock.advance(DISPATCH_SPACING)
    reconcile(store=broken.store, dispatcher=broken.dispatcher, clock=broken.clock)
    finalized = reconcile(store=broken.store, dispatcher=broken.dispatcher, clock=broken.clock)
    print(f"POST /v1/ocr/jobs (launch denied)     -> {finalized}")
    assert finalized == [f"finalized:engine_unavailable:{TEAM}/{broken_id}"]
    failed = broken.client.get(f"/v1/ocr/jobs/{broken_id}/result").json()
    print(f"GET  result (launch denied)           -> {failed['state']} reason={failed['reason']}")
    assert (failed["state"], failed["reason"]) == ("failed", "engine_unavailable")

    imported = sorted(
        name for name in sys.modules if name.split(".")[0] == "google" or name in CLOUD_MODULES
    )
    assert not imported, f"cloud client modules were imported: {imported}"
    print("worker demo ok: dispatch, worker, and reconcile ran with no cloud clients")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

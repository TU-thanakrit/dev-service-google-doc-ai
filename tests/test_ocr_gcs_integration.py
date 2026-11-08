"""Blocked real-GCS integration test for the durable store (issue #4).

BLOCKED: it needs an administrator-provisioned bucket named `dev-ocr-service-*`
in project `thaiunion-ai-platform` plus object-level permissions for the caller.
The current account cannot create a bucket, and reusing an unrelated existing
bucket is not allowed. Until that exists, this test does not run and makes no
live call; nothing here is cloud evidence.

To run it once provisioned (a paid/approved integration scope, see the spec):

    OCR_GCS_INTEGRATION_PROJECT=thaiunion-ai-platform \
    OCR_GCS_INTEGRATION_BUCKET=dev-ocr-service-<provisioned-suffix> \
    uv run pytest tests/test_ocr_gcs_integration.py -q

It asserts only the conditional-write behaviour the in-memory fake cannot prove:
create-if-absent, generation-matched replace/delete, and read-after-write
consistency on one bucket. It never touches a bucket outside the prefix and
deletes every object it created.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest

from app.ocr_store import Claim, DurableOcrStore, PreconditionFailed

BUCKET = os.environ.get("OCR_GCS_INTEGRATION_BUCKET", "")
PROJECT = os.environ.get("OCR_GCS_INTEGRATION_PROJECT", "")

pytestmark = pytest.mark.skipif(
    not BUCKET,
    reason=(
        "blocked: needs an admin-provisioned dev-ocr-service-* bucket and object "
        "permissions; the current account cannot create one (issue #4)"
    ),
)


def test_conditional_protocol_holds_against_real_gcs():
    assert BUCKET.startswith("dev-ocr-service-"), (
        "refusing to run against a bucket outside the approved dev-ocr-service-* prefix"
    )
    assert PROJECT == "thaiunion-ai-platform", (
        "refusing to run outside the approved integration project"
    )

    from app.gcs_objects import create_gcs_ocr_store

    store: DurableOcrStore = create_gcs_ocr_store(BUCKET, project=PROJECT)
    team_id = f"itr-{uuid.uuid4().hex}"
    key_hash = uuid.uuid4().hex
    claim = Claim(
        job_id=str(uuid.uuid4()),
        content_hash="0" * 64,
        byte_length=4,
        claimed_at=datetime.now(UTC),
    )
    claim_name = f"idem/{team_id}/{key_hash}"

    try:
        generation = store.create_claim(team_id, key_hash, claim)
        found = store.claim(team_id, key_hash)
        assert found is not None
        stored, read_generation = found
        assert stored == claim
        assert read_generation == generation

        with pytest.raises(PreconditionFailed):
            store.create_claim(team_id, key_hash, claim)

        generation = store.replace_claim(team_id, key_hash, claim, generation, claim.job_id)
        with pytest.raises(PreconditionFailed):
            # The generation read before the successful replace is now stale.
            store.replace_claim(team_id, key_hash, claim, generation - 1, claim.job_id)
    finally:
        found = store.objects.read(claim_name)
        if found is not None:
            store.objects.delete(claim_name, found[1])

    assert store.claim(team_id, key_hash) is None

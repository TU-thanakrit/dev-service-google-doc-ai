"""Real-GCS integration test for the durable store (issue #4).

Runs only against the approved cross-project bucket: the durable bucket lives in
`thaiunion-shared-sandbox` while the service stays in `thaiunion-ai-platform`, so
the store is opened with the bucket's own project
(`OCR_GCS_INTEGRATION_BUCKET_PROJECT`).

It writes generated metadata records only - an idempotency `Claim`, never document
content - deletes every object it created, and reads its unique prefix back to
prove nothing is left behind.

To run it (a paid/approved integration scope, see the spec; the caller's ADC
identity must have object access to the bucket):

    OCR_GCS_INTEGRATION_BUCKET=dev-ocr-service-data-asse1-1022222817897 \
    OCR_GCS_INTEGRATION_BUCKET_PROJECT=thaiunion-shared-sandbox \
    uv run pytest tests/test_ocr_gcs_integration.py -q

It asserts only the conditional-write behaviour the in-memory fake cannot prove:
create-if-absent, generation-matched replace/delete, and read-after-write
consistency on one bucket. It refuses to run against a bucket outside the
`dev-ocr-service-` prefix and leaves bucket IAM and PAP untouched.
"""

import json
import os
import uuid
from datetime import UTC, datetime

import pytest

from app.ocr_store import Claim, DurableOcrStore, PreconditionFailed

BUCKET = os.environ.get("OCR_GCS_INTEGRATION_BUCKET", "")
BUCKET_PROJECT = os.environ.get("OCR_GCS_INTEGRATION_BUCKET_PROJECT", "")

APPROVED_BUCKET_PROJECT = "thaiunion-shared-sandbox"
BUCKET_PREFIX = "dev-ocr-service-"

pytestmark = pytest.mark.skipif(
    not BUCKET,
    reason=(
        "needs the approved dev-ocr-service-* bucket, its project, and object access "
        "for the caller (issue #4)"
    ),
)


def test_conditional_protocol_holds_against_real_gcs():
    assert BUCKET.startswith(BUCKET_PREFIX), (
        f"refusing to run against a bucket outside the approved {BUCKET_PREFIX}* prefix"
    )
    assert BUCKET_PROJECT == APPROVED_BUCKET_PROJECT, (
        "refusing to run outside the approved cross-project bucket project"
    )

    from app.gcs_objects import create_gcs_ocr_store

    store: DurableOcrStore = create_gcs_ocr_store(BUCKET, project=BUCKET_PROJECT)
    team_id = f"itr-{uuid.uuid4().hex}"
    key_hash = uuid.uuid4().hex
    claim = Claim(
        job_id=str(uuid.uuid4()),
        content_hash="0" * 64,
        byte_length=4,
        claimed_at=datetime.now(UTC),
    )
    prefix = f"idem/{team_id}/"
    claim_name = f"{prefix}{key_hash}"

    try:
        generation = store.create_claim(team_id, key_hash, claim)
        assert store.objects.list_names(prefix) == [claim_name]

        # The object is the generated metadata record, not a stored document.
        created = store.objects.read(claim_name)
        assert created is not None
        payload, read_generation = created
        assert read_generation == generation
        assert set(json.loads(payload)) == {
            "schema_version",
            "job_id",
            "content_hash",
            "byte_length",
            "claimed_at",
        }

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

    # Cleanup proof: the test's unique prefix is empty again.
    assert store.objects.list_names(prefix) == []
    assert store.claim(team_id, key_hash) is None

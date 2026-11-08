"""Unit tests for the GCS object adapter's translation layer.

The fake bucket here mimics the `google-cloud-storage` call shapes and raises the
real `google.api_core` exceptions, so the mapping between SDK errors and this
application's `PreconditionFailed`/`StorageUnavailable` is exercised without a
network call. It proves nothing about real GCS behaviour, latency, or bucket
configuration; that evidence is the blocked integration test in
`tests/test_ocr_gcs_integration.py`.
"""

from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.api_core.exceptions import PreconditionFailed as ApiPreconditionFailed

from app.gcs_objects import GcsObjects, create_gcs_ocr_store
from app.ocr_store import Claim, PreconditionFailed, StorageUnavailable


class FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self._name = name
        self.generation = None
        self._payload = None

    def reload(self):
        self._bucket.fail_if_denied(self._name)
        entry = self._bucket.objects.get(self._name)
        if entry is None:
            raise NotFound(self._name)
        self._payload, self.generation = entry

    def download_as_bytes(self):
        return self._payload

    def upload_from_string(self, payload, if_generation_match=None):
        self._bucket.fail_if_denied(self._name)
        entry = self._bucket.objects.get(self._name)
        if if_generation_match == 0:
            if entry is not None:
                raise ApiPreconditionFailed(self._name)
            created = (payload, self._bucket.next_generation())
            self._bucket.objects[self._name] = created
            self.generation = created[1]
            return None
        if entry is None or entry[1] != if_generation_match:
            raise ApiPreconditionFailed(self._name)
        replaced = (payload, self._bucket.next_generation())
        self._bucket.objects[self._name] = replaced
        self.generation = replaced[1]
        return None

    def delete(self, if_generation_match=None):
        self._bucket.fail_if_denied(self._name)
        entry = self._bucket.objects.get(self._name)
        if entry is None:
            raise NotFound(self._name)
        if entry[1] != if_generation_match:
            raise ApiPreconditionFailed(self._name)
        del self._bucket.objects[self._name]


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self._generation = 0
        self.denied = False

    def blob(self, name):
        return FakeBlob(self, name)

    def next_generation(self):
        self._generation += 1
        return self._generation

    def fail_if_denied(self, name):
        if self.denied:
            raise Forbidden(name)


class FakeClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        return self._bucket


@pytest.fixture()
def bucket():
    return FakeBucket()


@pytest.fixture()
def objects(bucket):
    return GcsObjects(bucket)


def test_create_is_create_once_and_read_returns_the_generation(objects):
    generation = objects.create("idem/team-a/key", b"first")

    assert objects.read("idem/team-a/key") == (b"first", generation)
    with pytest.raises(PreconditionFailed):
        objects.create("idem/team-a/key", b"second")


def test_replace_and_delete_require_the_generation_read(objects):
    generation = objects.create("record", b"one")

    with pytest.raises(PreconditionFailed):
        objects.replace("record", b"two", generation + 1)
    with pytest.raises(PreconditionFailed):
        objects.replace("record", b"two", 0)

    replaced = objects.replace("record", b"two", generation)
    assert objects.read("record") == (b"two", replaced)

    with pytest.raises(PreconditionFailed):
        objects.delete("record", generation)
    objects.delete("record", replaced)
    assert objects.read("record") is None


def test_missing_object_reads_as_absent(objects):
    assert objects.read("never/written") is None


def test_backend_denial_is_a_storage_failure_not_a_precondition(objects, bucket):
    bucket.denied = True

    with pytest.raises(StorageUnavailable):
        objects.read("anything")
    with pytest.raises(StorageUnavailable):
        objects.create("anything", b"payload")
    with pytest.raises(StorageUnavailable):
        objects.delete("anything", 1)


def test_the_durable_store_runs_over_the_gcs_adapter(bucket):
    store = create_gcs_ocr_store("dev-ocr-service-test", client=FakeClient(bucket))
    claimed_at = datetime(2026, 11, 8, 4, 31, 7, tzinfo=UTC)
    claim = Claim(
        job_id="job-0001",
        content_hash="hash-a",
        byte_length=4,
        claimed_at=claimed_at,
    )

    generation = store.create_claim("team-a", "key", claim)
    found = store.claim("team-a", "key")
    assert found is not None
    stored, read_generation = found
    assert stored == claim
    assert read_generation == generation

    with pytest.raises(PreconditionFailed):
        store.create_claim("team-a", "key", claim)

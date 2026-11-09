"""GCS implementation of the conditional object seam (`app.ocr_store.ObjectStore`).

A thin translation over one bucket: it exposes single-object atomicity and
generation preconditions, nothing more. The application protocol (claims,
create-once records, the `final.json` arbiter) lives in `app.ocr_store`; this
module adds no cross-object behaviour and no retries.

Reads cost two calls because `download_as_bytes()` does not return the object's
generation, so the payload and the generation it is paired with can come from
adjacent generations. Every mutation this store makes is a conditional write at a
generation it re-reads immediately before, so a torn pairing loses the race
instead of overwriting state (docs/ocr-v1-contracts.md §5.2).
"""

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.api_core.exceptions import PreconditionFailed as ApiPreconditionFailed
from google.cloud import storage

from app.ocr_store import DurableOcrStore, PreconditionFailed, StorageUnavailable


class GcsObjects:
    """One bucket, addressed by object name, with generation preconditions."""

    def __init__(self, bucket: storage.Bucket) -> None:
        self._bucket = bucket

    def read(self, name: str) -> tuple[bytes, int] | None:
        """The object's bytes bound to the generation they were read at.

        `download_as_bytes()` alone returns the *current* bytes, which can be a
        newer generation than the metadata read; a decision made from that pair
        (a claim's `job_id`, `final.json`'s result name) would be made on
        mismatched state. The download is therefore conditioned on the generation
        just read: deleted in between reads as absent, replaced in between is a
        storage failure rather than a torn pair (contract §5.2).
        """
        blob = self._bucket.blob(name)
        try:
            blob.reload()
        except NotFound:
            return None
        except GoogleAPIError as exc:
            raise StorageUnavailable(name) from exc
        generation = blob.generation
        if generation is None:
            # No generation from the metadata read: the pair cannot be bound.
            raise StorageUnavailable(name)
        generation = int(generation)
        try:
            payload = blob.download_as_bytes(if_generation_match=generation)
        except NotFound:
            # Deleted between the metadata read and the download: absent, not an
            # error, so every caller's "no object" path handles it.
            return None
        except ApiPreconditionFailed as exc:
            raise StorageUnavailable(name) from exc
        except GoogleAPIError as exc:
            raise StorageUnavailable(name) from exc
        return payload, generation

    def exists(self, name: str) -> bool:
        """Whether the object is present, from metadata only (no download)."""
        try:
            return self._bucket.blob(name).exists()
        except GoogleAPIError as exc:
            raise StorageUnavailable(name) from exc

    def list_names(self, prefix: str) -> list[str]:
        try:
            return [blob.name for blob in self._bucket.list_blobs(prefix=prefix)]
        except GoogleAPIError as exc:
            raise StorageUnavailable(prefix) from exc

    def create(self, name: str, payload: bytes) -> int:
        return self._upload(name, payload, if_generation_match=0)

    def replace(self, name: str, payload: bytes, generation: int) -> int:
        if generation <= 0:
            # The 0 precondition is for creating object data only (contract §5.1).
            raise PreconditionFailed(name)
        return self._upload(name, payload, if_generation_match=generation)

    def delete(self, name: str, generation: int) -> None:
        if generation <= 0:
            raise PreconditionFailed(name)
        try:
            self._bucket.blob(name).delete(if_generation_match=generation)
        except (NotFound, ApiPreconditionFailed) as exc:
            raise PreconditionFailed(name) from exc
        except GoogleAPIError as exc:
            raise StorageUnavailable(name) from exc

    def _upload(self, name: str, payload: bytes, *, if_generation_match: int) -> int:
        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(payload, if_generation_match=if_generation_match)
            blob.reload()
        except ApiPreconditionFailed as exc:
            raise PreconditionFailed(name) from exc
        except GoogleAPIError as exc:
            raise StorageUnavailable(name) from exc
        return int(blob.generation)


def create_gcs_ocr_store(
    bucket_name: str, *, project: str | None = None, client: storage.Client | None = None
) -> DurableOcrStore:
    """Build the durable store over one GCS bucket.

    The client is injectable so callers can reuse an existing authenticated
    client. Pass account, project, and region explicitly at the call site; this
    function reads no ambient defaults beyond what `storage.Client` itself does.
    """
    storage_client = client or storage.Client(project=project)
    return DurableOcrStore(GcsObjects(storage_client.bucket(bucket_name)))

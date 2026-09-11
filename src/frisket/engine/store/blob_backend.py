"""Hash-verified canonical byte stores for project blobs."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

from frisket.project_identity import ProjectStorageKey


class BlobStoreError(RuntimeError):
    """A canonical blob operation failed."""


class BlobNotFoundError(BlobStoreError, LookupError):
    """The requested canonical object does not exist."""


class BlobIntegrityError(BlobStoreError):
    """Canonical bytes do not match their content digest."""


class ProjectBlobStore(Protocol):
    """The deliberately small canonical project-byte port.

    Contract both implementers must honor (load-bearing for callers such as
    an external edition's blob-cutover job, which rely on it instead of re-hashing):

    - ``put`` and ``put_path`` return the sha256 hex digest of the stored bytes.
      ``put_path`` must stream rather than materializing the whole file in RAM
      and must reject a supplied ``expected_digest`` when the source differs.
    - ``materialize`` re-verifies the bytes are content-addressed before
      yielding them and raises ``BlobIntegrityError`` on any mismatch, so a
      bare ``with store.materialize(digest): pass`` is a genuine integrity
      check, never a no-op. It also validates the digest string's shape
      (``validate_blob_digest``) before constructing any path/key.
    """

    def put(self, data: bytes) -> str: ...

    def put_path(
        self,
        path: str | Path,
        *,
        expected_digest: str | None = None,
    ) -> str: ...

    def materialize(self, digest: str) -> AbstractContextManager[Path]: ...


def validate_blob_digest(digest: str) -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("blob digest must be 64 lowercase hexadecimal characters")
    return digest


def _sha256_path(path: Path) -> str:
    digest, _size = sha256_blob_path(path)
    return digest


def sha256_blob_path(path: str | Path) -> tuple[str, int]:
    """Hash a file incrementally and return its digest and observed byte count."""

    source_path = Path(path)
    digest = hashlib.sha256()
    size = 0
    with source_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform supports directory fsync."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class FilesystemProjectBlobStore:
    """Portable bundle-local canonical blob storage."""

    def __init__(self, *, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_bundle(cls, bundle_path: str | Path) -> "FilesystemProjectBlobStore":
        return cls(root=Path(bundle_path) / "blobs")

    def _path_for_digest(self, digest: str) -> Path:
        clean = validate_blob_digest(digest)
        return self.root / clean[:2] / clean

    def put(self, data: bytes) -> str:
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        target = self._path_for_digest(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha256_path(target) != digest:
                raise BlobIntegrityError(f"canonical blob {digest} is corrupt")
            return digest
        fd, raw_temp = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as sink:
                sink.write(payload)
                sink.flush()
                os.fsync(sink.fileno())
            if _sha256_path(temp_path) != digest:
                raise BlobIntegrityError(f"staged blob {digest} failed verification")
            os.replace(temp_path, target)
            if _sha256_path(target) != digest:
                raise BlobIntegrityError(f"canonical blob {digest} failed verification")
        finally:
            temp_path.unlink(missing_ok=True)
        return digest

    def put_path(
        self,
        path: str | Path,
        *,
        expected_digest: str | None = None,
    ) -> str:
        """Stream a file into the canonical store without loading it into RAM.

        The source is copied and hashed in the same pass.  Only a fully written,
        fsync'd, hash-verified temporary file is atomically installed at the
        content-addressed destination.
        """

        source_path = Path(path)
        expected = (
            validate_blob_digest(expected_digest)
            if expected_digest is not None
            else None
        )
        fd, raw_temp = tempfile.mkstemp(prefix=".blob-path.", dir=self.root)
        temp_path = Path(raw_temp)
        digest_builder = hashlib.sha256()
        try:
            with source_path.open("rb") as source, os.fdopen(fd, "wb") as sink:
                # ``sink`` owns the descriptor from this point onward.
                fd = -1
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            digest = digest_builder.hexdigest()
            if expected is not None and digest != expected:
                raise BlobIntegrityError(
                    "blob source changed after its expected digest was computed"
                )
            if _sha256_path(temp_path) != digest:
                raise BlobIntegrityError(f"staged blob {digest} failed verification")

            target = self._path_for_digest(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _sha256_path(target) != digest:
                    raise BlobIntegrityError(f"canonical blob {digest} is corrupt")
                return digest

            os.replace(temp_path, target)
            if _sha256_path(target) != digest:
                raise BlobIntegrityError(f"canonical blob {digest} failed verification")
            _fsync_directory(target.parent)
            return digest
        finally:
            # If either context expression failed before ``sink`` took
            # ownership, the raw descriptor still needs closing.
            if fd >= 0:
                os.close(fd)
            temp_path.unlink(missing_ok=True)

    @contextmanager
    def materialize(self, digest: str) -> Iterator[Path]:
        target = self._path_for_digest(digest)
        if not target.is_file():
            raise BlobNotFoundError(f"no such blob: {digest}")
        if _sha256_path(target) != digest:
            raise BlobIntegrityError(f"canonical blob {digest} is corrupt")
        yield target


class S3ProjectBlobStore:
    """Scoped S3-compatible canonical storage without a local shared cache."""

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str,
        project_storage_key: ProjectStorageKey,
    ):
        if not bucket:
            raise ValueError("bucket is required")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.project_storage_key = project_storage_key

    def _object_key(self, digest: str) -> str:
        clean = validate_blob_digest(digest)
        return f"{self._project_object_prefix()}{clean}"

    def _project_object_prefix(self) -> str:
        parts = [
            self.prefix,
            str(self.project_storage_key.storage_org_id),
            self.project_storage_key.project_slug,
            "sha256",
        ]
        return f"{'/'.join(part for part in parts if part)}/"

    @staticmethod
    def _error_parts(exc: Exception) -> tuple[str, int | None]:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return "", None
        error = response.get("Error")
        metadata = response.get("ResponseMetadata")
        code = str(error.get("Code") or "") if isinstance(error, dict) else ""
        raw_status = (
            metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        )
        return code, raw_status if isinstance(raw_status, int) else None

    @classmethod
    def _raise_store_error(cls, exc: Exception, *, digest: str) -> None:
        code, status = cls._error_parts(exc)
        if status == 404 or code in {"404", "NoSuchKey", "NotFound", "NoSuchObject"}:
            raise BlobNotFoundError(f"no such blob: {digest}") from exc
        raise BlobStoreError(f"object store failed for blob {digest}") from exc

    def _download_to(self, digest: str, target: Path) -> None:
        key = self._object_key(digest)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response.get("Body") if isinstance(response, dict) else None
            if body is None or not hasattr(body, "read"):
                raise BlobStoreError(f"object store returned no body for blob {digest}")
            try:
                with target.open("wb") as sink:
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        sink.write(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except BlobStoreError:
            raise
        except Exception as exc:  # boto and S3-compatible clients share this envelope
            self._raise_store_error(exc, digest=digest)

    def put(self, data: bytes) -> str:
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        key = self._object_key(digest)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                Metadata={"sha256": digest},
                ChecksumSHA256=base64.b64encode(
                    hashlib.sha256(payload).digest()
                ).decode("ascii"),
            )
        except Exception as exc:
            self._raise_store_error(exc, digest=digest)
        with self.materialize(digest):
            pass
        return digest

    def put_path(
        self,
        path: str | Path,
        *,
        expected_digest: str | None = None,
    ) -> str:
        """Multipart-capable streaming upload followed by end-to-end hashing."""

        source_path = Path(path)
        expected = (
            validate_blob_digest(expected_digest)
            if expected_digest is not None
            else None
        )
        with tempfile.TemporaryDirectory(prefix="frisket-blob-upload-") as raw_dir:
            # Upload an immutable private snapshot, not the caller's live path.
            # A renderer cleaning up or replacing its scratch file mid-upload can
            # therefore never poison the canonical content-addressed key.
            snapshot = Path(raw_dir) / "payload"
            digest_builder = hashlib.sha256()
            with source_path.open("rb") as source, snapshot.open("xb") as sink:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            digest = digest_builder.hexdigest()
            if expected is not None and digest != expected:
                raise BlobIntegrityError(
                    "blob source changed after its expected digest was computed"
                )
            if _sha256_path(snapshot) != digest:
                raise BlobIntegrityError(f"staged blob {digest} failed verification")

            # Never overwrite a valid canonical object.  This also fails closed
            # when an object exists under the digest key but its bytes are corrupt.
            try:
                with self.materialize(digest):
                    pass
                return digest
            except BlobNotFoundError:
                pass

            key = self._object_key(digest)
            upload_fileobj = getattr(self.client, "upload_fileobj", None)
            if not callable(upload_fileobj):
                raise BlobStoreError(
                    f"object store cannot stream a file upload for blob {digest}"
                )
            try:
                with snapshot.open("rb") as source:
                    upload_fileobj(
                        Fileobj=source,
                        Bucket=self.bucket,
                        Key=key,
                        ExtraArgs={
                            "Metadata": {"sha256": digest},
                            "ChecksumAlgorithm": "SHA256",
                        },
                    )
            except Exception as exc:
                self._raise_store_error(exc, digest=digest)
            with self.materialize(digest):
                pass
            return digest

    def _delete_objects(self, objects: list[dict[str, str]]) -> None:
        for start in range(0, len(objects), 1_000):
            chunk = objects[start : start + 1_000]
            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        "Objects": chunk,
                        "Quiet": True,
                    },
                )
            except Exception as exc:
                raise BlobStoreError(
                    "object store failed while deleting project blobs"
                ) from exc
            errors = response.get("Errors") if isinstance(response, dict) else None
            if errors:
                raise BlobStoreError(
                    "object store failed to delete one or more project blobs"
                )

    def _delete_object_keys(self, keys: list[str]) -> None:
        self._delete_objects([{"Key": key} for key in keys])

    def _list_project_object_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        seen_keys: set[str] = set()
        continuation_token: str | None = None
        seen_tokens: set[str] = set()

        while True:
            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": 1_000,
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            try:
                response = self.client.list_objects_v2(**request)
            except Exception as exc:
                raise BlobStoreError(
                    "object store failed while listing project blobs"
                ) from exc

            if not isinstance(response, dict):
                raise BlobStoreError("object store returned an invalid project listing")
            contents = response.get("Contents", [])
            if contents is None:
                contents = []
            if not isinstance(contents, list):
                raise BlobStoreError("object store returned an invalid project listing")
            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise BlobStoreError(
                        "object store returned a key outside the project scope"
                    )
                if key not in seen_keys:
                    seen_keys.add(key)
                    keys.append(key)

            truncated = response.get("IsTruncated", False)
            if not isinstance(truncated, bool):
                raise BlobStoreError("object store returned an invalid project listing")
            if not truncated:
                return keys
            next_token = response.get("NextContinuationToken")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token in seen_tokens
            ):
                raise BlobStoreError(
                    "object store returned an invalid project continuation token"
                )
            seen_tokens.add(next_token)
            continuation_token = next_token

    @classmethod
    def _version_listing_unavailable(cls, exc: Exception) -> bool:
        if isinstance(exc, (AttributeError, NotImplementedError)):
            return True
        code, status = cls._error_parts(exc)
        return status == 501 or code in {
            "501",
            "NotImplemented",
            "UnsupportedOperation",
            "XNotImplemented",
        }

    def _list_project_object_versions(
        self,
        prefix: str,
    ) -> list[dict[str, str]] | None:
        list_versions = getattr(self.client, "list_object_versions", None)
        if not callable(list_versions):
            return None

        objects: list[dict[str, str]] = []
        seen_objects: set[tuple[str, str]] = set()
        key_marker: str | None = None
        version_id_marker: str | None = None
        seen_markers: set[tuple[str, str | None]] = set()
        listed_page = False

        while True:
            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": 1_000,
            }
            if key_marker is not None:
                request["KeyMarker"] = key_marker
            if version_id_marker is not None:
                request["VersionIdMarker"] = version_id_marker
            try:
                response = list_versions(**request)
            except Exception as exc:
                if not listed_page and self._version_listing_unavailable(exc):
                    return None
                raise BlobStoreError(
                    "object store failed while listing project blob versions"
                ) from exc

            if not isinstance(response, dict):
                raise BlobStoreError(
                    "object store returned an invalid project version listing"
                )
            listed_page = True
            for field in ("Versions", "DeleteMarkers"):
                entries = response.get(field, [])
                if entries is None:
                    entries = []
                if not isinstance(entries, list):
                    raise BlobStoreError(
                        "object store returned an invalid project version listing"
                    )
                for item in entries:
                    key = item.get("Key") if isinstance(item, dict) else None
                    version_id = (
                        item.get("VersionId") if isinstance(item, dict) else None
                    )
                    if not isinstance(key, str) or not key.startswith(prefix):
                        raise BlobStoreError(
                            "object store returned a key outside the project scope"
                        )
                    if not isinstance(version_id, str) or not version_id:
                        raise BlobStoreError(
                            "object store returned an invalid project object version"
                        )
                    identity = (key, version_id)
                    if identity not in seen_objects:
                        seen_objects.add(identity)
                        objects.append({"Key": key, "VersionId": version_id})

            truncated = response.get("IsTruncated", False)
            if not isinstance(truncated, bool):
                raise BlobStoreError(
                    "object store returned an invalid project version listing"
                )
            if not truncated:
                return objects

            next_key_marker = response.get("NextKeyMarker")
            next_version_id_marker = response.get("NextVersionIdMarker")
            if not isinstance(next_key_marker, str) or not next_key_marker:
                raise BlobStoreError(
                    "object store returned an invalid project version marker"
                )
            if next_version_id_marker is not None and (
                not isinstance(next_version_id_marker, str)
                or not next_version_id_marker
            ):
                raise BlobStoreError(
                    "object store returned an invalid project version marker"
                )
            next_markers = (next_key_marker, next_version_id_marker)
            if next_markers in seen_markers:
                raise BlobStoreError(
                    "object store returned a repeated project version marker"
                )
            seen_markers.add(next_markers)
            key_marker, version_id_marker = next_markers

    def delete_project_objects(self) -> None:
        """Delete every canonical object below this exact project scope.

        Terminal project deletion cannot rely only on the bundle's ``blobs``
        rows: an upload may become durable before its metadata transaction
        commits, and metadata GC intentionally removes references before the
        canonical byte.  The project identity already fixes a safe prefix, so
        enumerate only beneath that prefix, validate every returned key and
        version before the first mutation, then verify both current objects and
        historical versions/delete markers are empty.  S3-compatible stores
        without version-listing support retain the current-key fallback.
        """

        prefix = self._project_object_prefix()
        versions = self._list_project_object_versions(prefix)
        if versions is not None:
            self._delete_objects(versions)

        # Keep the ordinary object sweep for clients without version APIs and
        # for a current object that appeared after the version snapshot.
        self._delete_object_keys(self._list_project_object_keys(prefix))

        if self._list_project_object_keys(prefix):
            raise BlobStoreError("object store retained project blobs after deletion")

        if versions is not None:
            remaining_versions = self._list_project_object_versions(prefix)
            if remaining_versions is None:
                raise BlobStoreError(
                    "object store could not verify project version deletion"
                )
            if remaining_versions:
                raise BlobStoreError(
                    "object store retained project blob versions after deletion"
                )

    @contextmanager
    def materialize(self, digest: str) -> Iterator[Path]:
        clean = validate_blob_digest(digest)
        with tempfile.TemporaryDirectory(prefix="frisket-project-blob-") as raw_dir:
            path = Path(raw_dir) / clean
            self._download_to(clean, path)
            if _sha256_path(path) != clean:
                raise BlobIntegrityError(f"canonical blob {clean} is corrupt")
            yield path

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from helpers import stub_rapidocr_run_scope
from frisket.engine.store import Project


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class _BlobApi:
    project_blob_store: type[Any]
    filesystem_store: type[Any]
    s3_store: type[Any]
    integrity_error: type[Exception]
    not_found_error: type[Exception]
    backend_error: type[Exception]
    project_storage_key: type[Any]


def _blob_api() -> _BlobApi:
    assert find_spec("frisket.engine.store.blob_backend") is not None, (
        "missing frisket.store.blob_backend ProjectBlobStore port"
    )
    module = importlib.import_module("frisket.engine.store.blob_backend")
    identity = importlib.import_module("frisket.project_identity")
    names = {
        "project_blob_store": "ProjectBlobStore",
        "filesystem_store": "FilesystemProjectBlobStore",
        "s3_store": "S3ProjectBlobStore",
        "integrity_error": "BlobIntegrityError",
        "not_found_error": "BlobNotFoundError",
        "backend_error": "BlobStoreError",
    }
    missing = [export for export in names.values() if not hasattr(module, export)]
    assert not missing, f"blob backend module lacks frozen exports: {missing}"
    assert hasattr(identity, "ProjectStorageKey"), (
        "frisket.project_identity lacks the shared ProjectStorageKey"
    )
    return _BlobApi(
        **{field_name: getattr(module, export) for field_name, export in names.items()},
        project_storage_key=identity.ProjectStorageKey,
    )


@dataclass
class _StoredObject:
    data: bytes
    metadata: dict[str, str]


class _S3ClientError(Exception):
    """Dependency-free equivalent of botocore's structured ClientError."""

    def __init__(self, operation: str, code: str, status: int) -> None:
        super().__init__(f"synthetic {code}")
        self.operation_name = operation
        self.response = {
            "Error": {"Code": code, "Message": f"synthetic {code}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _FakeS3Client:
    """Small boto-shaped object store; it never resolves credentials or a network."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], _StoredObject] = {}
        self.events: list[dict[str, str]] = []
        self.corrupt_writes = False
        self.read_error: tuple[str, int] | None = None

    @staticmethod
    def _checksum(data: bytes) -> str:
        return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")

    @staticmethod
    def _body_bytes(body: Any) -> bytes:
        if isinstance(body, bytes):
            return body
        if isinstance(body, bytearray | memoryview):
            return bytes(body)
        if hasattr(body, "read"):
            return bytes(body.read())
        raise TypeError(f"unsupported fake S3 body {type(body).__name__}")

    @staticmethod
    def _client_error(operation: str, code: str, status: int) -> _S3ClientError:
        return _S3ClientError(operation, code, status)

    def _read(self, *, operation: str, bucket: str, key: str) -> _StoredObject:
        self.events.append({"op": operation, "bucket": bucket, "key": key})
        if self.read_error is not None:
            code, status = self.read_error
            raise self._client_error(operation, code, status)
        try:
            return self.objects[(bucket, key)]
        except KeyError as exc:
            raise self._client_error(operation, "NoSuchKey", 404) from exc

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        Metadata: dict[str, str] | None = None,
        **_: Any,
    ) -> dict[str, str]:
        data = self._body_bytes(Body)
        if self.corrupt_writes:
            data += b"corrupt"
        self.objects[(Bucket, Key)] = _StoredObject(data, dict(Metadata or {}))
        self.events.append({"op": "put", "bucket": Bucket, "key": Key})
        return {"ChecksumSHA256": self._checksum(data)}

    def upload_fileobj(
        self,
        Fileobj: Any,
        Bucket: str,
        Key: str,
        ExtraArgs: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.put_object(
            Bucket=Bucket,
            Key=Key,
            Body=Fileobj,
            Metadata=dict((ExtraArgs or {}).get("Metadata") or {}),
        )

    def head_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        item = self._read(operation="head", bucket=Bucket, key=Key)
        return {
            "ContentLength": len(item.data),
            "ChecksumSHA256": self._checksum(item.data),
            "Metadata": dict(item.metadata),
        }

    def get_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        item = self._read(operation="get", bucket=Bucket, key=Key)
        return {
            "Body": io.BytesIO(item.data),
            "ContentLength": len(item.data),
            "ChecksumSHA256": self._checksum(item.data),
            "Metadata": dict(item.metadata),
        }

    def download_fileobj(
        self,
        Bucket: str,
        Key: str,
        Fileobj: Any,
        **_: Any,
    ) -> None:
        item = self._read(operation="download", bucket=Bucket, key=Key)
        Fileobj.write(item.data)


def _storage_key(
    api: _BlobApi,
    *,
    storage_org_id: int = 7,
    project_slug: str = "project-a",
) -> Any:
    return api.project_storage_key(
        storage_org_id=storage_org_id,
        project_slug=project_slug,
    )


def _filesystem_store(api: _BlobApi, root: Path) -> Any:
    return api.filesystem_store(root=root)


def _s3_store(
    api: _BlobApi,
    client: _FakeS3Client,
    *,
    storage_org_id: int = 7,
    project_slug: str = "project-a",
) -> Any:
    return api.s3_store(
        client=client,
        bucket="test-project-blobs",
        prefix="acceptance",
        project_storage_key=_storage_key(
            api,
            storage_org_id=storage_org_id,
            project_slug=project_slug,
        ),
    )


def _project_with_store(path: Path, store: Any, *, name: str) -> Project:
    try:
        return Project.create(path, name=name, blob_store=store)
    except TypeError as exc:
        pytest.fail(
            f"Project.create must accept the scoped ProjectBlobStore: {exc}",
            pytrace=False,
        )


def _remote_key(client: _FakeS3Client, digest: str) -> tuple[str, str]:
    matches = [key for key in client.objects if key[1].endswith(digest)]
    assert len(matches) == 1, f"expected one remote key for {digest}, got {matches}"
    return matches[0]


@dataclass
class _RecordingStore:
    delegate: Any
    yielded_paths: list[Path] = field(default_factory=list)
    exited_paths: list[Path] = field(default_factory=list)

    def put(self, data: bytes) -> str:
        return str(self.delegate.put(data))

    def put_path(self, path: str | Path, *, expected_digest: str | None = None) -> str:
        return str(self.delegate.put_path(path, expected_digest=expected_digest))

    @contextmanager
    def materialize(self, digest: str) -> Iterator[Path]:
        with self.delegate.materialize(digest) as raw_path:
            path = Path(raw_path)
            assert path.exists(), "backend yielded a nonexistent materialization"
            self.yielded_paths.append(path)
            yield path
            assert path.exists(), "consumer lost its materialization before exit"
        self.exited_paths.append(path)
        assert not path.exists(), "remote materialization survived context exit"


@pytest.mark.gap
def test_filesystem_and_s3_backends_share_hash_verified_contract(
    tmp_path: Path,
) -> None:
    api = _blob_api()
    payload = b"same immutable bytes"
    digest = hashlib.sha256(payload).hexdigest()
    filesystem = _filesystem_store(api, tmp_path / "filesystem")
    fake_s3 = _FakeS3Client()
    s3 = _s3_store(api, fake_s3)

    for store in (filesystem, s3):
        assert store.put(payload) == digest
        with store.materialize(digest) as raw_path:
            path = Path(raw_path)
            assert path.read_bytes() == payload
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    filesystem_path = next((tmp_path / "filesystem").rglob(digest))
    filesystem_path.write_bytes(b"corrupt canonical bytes")
    with pytest.raises(api.integrity_error):
        with filesystem.materialize(digest):
            pass

    bucket_key = _remote_key(fake_s3, digest)
    fake_s3.objects[bucket_key].data = b"corrupt remote bytes"
    with pytest.raises(api.integrity_error):
        with s3.materialize(digest):
            pass

    isolated = _FakeS3Client()
    scopes = [
        _s3_store(api, isolated, storage_org_id=7, project_slug="same-slug"),
        _s3_store(api, isolated, storage_org_id=8, project_slug="same-slug"),
    ]
    for store in scopes:
        assert store.put(payload) == digest
    keys = [key for bucket, key in isolated.objects if bucket == "test-project-blobs"]
    assert len(set(keys)) == 2
    assert any("/7/same-slug/sha256/" in f"/{key}" for key in keys)
    assert any("/8/same-slug/sha256/" in f"/{key}" for key in keys)

    missing = _s3_store(api, isolated, storage_org_id=9, project_slug="same-slug")
    with pytest.raises(api.not_found_error):
        with missing.materialize(digest):
            pass

    before_invalid = list(isolated.events)
    with pytest.raises(ValueError):
        with missing.materialize("../not-a-digest"):
            pass
    assert isolated.events == before_invalid, "invalid digest reached the backend"

    for code, status in (("AccessDenied", 403), ("InternalError", 500)):
        isolated.read_error = (code, status)
        with pytest.raises(api.backend_error) as caught:
            with missing.materialize(digest):
                pass
        assert not isinstance(caught.value, api.not_found_error), (
            f"{code} was normalized to absence"
        )


@pytest.mark.gap
def test_versioned_project_purge_deletes_every_target_version_and_marker() -> None:
    """A current-key delete is not terminal deletion in a versioned bucket."""

    class _VersionedS3Client(_FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.history: list[dict[str, Any]] = []
            self.delete_requests: list[list[dict[str, str]]] = []
            self.version_list_requests: list[dict[str, Any]] = []
            self._generated_marker = 0

        def seed(
            self,
            key: str,
            version_id: str,
            *,
            delete_marker: bool = False,
        ) -> None:
            self.history.append(
                {
                    "Key": key,
                    "VersionId": version_id,
                    "DeleteMarker": delete_marker,
                }
            )

        def scoped_history(self, prefix: str) -> tuple[tuple[str, str, bool], ...]:
            return tuple(
                (
                    str(item["Key"]),
                    str(item["VersionId"]),
                    bool(item["DeleteMarker"]),
                )
                for item in self.history
                if str(item["Key"]).startswith(prefix)
            )

        def _latest(self, key: str) -> dict[str, Any] | None:
            return next(
                (item for item in reversed(self.history) if item["Key"] == key),
                None,
            )

        def list_objects_v2(
            self,
            *,
            Bucket: str,
            Prefix: str,
            **_: Any,
        ) -> dict[str, Any]:
            del Bucket
            visible: list[dict[str, str]] = []
            keys = sorted(
                {
                    str(item["Key"])
                    for item in self.history
                    if str(item["Key"]).startswith(Prefix)
                }
            )
            for key in keys:
                latest = self._latest(key)
                if latest is not None and not latest["DeleteMarker"]:
                    visible.append({"Key": key})
            return {
                "Contents": visible,
                "IsTruncated": False,
                "KeyCount": len(visible),
            }

        def list_object_versions(
            self,
            *,
            Bucket: str,
            Prefix: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.version_list_requests.append(
                {"Bucket": Bucket, "Prefix": Prefix, **kwargs}
            )
            versions: list[dict[str, Any]] = []
            markers: list[dict[str, Any]] = []
            for item in self.history:
                key = str(item["Key"])
                if not key.startswith(Prefix):
                    continue
                record = {
                    "Key": key,
                    "VersionId": str(item["VersionId"]),
                    "IsLatest": item is self._latest(key),
                }
                (markers if item["DeleteMarker"] else versions).append(record)
            return {
                "Versions": versions,
                "DeleteMarkers": markers,
                "IsTruncated": False,
            }

        def delete_objects(
            self,
            *,
            Bucket: str,
            Delete: dict[str, Any],
            **_: Any,
        ) -> dict[str, Any]:
            del Bucket
            requested = [dict(item) for item in Delete["Objects"]]
            self.delete_requests.append(requested)
            deleted: list[dict[str, str]] = []
            for item in requested:
                key = str(item["Key"])
                version_id = item.get("VersionId")
                if version_id is None:
                    self._generated_marker += 1
                    version_id = f"generated-marker-{self._generated_marker}"
                    self.seed(key, version_id, delete_marker=True)
                else:
                    self.history = [
                        existing
                        for existing in self.history
                        if not (
                            existing["Key"] == key
                            and existing["VersionId"] == version_id
                        )
                    ]
                deleted.append({"Key": key, "VersionId": str(version_id)})
            return {"Errors": [], "Deleted": deleted}

    api = _blob_api()
    client = _VersionedS3Client()
    store = _s3_store(api, client, storage_org_id=7, project_slug="project-a")
    target_prefix = "acceptance/7/project-a/sha256/"
    sibling_prefix = "acceptance/8/project-a/sha256/"
    target_visible = f"{target_prefix}{'a' * 64}"
    target_hidden = f"{target_prefix}{'b' * 64}"
    sibling_key = f"{sibling_prefix}{'a' * 64}"

    client.seed(target_visible, "target-visible-v1")
    client.seed(target_visible, "target-visible-v2")
    client.seed(target_hidden, "target-hidden-v1")
    client.seed(target_hidden, "target-hidden-delete", delete_marker=True)
    client.seed(sibling_key, "sibling-v1")
    client.seed(sibling_key, "sibling-v2")
    sibling_before = client.scoped_history(sibling_prefix)

    store.delete_project_objects()

    assert client.scoped_history(sibling_prefix) == sibling_before
    assert client.scoped_history(target_prefix) == (), (
        "project purge reported success after hiding only current keys behind "
        "delete markers"
    )
    assert client.version_list_requests
    assert all(
        "VersionId" in item for request in client.delete_requests for item in request
    )


@pytest.mark.gap
def test_object_write_precedes_reference_and_per_call_materialization_cleans_up(
    tmp_path: Path,
) -> None:
    api = _blob_api()
    fake_s3 = _FakeS3Client()
    store = _RecordingStore(_s3_store(api, fake_s3))
    project = _project_with_store(tmp_path / "ordering.frisket", store, name="Ordering")
    payload = b"verified object before SQLite reference"
    digest = hashlib.sha256(payload).hexdigest()
    try:
        fake_s3.corrupt_writes = True
        with pytest.raises(api.integrity_error):
            project.add_blob(payload, filename="evidence.bin")
        assert project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0

        fake_s3.corrupt_writes = False
        assert project.add_blob(payload, filename="evidence.bin") == digest
        assert (
            project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
            is not None
        )

        materialized: list[Path] = []
        for _ in range(2):
            with project.materialize_blob(digest) as raw_path:
                path = Path(raw_path)
                materialized.append(path)
                assert path.read_bytes() == payload
                assert path.exists()
            assert not path.exists()
        assert materialized[0] != materialized[1], (
            "remote materialization must use a fresh per-call temporary path"
        )
        assert store.exited_paths == materialized
    finally:
        project.close()


@pytest.mark.gap
def test_workspace_create_reopen_and_product_consumers_keep_blob_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _blob_api()
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.media_blobs import backfill_blob_metadata
    from frisket.ops.ocr_engines import OcrEngines
    from frisket.server import workspace as workspace_module
    from frisket.server.workspace import Workspace
    from frisket.engine.store.media_blobs import MediaBlobStore

    fake_s3 = _FakeS3Client()
    stores: list[_RecordingStore] = []
    factory_calls: list[str] = []

    def store_factory(project_slug: str) -> _RecordingStore:
        factory_calls.append(project_slug)
        store = _RecordingStore(
            _s3_store(api, fake_s3, storage_org_id=7, project_slug=project_slug)
        )
        stores.append(store)
        return store

    monkeypatch.setattr(
        workspace_module,
        "bootstrap_project_bundled_plugins",
        lambda project, *, project_id: None,
    )
    workspace = Workspace(
        tmp_path / "projects",
        project_blob_store_factory=store_factory,
    )
    created = workspace.create("Remote media")
    project_id = str(created["id"])
    assert factory_calls == [project_id], "Workspace.create bypassed its store factory"

    project = workspace.get(project_id)
    assert factory_calls == [project_id, project_id], (
        "Workspace.get/reopen did not rebuild the same scoped store"
    )
    assert project.blob_store is stores[-1]

    upload = tmp_path / "upload.png"
    upload.write_bytes(PNG)
    imported = run_action_spec(
        project,
        {
            "action_id": "import.files",
            "scope": {"kind": "project"},
            "sheet_name": "Remote media",
            "params": {
                "files": [
                    {
                        "path": str(upload),
                        "filename": upload.name,
                        "mime": "image/png",
                    }
                ],
            },
            "idempotency_key": "import_files@sha256:blob-port",
        },
        project_id=project_id,
    )
    assert imported.status == "completed", imported.errors
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE name='Remote media'"
    ).fetchone()
    assert sheet is not None
    sheet_id = int(sheet["id"])
    media_column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='media'", (sheet_id,)
    ).fetchone()
    assert media_column is not None
    media_value = next(
        iter(project.get_values(sheet_id, int(media_column["id"])).values())
    )
    digest = str(media_value["blob"])
    assert not list((project.path / "blobs").rglob(digest))

    consumed_paths: list[Path] = []

    async def fake_rapidocr(
        self: OcrEngines,
        page_paths: list[Path],
        scratch: Path,
        language: str | None,
    ) -> list[dict[str, Any]]:
        del self, scratch, language
        assert len(page_paths) == 1
        consumed_paths.extend(page_paths)
        assert page_paths[0].read_bytes() == PNG
        assert all(path.exists() for path in page_paths)
        return [{"text": "remote ocr text", "blocks": []}]

    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    stub_rapidocr_run_scope(monkeypatch)
    ocr = run_action_spec(
        project,
        {
            "action_id": "media.ocr",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
            "params": {
                "source": "media",
                "engine": "rapidocr",
            },
            "idempotency_key": "media_ocr@sha256:blob-port",
        },
        project_id=project_id,
    )
    assert ocr.status == "completed", ocr.errors
    assert consumed_paths and all(not path.exists() for path in consumed_paths)

    text_digest = project.add_blob(
        b"hello\nremote media", filename="remote.txt", mime="text/plain"
    )
    materializations_before_backfill = len(stores[-1].yielded_paths)
    metadata = backfill_blob_metadata(project)
    assert metadata["errors"] == []
    assert _remote_key(fake_s3, text_digest)
    assert len(stores[-1].yielded_paths) > materializations_before_backfill
    assert stores[-1].yielded_paths == stores[-1].exited_paths
    persisted = MediaBlobStore(project).probe_metadata(text_digest)
    assert persisted["size_bytes"] == len(b"hello\nremote media")
    assert "probe_error" not in persisted


def _looks_like_project_blob_join(node: ast.BinOp) -> bool:
    if not (
        isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == "blobs"
    ):
        return False
    return isinstance(node.left, ast.Attribute) and node.left.attr == "path"


def test_validate_project_slug_accepts_normal_lowercase_and_uppercase() -> None:
    from frisket.project_identity import validate_project_slug

    assert validate_project_slug("normal-project_1") == "normal-project_1"
    assert validate_project_slug("lower") == "lower"
    assert validate_project_slug("UPPER") == "UPPER"
    assert validate_project_slug("MiXeD-Case_42") == "MiXeD-Case_42"


@pytest.mark.parametrize(
    "slug",
    [
        "CON",
        "con",
        "Con",
        "PRN",
        "AUX",
        "NUL",
        "com1",
        "COM9",
        "lpt1",
        "LPT9",
    ],
)
def test_validate_project_slug_rejects_windows_device_stems_case_insensitively(
    slug: str,
) -> None:
    from frisket.project_identity import validate_project_slug

    with pytest.raises(ValueError, match="reserved Windows device name"):
        validate_project_slug(slug)


@pytest.mark.parametrize(
    "slug",
    ["", "a" * 97, "has space", "has/slash", "has\\backslash", " padded ", "dot.dot"],
)
def test_validate_project_slug_rejects_length_and_character_violations(
    slug: str,
) -> None:
    from frisket.project_identity import validate_project_slug

    with pytest.raises(ValueError):
        validate_project_slug(slug)


def test_project_storage_key_rejects_reserved_device_stem_slug() -> None:
    from frisket.project_identity import ProjectStorageKey

    with pytest.raises(ValueError, match="reserved Windows device name"):
        ProjectStorageKey(storage_org_id=1, project_slug="con")
    # A normal slug still round-trips unchanged (no trimming/normalization).
    key = ProjectStorageKey(storage_org_id=1, project_slug="normal-slug")
    assert key.project_slug == "normal-slug"

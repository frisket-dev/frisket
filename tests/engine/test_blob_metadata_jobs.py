"""Import completion does not wait for optional media parsers."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.engine.jobs import blob_metadata
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.server.app import create_app


def _handler(root, **kwargs):
    registry = HandlerRegistry()
    blob_metadata.register_blob_metadata_handler(
        registry, workspace_root=root, **kwargs
    )
    return registry.get(blob_metadata.BLOB_METADATA_KIND)


def test_import_returns_before_probe_and_enqueues_durable_backfill(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("import must not run a metadata parser")

    monkeypatch.setattr(
        "frisket.engine.executor.import_blob_stage.probe_for_ingest", forbidden
    )
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)  # No background worker: drain the job explicitly below.
    pid = client.post("/api/projects", json={"name": "Files"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/files?sheet_name=assets",
        files=[("files", ("notes.txt", b"source notes", "text/plain"))],
    )
    assert response.status_code == 200, response.text
    workspace = app.state.workspace
    project = workspace.get(pid)
    store = MediaBlobStore(project)
    [digest] = store.hashes_needing_metadata()
    assert store.probe_metadata(digest) == {}
    job = workspace.queue.claim("metadata-test")
    assert job.kind == blob_metadata.BLOB_METADATA_KIND
    assert job.payload["project_id"] == pid
    handler = workspace.registry.get(job.kind)
    result = handler(job.payload, JobHandlerContext.without_job_row())
    assert result == {"project_id": pid, "updated": 1}
    assert store.probe_metadata(digest)["size_bytes"] == len(b"source notes")
    assert store.hashes_needing_metadata() == []
    project.close()
    workspace.queue.close()


def test_reopen_recovers_missing_enqueue_without_blocking_reads(tmp_path, monkeypatch):
    app = create_app(tmp_path / "workspace")
    workspace = app.state.workspace
    pid = workspace.create("Existing")["id"]
    with closing(Project(workspace.root / f"{pid}.frisket")) as project:
        digest = project.add_blob(b"pending", filename="pending.txt", mime="text/plain")
    enqueue = workspace.queue.enqueue
    monkeypatch.setattr(
        workspace.queue,
        "enqueue",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("queue unavailable")),
    )
    project = workspace.get(pid)
    assert MediaBlobStore(project).blob_exists(digest)
    project.close()
    workspace._projects.clear()
    monkeypatch.setattr(workspace.queue, "enqueue", enqueue)
    reopened = workspace.get(pid)
    job = workspace.queue.claim("metadata-test")
    assert job.kind == blob_metadata.BLOB_METADATA_KIND
    assert job.payload["dedupe_key"] == "blob-metadata:recovery"
    reopened.close()
    workspace.queue.close()


def test_bounded_scan_preserves_acquisition_and_skips_completed_probes(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    with closing(Project.create(root / "files.frisket")) as project:
        digests = [
            project.add_blob(str(i).encode(), filename=f"{i}.txt", mime="text/plain")
            for i in range(5)
        ]
        store = MediaBlobStore(project)
        store.update_metadata(digests[0], {"_acquisition": {"title": "original"}})
        store.replace_probe_metadata(digests[1], {"kind": "text", "size_bytes": 99})
    monkeypatch.setattr(blob_metadata, "PROBE_BATCH_SIZE", 2)
    scanned = []
    original = MediaBlobStore.hashes_needing_metadata

    def record(self, **kwargs):
        result = original(self, **kwargs)
        assert kwargs["limit"] == 2
        assert len(result) <= 2
        scanned.extend(result)
        return result

    monkeypatch.setattr(MediaBlobStore, "hashes_needing_metadata", record)
    handler = _handler(root)
    result = handler({"project_id": "files"}, JobHandlerContext.without_job_row())
    assert result["updated"] == 4
    assert scanned == sorted(set(digests) - {digests[1]})
    with closing(Project(root / "files.frisket")) as project:
        store = MediaBlobStore(project)
        assert store.probe_metadata(digests[1])["size_bytes"] == 99
        assert store.metadata(digests[0])["_acquisition"] == {"title": "original"}


def test_failure_retries_only_remaining_blobs(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "files.frisket")) as project:
        digests = sorted(
            project.add_blob(str(i).encode(), filename=f"{i}.txt", mime="text/plain")
            for i in range(3)
        )
    original = blob_metadata.update_blob_metadata
    attempted = []

    def interrupted(project, digest):
        attempted.append(digest)
        if digest == digests[1]:
            raise OSError("temporary materialization failure")
        return original(project, digest)

    monkeypatch.setattr(blob_metadata, "update_blob_metadata", interrupted)
    handler = _handler(tmp_path)
    with pytest.raises(OSError):
        handler({"project_id": "files"}, JobHandlerContext.without_job_row())
    assert attempted == digests[:2]
    monkeypatch.setattr(blob_metadata, "update_blob_metadata", original)
    assert (
        handler({"project_id": "files"}, JobHandlerContext.without_job_row())["updated"]
        == 2
    )


def test_hosted_opener_refuses_unclaimed_payload_before_open(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("must reject before opening tenant storage")

    handler = _handler(tmp_path, project_opener=forbidden)
    with pytest.raises(ValueError, match="claimed storage identity"):
        handler(
            {"project_id": "files", "workspace_root": "/wrong"},
            JobHandlerContext.without_job_row(),
        )

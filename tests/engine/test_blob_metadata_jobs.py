"""Import completion does not wait for optional media parsers."""

from contextlib import closing
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import (
    HandlerRegistry,
    JobHandlerContext,
    SqliteJobQueue,
    Worker,
)
from frisket.engine.jobs import blob_metadata
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.server.app import create_app
from frisket.project_identity import ProjectStorageKey


def _handler(root, *, queue=None, **kwargs):
    registry = HandlerRegistry()
    blob_metadata.register_blob_metadata_handler(
        registry,
        workspace_root=root,
        queue=queue if queue is not None else Mock(),
        **kwargs,
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
    assert result == {"project_id": pid, "updated": 1, "failed": 0}
    assert store.probe_metadata(digest)["size_bytes"] == len(b"source notes")
    assert store.hashes_needing_metadata() == []
    project.close()
    workspace.queue.close()


@pytest.mark.parametrize("drop_cache", [False, True])
def test_reopen_recovers_missing_enqueue_without_blocking_reads(
    tmp_path, monkeypatch, drop_cache
):
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
    if drop_cache:
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
    queued = [{"project_id": "files"}]
    queue = Mock()
    queue.enqueue.side_effect = lambda kind, payload: queued.append(payload)
    handler = _handler(root, queue=queue)
    updates = []
    while queued:
        result = handler(queued.pop(0), JobHandlerContext.without_job_row())
        updates.append(result["updated"])
    assert updates == [2, 2, 0]
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
    result = handler({"project_id": "files"}, JobHandlerContext.without_job_row())
    assert result == {"project_id": "files", "updated": 2, "failed": 1}
    assert attempted == digests
    monkeypatch.setattr(blob_metadata, "update_blob_metadata", original)
    assert (
        handler({"project_id": "files"}, JobHandlerContext.without_job_row())["updated"]
        == 1
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


def test_worker_yields_to_other_work_between_batches(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "files.frisket")) as project:
        for i in range(5):
            project.add_blob(str(i).encode(), filename=f"{i}.txt", mime="text/plain")
    monkeypatch.setattr(blob_metadata, "PROBE_BATCH_SIZE", 2)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        blob_metadata.register_blob_metadata_handler(
            registry, workspace_root=tmp_path, queue=queue
        )
        other_work = []
        registry.register("other", lambda *_: other_work.append("ran"))
        first = queue.enqueue(
            blob_metadata.BLOB_METADATA_KIND,
            {"project_id": "files", "dedupe_key": "import-one"},
        )
        other = queue.enqueue("other", {})
        worker = Worker(queue, registry)
        assert worker.run_once()
        assert queue.get(first).status == "done"
        with closing(Project(tmp_path / "files.frisket")) as project:
            assert len(MediaBlobStore(project).hashes_needing_metadata()) == 3
        assert worker.run_once()
        assert other_work == ["ran"]
        assert queue.get(other).status == "done"
        while worker.run_once():
            pass
        with closing(Project(tmp_path / "files.frisket")) as project:
            assert MediaBlobStore(project).hashes_needing_metadata() == []


def test_continuation_preserves_claimed_tenant_not_payload_identity(
    tmp_path, monkeypatch
):
    root = tmp_path / "7"
    with closing(Project.create(root / "files.frisket")) as project:
        project.add_blob(b"a", filename="a.txt", mime="text/plain")
    monkeypatch.setattr(blob_metadata, "PROBE_BATCH_SIZE", 1)
    queue = Mock()
    opened = []

    def opener(key, path):
        opened.append((key, path))
        return Project(path)

    handler = _handler(
        root, queue=queue, project_opener=opener, workspace_root_storage_org_id=7
    )
    key = ProjectStorageKey(7, "files")
    handler(
        {
            "project_id": "wrong",
            "workspace_root": "/wrong",
            "storage_org_id": 99,
            "org_id": 99,
            CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
        },
        JobHandlerContext.from_claimed_job(trusted_org_id=8),
    )
    assert opened == [(key, root / "files.frisket")]
    [call] = queue.enqueue.call_args_list
    payload = call.args[1]
    assert payload["project_id"] == "files"
    assert payload["workspace_root"] == str(root)
    assert payload["storage_org_id"] == 7
    assert payload["org_id"] == 8
    assert CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY not in payload


def test_failed_continuation_enqueue_can_retry_without_reprobing(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "files.frisket")) as project:
        for i in range(3):
            project.add_blob(str(i).encode(), filename=f"{i}.txt", mime="text/plain")
    monkeypatch.setattr(blob_metadata, "PROBE_BATCH_SIZE", 2)
    queue = Mock()
    queue.enqueue.side_effect = OSError("queue temporarily unavailable")
    handler = _handler(tmp_path, queue=queue)
    with pytest.raises(OSError):
        handler({"project_id": "files"}, JobHandlerContext.without_job_row())
    # The first two probe documents committed before enqueue failed. A retry
    # consumes the remainder, not the already completed expensive probes.
    queue.enqueue.side_effect = None
    assert (
        handler({"project_id": "files"}, JobHandlerContext.without_job_row())["updated"]
        == 1
    )


def test_delete_stops_metadata_then_waits_for_the_actual_handler_exit(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    workspace = app.state.workspace
    pid = workspace.create("Delete media")["id"]
    project = workspace.get(pid)
    for i in range(3):
        project.add_blob(str(i).encode(), filename=f"{i}.txt", mime="text/plain")
    job_id = project._frisket_schedule_blob_metadata("import-one")
    job = workspace.queue.claim("metadata-test")
    assert job.id == job_id
    monkeypatch.setattr(blob_metadata, "PROBE_BATCH_SIZE", 1)
    probe = blob_metadata.update_blob_metadata
    calls = []

    def stop_during_probe(project, digest):
        calls.append(digest)
        result = probe(project, digest)
        response = client.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Delete media"}
        )
        assert response.status_code == 409, response.text
        assert "Stopping background metadata" in response.json()["detail"]
        assert (workspace.root / f"{pid}.frisket").exists()
        return result

    monkeypatch.setattr(blob_metadata, "update_blob_metadata", stop_during_probe)
    result = workspace.registry.get(job.kind)(
        {**job.payload, "job_id": job_id}, JobHandlerContext.without_job_row()
    )
    assert result["updated"] == len(calls) == 1
    assert workspace.queue.get(job_id).status == "cancelled"
    assert workspace.queue.list_project_jobs(pid, status="queued") == []
    # The normal worker acknowledges exit even though cancellation rejected
    # its completion write; until then deletion must not ignore its authority.
    assert not workspace.queue.complete(job_id, "metadata-test", result)
    response = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Delete media"}
    )
    assert response.status_code == 200, response.text
    assert not (workspace.root / f"{pid}.frisket").exists()
    workspace.queue.close()


def test_optional_probe_inventory_failure_does_not_block_open(tmp_path, monkeypatch):
    app = create_app(tmp_path / "workspace")
    workspace = app.state.workspace
    pid = workspace.create("Readable")["id"]

    def unavailable(*args, **kwargs):
        raise OSError("optional inventory unavailable")

    monkeypatch.setattr(MediaBlobStore, "hashes_needing_metadata", unavailable)
    project = workspace.get(pid)
    assert project.project_metadata()["name"] == "Readable"
    assert workspace.get(pid) is project
    project.close()
    workspace.queue.close()

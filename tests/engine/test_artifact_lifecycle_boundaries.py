"""Independent shared-cache removal and admission boundaries; no live network."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
from threading import Event

from filelock import FileLock
import httpx
import pytest

from frisket.ai.models import model_cache
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile
from frisket.engine.jobs import artifact_pull, model_pull_store as store
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.queue import open_queue


@pytest.fixture
def queues(tmp_path):
    values = [open_queue(workspace=tmp_path / name) for name in ("first", "second")]
    yield values
    for queue in values:
        queue.close()


def done(queue, workspace, art):
    row, created = store.create_or_get_active(
        queue.engine, workspace_root=workspace, model_ref=art.canonical
    )
    assert created
    store.mark_done(
        queue.engine, row.id, resolved_digest="sha256:fixture", resolved_size=7
    )
    return row.id


def test_other_workspace_lock_blocks_remove_without_tombstone(tmp_path, queues):
    art = normalize_artifact_ref("opus-mt:en-es")
    cache = tmp_path / "shared-cache"
    pull_id = done(queues[1], "/second", art)
    lock_path = model_cache.artifact_lock_path(art, root=cache)
    lock_path.parent.mkdir(parents=True)
    with FileLock(lock_path):
        with pytest.raises(store.UninstallInFlightError):
            artifact_pull.uninstall_artifact(
                queues[1].engine, workspace_root="/second", art=art, cache_root=cache
            )
    assert store.get(queues[1].engine, pull_id).status == store.STATUS_DONE


def test_remove_keeps_new_local_admission_out_until_bytes_are_removed(
    tmp_path, queues, monkeypatch
):
    art = normalize_artifact_ref("opus-mt:en-es")
    queue = queues[0]
    cache = tmp_path / "shared-cache"
    old_id = done(queue, "/first", art)
    entered, release, admitting = Event(), Event(), Event()

    def remove(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return True

    def admit():
        admitting.set()
        return store.create_or_get_active(
            queue.engine, workspace_root="/first", model_ref=art.canonical
        )

    monkeypatch.setattr(model_cache, "uninstall", remove)
    with ThreadPoolExecutor(max_workers=2) as pool:
        removal = pool.submit(
            artifact_pull.uninstall_artifact,
            queue.engine,
            workspace_root="/first",
            art=art,
            cache_root=cache,
        )
        try:
            assert entered.wait(5)
            admission = pool.submit(admit)
            assert admitting.wait(5)
            with pytest.raises(TimeoutError):
                admission.result(timeout=0.1)
        finally:
            release.set()
        removal.result(timeout=5)
        new, created = admission.result(timeout=5)
    assert created and new.id != old_id
    assert store.get(queue.engine, old_id).status == store.STATUS_UNINSTALLED
    assert new.status == store.STATUS_PENDING


def test_wrong_workspace_cannot_remove_another_workspaces_record_or_bytes(
    tmp_path, queues
):
    art = normalize_artifact_ref("opus-mt:en-es")
    queue = queues[0]
    cache = tmp_path / "shared-cache"
    pull_id = done(queue, "/first", art)
    target = model_cache.artifact_install_dir(art, root=cache) / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"weights")
    result = artifact_pull.uninstall_artifact(
        queue.engine, workspace_root="/second", art=art, cache_root=cache
    )
    assert result is None
    assert target.read_bytes() == b"weights"
    assert store.get(queue.engine, pull_id).status == store.STATUS_DONE


def test_cancelled_waiter_never_deletes_other_workspace_active_staging(
    tmp_path, queues
):
    art = normalize_artifact_ref("opus-mt:en-es")
    cache = tmp_path / "shared-cache"
    payload = b"weights"
    pinned = PinnedArtifact(
        ref=art.canonical,
        kind="ct2_pair",
        display_name="Fixture",
        manifest_version="fixture-v1",
        license="CC0",
        license_url="https://example.test/license",
        source_url="https://example.test/model",
        files=(
            PinnedFile(
                repo_relpath="model.bin",
                source="https://example.test/model.bin",
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            ),
        ),
    )
    rows = [
        store.create_or_get_active(
            queue.engine, workspace_root=f"/workspace-{index}", model_ref=art.canonical
        )[0]
        for index, queue in enumerate(queues)
    ]
    assert rows[0].id == rows[1].id
    entered, release = Event(), Event()

    class PausedBody(httpx.SyncByteStream):
        def __iter__(self):
            entered.set()
            assert release.wait(5)
            yield payload

    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=PausedBody())
            )
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        owner = pool.submit(
            artifact_pull.run_artifact_pull,
            client,
            engine=queues[0].engine,
            pull_id=rows[0].id,
            art=art,
            should_cancel=lambda: False,
            is_final_attempt=True,
            manifest_lookup=lambda ref: pinned,
            cache_root=cache,
        )
        try:
            assert entered.wait(5)
            staging_file = (
                model_cache.tmp_dir(
                    artifact_pull.artifact_staging_token(queues[0].engine, rows[0].id),
                    root=cache,
                )
                / "model.bin"
            )
            assert staging_file.exists()
            cancelled = artifact_pull.run_artifact_pull(
                client,
                engine=queues[1].engine,
                pull_id=rows[1].id,
                art=art,
                should_cancel=lambda: True,
                is_final_attempt=True,
                manifest_lookup=lambda ref: pinned,
                cache_root=cache,
            )
            staging_survived = staging_file.exists()
        finally:
            release.set()
        owner_error = owner.exception(timeout=5)
    assert cancelled["status"] == "cancelled"
    assert staging_survived, (
        "A cancelled waiter deleted the other workspace active staging file"
    )
    assert owner_error is None
    assert (
        model_cache.artifact_install_dir(art, root=cache) / "model.bin"
    ).read_bytes() == payload

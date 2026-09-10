"""manifest.json is published atomically and written under one lock.

manifest.json is an externalized summary that every cheap reader depends on:
``Workspace.list()`` (src/frisket/server/workspace.py) builds the whole project
listing from it without opening a single sqlite db, and team's bundle-validity
check reads it to decide whether a `.frisket` directory is a real project.

Two properties keep those readers honest, and neither came for free:

*Atomic publish.* ``write_text`` truncates at open() and only fills the file at
close(). JSON has no valid prefixes, so a reader inside that window does not
get a stale-but-sane answer — it gets a parse error, and a crash inside the
window leaves an empty manifest on disk permanently.

*Serialized read-modify-write.* The writer merges updates into what it read, so
two concurrent writers each merge onto their own stale read and the loser's
field is silently reverted. The writers are genuinely concurrent: a run's
finalization refreshes ``pending_review_count`` from the `frisket worker`
process while an HTTP request in the app process can be writing ``sensitive``,
the flag that suppresses telemetry.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

import frisket.engine.store.project_meta as project_meta
from frisket.engine.store import Project
from frisket.server.workspace import Workspace


def _sample_manifest(path: Path, seen: list[str]) -> None:
    """Classify one read of the manifest as a reader would experience it."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        seen.append("missing")
        return
    if raw == b"":
        seen.append("empty")
        return
    try:
        json.loads(raw)
    except ValueError:
        seen.append("unparseable")
        return
    seen.append("ok")


def _sample_until(path: Path, stop: threading.Event) -> list[str]:
    seen: list[str] = []
    while not stop.is_set():
        _sample_manifest(path, seen)
    return seen


def test_a_concurrent_reader_never_sees_a_half_written_manifest(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "sampled.frisket", name="Sampled")
    manifest_path = project.path / "manifest.json"
    stop = threading.Event()
    seen: list[str] = []

    def reader() -> None:
        seen.extend(_sample_until(manifest_path, stop))

    # The claim is about a window between two syscalls, which only a genuinely
    # concurrent reader can observe.
    # realtime: no duration is asserted, only that a bad sample never appears.
    sampler = threading.Thread(target=reader, name="manifest-sampler")
    sampler.start()
    try:
        for index in range(800):
            project._write_manifest(pending_review_count=index)
    finally:
        stop.set()
        sampler.join()
    project.close()

    assert seen, "the sampler observed nothing; the race was never exercised"
    assert set(seen) == {"ok"}, {
        outcome: seen.count(outcome) for outcome in sorted(set(seen))
    }


def test_a_concurrent_reader_never_sees_a_half_written_initial_manifest(
    tmp_path: Path,
) -> None:
    """``Project.create``'s manifest gets the same publish as every later one."""
    bundle = tmp_path / "created.frisket"
    project = Project.create(bundle, name="Created")
    project.close()
    manifest_path = bundle / "manifest.json"
    stop = threading.Event()
    seen: list[str] = []

    def reader() -> None:
        seen.extend(_sample_until(manifest_path, stop))

    # realtime: see the sampler above.
    sampler = threading.Thread(target=reader, name="initial-manifest-sampler")
    sampler.start()
    try:
        for _ in range(800):
            Project._write_initial_manifest(
                bundle,
                "Created",
                None,
                storage_id="frisket.bundle.v1:test",
                sensitive=False,
            )
    finally:
        stop.set()
        sampler.join()

    assert seen, "the sampler observed nothing; the race was never exercised"
    assert set(seen) == {"ok"}, {
        outcome: seen.count(outcome) for outcome in sorted(set(seen))
    }


def test_two_concurrent_writers_cannot_revert_each_others_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-modify-write is serialized, so neither merge is built on a
    stale read. Deterministic in both directions: the parked writer has read
    the manifest and is holding whatever exclusion the writer provides. If the
    second writer can get in and publish, the parked one's own publish reverts
    it — which is the lost-update this lock exists to prevent."""
    project = Project.create(tmp_path / "contended.frisket", name="Contended")
    parked_has_read = threading.Event()
    other_writer_done = threading.Event()
    real_read = project_meta._read_manifest

    def read_then_park(target: Any) -> dict[str, Any]:
        manifest = real_read(target)
        if threading.current_thread().name == "parked-writer":
            parked_has_read.set()
            # Falls through on timeout when the lock (correctly) keeps the
            # other writer out, so this can never deadlock.
            other_writer_done.wait(timeout=1.0)
        return manifest

    monkeypatch.setattr(project_meta, "_read_manifest", read_then_park)

    def parked_write() -> None:
        project._write_manifest(alpha="from-the-parked-writer")

    # realtime: two writers must genuinely overlap for a lost update to exist.
    parked = threading.Thread(target=parked_write, name="parked-writer")
    parked.start()
    assert parked_has_read.wait(timeout=30), "the parked writer never started"
    project._write_manifest(beta="from-the-other-writer")
    other_writer_done.set()
    parked.join(timeout=30)
    assert not parked.is_alive()

    manifest = json.loads((project.path / "manifest.json").read_text())
    project.close()
    assert manifest.get("alpha") == "from-the-parked-writer"
    assert manifest.get("beta") == "from-the-other-writer", (
        "the parked writer merged onto a stale read and reverted the other "
        "writer's field"
    )


def test_one_unreadable_manifest_costs_one_row_not_the_whole_listing(
    tmp_path: Path,
) -> None:
    """A manifest corrupted some other way (truncated by a crash before the
    atomic publish landed, half-restored, hand-edited) must not take every
    project in the workspace down with it."""
    root = tmp_path / "data"
    root.mkdir()
    for name in ("alpha", "beta", "gamma"):
        Project.create(root / f"{name}.frisket", name=name).close()
    (root / "beta.frisket" / "manifest.json").write_text("")

    listed = Workspace(root).list()

    assert sorted(row["id"] for row in listed) == ["alpha", "gamma"]

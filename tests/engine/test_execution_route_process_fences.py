"""Real-process probes for installation identity and standing-consent fences.

Threads cannot prove either contract: ``FileLock`` and SQLite's writer lock
exist specifically to coordinate independent interpreter processes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import (
    COST_CONSENT_ENV,
    INSTALLATION_ID_FILENAME,
    STANDING_COST_POLICY,
    _read_or_mint_installation_id,
)


_INSTALLATION_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

import frisket.engine.store.execution_routes as routes

root = Path(sys.argv[1])
role = sys.argv[2]
barrier = Path(sys.argv[3])
release = Path(sys.argv[4])
ready = Path(sys.argv[5])

def signal(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

if role == "holder":
    original_write_text = Path.write_text

    def pause_with_empty_scratch(path, data, *args, **kwargs):
        if (
            path.parent == root
            and path.name.startswith(".installation_id.")
            and path.name.endswith(".tmp")
        ):
            with path.open("w", encoding="utf-8") as handle:
                signal(barrier)
                deadline = time.monotonic() + 15  # realtime: bounded real-process crash barrier
                while not release.exists():
                    if time.monotonic() >= deadline:  # realtime: bounds a stuck child
                        raise TimeoutError("installation-id barrier was never released")
                    time.sleep(0.01)  # realtime: poll a cross-process filesystem barrier
                return handle.write(data)
        return original_write_text(path, data, *args, **kwargs)

    Path.write_text = pause_with_empty_scratch

signal(ready)
value = routes._read_or_mint_installation_id(root)
print(json.dumps({"pid": os.getpid(), "installation_id": value}), flush=True)
"""


_CONSENT_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

from frisket.engine.store import Project
import frisket.engine.store.execution_routes as routes

bundle = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
target_threshold = sys.argv[4]

# Open without changing policy, then race only the explicit reconciliation
# after both independent processes have reached the barrier.
os.environ[routes.COST_CONSENT_ENV] = "1"
project = Project(bundle)
os.environ[routes.COST_CONSENT_ENV] = target_threshold

original_consent_row = routes._consent_row
delayed = [False]

def widen_read_insert_window(row):
    consent = original_consent_row(row)
    if not delayed[0]:
        delayed[0] = True
        time.sleep(0.35)  # realtime: widens the real SQLite process-race window
    return consent

routes._consent_row = widen_read_insert_window
ready.write_text(str(os.getpid()), encoding="utf-8")
deadline = time.monotonic() + 15  # realtime: bounds the real-process start barrier
while not release.exists():
    if time.monotonic() >= deadline:  # realtime: bounds a stuck child
        raise TimeoutError("standing-consent barrier was never released")
    time.sleep(0.01)  # realtime: poll a cross-process filesystem barrier

try:
    consent = routes.ensure_standing_cost_consent(project)
    print(json.dumps({"pid": os.getpid(), "consent_id": consent.id}), flush=True)
finally:
    project.close()
"""


def _spawn_worker(code: str, *args: object) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            # subprocess-boundary: these probes assert FileLock/SQLite visibility across independent PIDs
            sys.executable,
            "-c",
            code,
            *(str(arg) for arg in args),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def _wait_for(path: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout  # realtime: bounds a real-process barrier
    while not path.exists():
        if time.monotonic() >= deadline:  # realtime: bounds a stuck child
            pytest.fail(f"timed out waiting for child marker: {path.name}")
        time.sleep(0.01)  # realtime: poll a cross-process filesystem barrier


def _finish_json(
    process: subprocess.Popen[str], *, timeout: float = 15
) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"child {process.pid} hung: stdout={stdout!r}, stderr={stderr!r}")
    assert process.returncode == 0, (
        f"child {process.pid} failed with {process.returncode}: "
        f"stdout={stdout!r}, stderr={stderr!r}"
    )
    return json.loads(stdout)


def _kill_leftovers(*processes: subprocess.Popen[str] | None) -> None:
    for process in processes:
        if process is not None and process.poll() is None:
            process.kill()
    for process in processes:
        if process is not None and process.poll() is None:
            process.communicate(timeout=5)


def test_installation_id_serializes_two_processes_at_empty_scratch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    barrier = tmp_path / "empty-scratch"
    release = tmp_path / "release-holder"
    holder_ready = tmp_path / "holder-ready"
    contender_ready = tmp_path / "contender-ready"
    holder = _spawn_worker(
        _INSTALLATION_WORKER,
        root,
        "holder",
        barrier,
        release,
        holder_ready,
    )
    contender: subprocess.Popen[str] | None = None
    try:
        _wait_for(barrier)
        scratch = list(root.glob(".installation_id.*.tmp"))
        assert len(scratch) == 1
        assert scratch[0].read_bytes() == b""
        assert not (root / INSTALLATION_ID_FILENAME).exists()

        contender = _spawn_worker(
            _INSTALLATION_WORKER,
            root,
            "contender",
            tmp_path / "unused",
            release,
            contender_ready,
        )
        _wait_for(contender_ready)
        # realtime: the positive liveness bound lets the contender attempt
        # the real OS lock while the holder remains paused inside its write.
        time.sleep(0.2)  # realtime: exercises real FileLock contention
        assert contender.poll() is None
        assert not (root / INSTALLATION_ID_FILENAME).exists()

        release.touch()
        holder_result = _finish_json(holder)
        contender_result = _finish_json(contender)
    finally:
        release.touch(exist_ok=True)
        _kill_leftovers(holder, contender)

    assert holder_result["pid"] != contender_result["pid"]
    assert holder_result["installation_id"] == contender_result["installation_id"]
    assert (
        root.joinpath(INSTALLATION_ID_FILENAME).read_text(encoding="utf-8").strip()
        == holder_result["installation_id"]
    )


def test_installation_id_does_not_replace_an_unreadable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / INSTALLATION_ID_FILENAME
    original = b"stable-installation-id\n"
    path.write_bytes(original)
    original_read_text = Path.read_text

    def unreadable_identity(candidate: Path, *args: Any, **kwargs: Any) -> str:
        if candidate == path:
            raise PermissionError("installation identity is temporarily unreadable")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_identity)
    with pytest.raises(PermissionError, match="temporarily unreadable"):
        _read_or_mint_installation_id(root)

    assert path.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL recovery is POSIX-specific")
def test_installation_id_recovers_after_sigkill_of_empty_scratch_holder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    barrier = tmp_path / "empty-scratch"
    release = tmp_path / "never-release-holder"
    holder_ready = tmp_path / "holder-ready"
    contender_ready = tmp_path / "contender-ready"
    holder = _spawn_worker(
        _INSTALLATION_WORKER,
        root,
        "holder",
        barrier,
        release,
        holder_ready,
    )
    contender: subprocess.Popen[str] | None = None
    reader: subprocess.Popen[str] | None = None
    try:
        _wait_for(barrier)
        contender = _spawn_worker(
            _INSTALLATION_WORKER,
            root,
            "contender",
            tmp_path / "unused",
            release,
            contender_ready,
        )
        _wait_for(contender_ready)
        holder.kill()
        holder.communicate(timeout=5)
        assert holder.returncode is not None and holder.returncode < 0

        contender_result = _finish_json(contender)
        reader = _spawn_worker(
            _INSTALLATION_WORKER,
            root,
            "reader",
            tmp_path / "unused-reader",
            release,
            tmp_path / "reader-ready",
        )
        reader_result = _finish_json(reader)
    finally:
        _kill_leftovers(holder, contender, reader)

    assert contender_result["pid"] != reader_result["pid"]
    assert contender_result["installation_id"] == reader_result["installation_id"]
    assert (
        root.joinpath(INSTALLATION_ID_FILENAME).read_text(encoding="utf-8").strip()
        == contender_result["installation_id"]
    )
    stale_scratch = list(root.glob(".installation_id.*.tmp"))
    assert len(stale_scratch) == 1
    assert stale_scratch[0].read_bytes() == b""


def test_standing_consent_process_race_mints_one_successor_per_policy_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(COST_CONSENT_ENV, "1")
    bundle = tmp_path / "race.frisket"
    seed = Project.create(bundle, name="race")
    seed.close()

    ready_one = tmp_path / "ready-one"
    ready_two = tmp_path / "ready-two"
    release = tmp_path / "release"
    first = _spawn_worker(_CONSENT_WORKER, bundle, ready_one, release, "7")
    second = _spawn_worker(_CONSENT_WORKER, bundle, ready_two, release, "7")
    try:
        _wait_for(ready_one)
        _wait_for(ready_two)
        release.touch()
        first_result = _finish_json(first)
        second_result = _finish_json(second)
    finally:
        release.touch(exist_ok=True)
        _kill_leftovers(first, second)

    assert first_result["pid"] != second_result["pid"]
    assert first_result["consent_id"] == second_result["consent_id"]
    with sqlite3.connect(bundle / "project.db") as db:
        rows = db.execute(
            "SELECT policy_params_json FROM consents "
            "WHERE standing_policy=? ORDER BY granted_at, id",
            (STANDING_COST_POLICY,),
        ).fetchall()
    thresholds = [json.loads(row[0])["threshold_usd"] for row in rows]
    assert thresholds == ["1", "7"]

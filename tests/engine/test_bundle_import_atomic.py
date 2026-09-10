"""PC-5: local bundle imports publish only after staged verification."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from frisket.engine.jobs import HandlerRegistry
from frisket.engine.store import Project
from frisket.server.workspace import Workspace

pytestmark = pytest.mark.realtime


def _export_bundle(
    tmp_path: Path, *, with_blob: bool = False
) -> tuple[Path, str | None]:
    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        digest = project.add_blob(b"original blob") if with_blob else None
        archive = project.export(tmp_path / "source.frisket.zip")
    finally:
        project.close()
    return archive, digest


def test_import_verifies_before_publishing_target(tmp_path: Path) -> None:
    archive, _ = _export_bundle(tmp_path)
    malformed = tmp_path / "malformed.frisket.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(malformed, "w") as dest:
        for member in source.infolist():
            data = (
                b"not a sqlite database"
                if member.filename == "project.db"
                else source.read(member)
            )
            dest.writestr(member, data)

    target = tmp_path / "workspace" / "restored.frisket"
    with pytest.raises(sqlite3.DatabaseError):
        Project.import_bundle(malformed, target)

    assert not target.exists()
    assert list(target.parent.glob("*.frisket")) == []


def test_import_hash_failure_never_publishes_target(tmp_path: Path) -> None:
    archive, digest = _export_bundle(tmp_path, with_blob=True)
    assert digest is not None
    tampered = tmp_path / "tampered.frisket.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as dest:
        for member in source.infolist():
            data = source.read(member)
            if member.filename.endswith(digest):
                data = b"tampered"
            dest.writestr(member, data)

    target = tmp_path / "workspace" / "restored.frisket"
    with pytest.raises(ValueError, match="hash verification"):
        Project.import_bundle(tampered, target)

    assert not target.exists()
    assert list(target.parent.glob("*.frisket")) == []


def test_sigkill_mid_import_leaves_target_absent_from_workspace_listing(
    tmp_path: Path,
) -> None:
    archive, _ = _export_bundle(tmp_path)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    target = workspace_root / "restored.frisket"
    extracted = tmp_path / "extracted.marker"
    child_code = """
import sys
import time
import zipfile
from pathlib import Path

from frisket.engine.store import Project

original_extractall = zipfile.ZipFile.extractall

def extract_then_pause(archive, *args, **kwargs):
    result = original_extractall(archive, *args, **kwargs)
    Path(sys.argv[3]).write_text("extracted")
    while True:
        time.sleep(1)
    return result

zipfile.ZipFile.extractall = extract_then_pause
Project.import_bundle(sys.argv[1], sys.argv[2])
"""
    process = subprocess.Popen(
        [
            # subprocess-boundary: SIGKILL visibility requires a real importer
            sys.executable,
            "-c",
            child_code,
            str(archive),
            str(target),
            str(extracted),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not extracted.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                pytest.fail(
                    "import child exited before the extraction barrier: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.02)
        assert extracted.exists(), "import child did not reach extraction barrier"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert not target.exists()
    workspace = Workspace(
        workspace_root,
        registry=HandlerRegistry(),
        enable_local_model_pull=False,
    )
    try:
        assert workspace.list() == []
    finally:
        workspace.queue.close()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL recovery is POSIX-specific")
def test_sigkill_after_publication_leaves_a_fully_valid_bundle(tmp_path: Path) -> None:
    archive, _ = _export_bundle(tmp_path)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    target = workspace_root / "restored.frisket"
    published = tmp_path / "published.marker"
    child_code = """
import os
import sys
import time
from pathlib import Path

from frisket.engine.store import Project

target = Path(sys.argv[2])
published = Path(sys.argv[3])
original_replace = os.replace

def replace_then_pause(source, destination):
    result = original_replace(source, destination)
    source_path = Path(source)
    if (
        Path(destination) == target
        and source_path.parent == target.parent
        and source_path.name.startswith(f".{target.name}.import-")
    ):
        published.write_text("published")
        while True:
            time.sleep(1)
    return result

os.replace = replace_then_pause
Project.import_bundle(sys.argv[1], target)
"""
    process = subprocess.Popen(
        [
            # subprocess-boundary: SIGKILL at the atomic publication boundary
            sys.executable,
            "-c",
            child_code,
            str(archive),
            str(target),
            str(published),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not published.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                pytest.fail(
                    "import child exited before the publication barrier: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.02)
        assert published.exists(), "import child did not reach publication barrier"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["project_id"] == target.stem
    installation_id = (workspace_root / "installation_id").read_text().strip()
    with sqlite3.connect(target / "project.db") as db:
        principal = db.execute(
            "SELECT value FROM meta WHERE key='instance_principal'"
        ).fetchone()[0]
        standing_count = db.execute(
            "SELECT count(*) FROM consents WHERE standing_policy=? AND actor=?",
            ("cost_under_gate_v1", principal),
        ).fetchone()[0]
    assert principal == f"deployment:{installation_id}"
    assert standing_count == 1

    workspace = Workspace(
        workspace_root,
        registry=HandlerRegistry(),
        enable_local_model_pull=False,
    )
    try:
        assert [row["id"] for row in workspace.list()] == [target.stem]
    finally:
        workspace.queue.close()


def test_successful_import_atomically_publishes_final_path(tmp_path: Path) -> None:
    archive, _ = _export_bundle(tmp_path)
    workspace_root = tmp_path / "workspace"
    target = workspace_root / "restored.frisket"

    restored = Project.import_bundle(archive, target)
    try:
        assert restored.path == target
        assert target.is_dir()
        assert (target / "manifest.json").is_file()
        manifest = json.loads((target / "manifest.json").read_text())
        assert manifest["project_id"] == target.stem
    finally:
        restored.close()

    assert list(workspace_root.glob(".restored.frisket.import-*.tmp")) == []

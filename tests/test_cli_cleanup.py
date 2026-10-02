"""Offline cleanup requires an explicit apply and an idle local launcher."""

import json

import pytest

from frisket.cli.cleanup import cleanup
from frisket.engine.store import Project
from frisket.server.standalone import StandaloneLifetimeLock


def test_cleanup_defaults_to_dry_run_then_reclaims_bytes(tmp_path, capsys):
    path = tmp_path / "data.frisket"
    project = Project.create(path)
    digest = project.add_blob(b"unused original")
    project.close()
    blob = path / "blobs" / digest[:2] / digest

    assert cleanup([str(path)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["bytes_freed"] == 0
    assert blob.read_bytes() == b"unused original"

    assert cleanup([str(path), "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["bytes_freed"] == len(b"unused original")
    assert not blob.exists()


@pytest.mark.parametrize("flags", [[], ["--apply"]])
def test_running_launcher_blocks_even_dry_run(tmp_path, capsys, flags):
    path = tmp_path / "data.frisket"
    project = Project.create(path)
    digest = project.add_blob(b"keep while running")
    project.close()
    before = (path / "project.db").read_bytes()
    with StandaloneLifetimeLock(tmp_path):
        assert cleanup([str(path), *flags]) == 2
    assert "stop Frisket" in capsys.readouterr().err
    assert (path / "project.db").read_bytes() == before
    assert (path / "blobs" / digest[:2] / digest).exists()
    assert cleanup([str(path)]) == 0


def test_cleanup_missing_bundle_does_not_create_workspace(tmp_path, capsys):
    path = tmp_path / "missing" / "absent.frisket"
    assert cleanup([str(path), "--apply"]) == 2
    assert "not a project bundle" in capsys.readouterr().err
    assert not path.parent.exists()

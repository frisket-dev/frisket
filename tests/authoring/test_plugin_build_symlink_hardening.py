"""Regression checks for the second-model review symlink hardening of
`frisket plugin build` (`src/frisket/authoring/plugin_build.py`).

Regardless of the author-local trust model, a build must never read through or
write through a symlink at one of the paths it owns: a stray symlink (e.g.
``plugin.json -> /etc/hosts`` or ``.frisket-sdk -> ~/.ssh``) would otherwise let
the build clobber or read an out-of-tree target. The guard runs before any
vendoring/emit and before the node/PATH check, so these tests need no node.

Workspaces are created through the REAL `frisket plugin init` entry point.
"""

from __future__ import annotations

import io
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from frisket.cli import plugin_cmd as plugin_cli
from frisket.authoring.plugin_build import plugin_build


def _init(tmp_path: Path, plugin_id: str) -> Path:
    """The SDK-shaped workspace, which is the one owning plugin.config.mjs,
    .frisket-sdk/ and workbench-descriptors.json."""
    root = tmp_path / plugin_id.replace(".", "_")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_cli(
            ["init", "--id", plugin_id, "--output", str(root), "--with", "view"]
        )
    assert code == 0, err.getvalue()
    assert (root / "plugin.config.mjs").is_file()
    return root


def _init_backend(tmp_path: Path, plugin_id: str) -> Path:
    """The backend-only Action workspace: plugin.py alone, generating
    plugin.json. It has its own symlink guards on both paths."""
    root = tmp_path / plugin_id.replace(".", "_")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_cli(["init", "--id", plugin_id, "--output", str(root)])
    assert code == 0, err.getvalue()
    assert (root / "plugin.py").is_file()
    assert not (root / "plugin.config.mjs").exists()
    return root


def _build(root: Path) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_build(root)
    return code, err.getvalue()


@pytest.mark.parametrize("artifact", ["plugin.json", "workbench-descriptors.json"])
def test_build_refuses_symlinked_emit_target_and_never_writes_through_it(
    tmp_path: Path, artifact: str
) -> None:
    root = _init(tmp_path, "acme.symlink_emit")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not clobber me\n", encoding="utf-8")

    os.symlink(outside, root / artifact)

    code, err = _build(root)
    assert code == 1
    assert "symlinked" in err
    # The out-of-tree target is untouched: the build never followed the link.
    assert outside.read_text(encoding="utf-8") == "do not clobber me\n"


def test_build_refuses_symlinked_frisket_sdk_dir_before_vendoring(
    tmp_path: Path,
) -> None:
    root = _init(tmp_path, "acme.symlink_sdk")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()

    # `frisket plugin init` may already scaffold a real .frisket-sdk dir;
    # replace it with a symlink to the out-of-tree target.
    sdk_path = root / ".frisket-sdk"
    if sdk_path.exists() or sdk_path.is_symlink():
        shutil.rmtree(sdk_path)
    os.symlink(outside_dir, sdk_path, target_is_directory=True)

    code, err = _build(root)
    assert code == 1
    assert "symlinked" in err
    # Nothing was vendored into the symlinked directory target.
    assert list(outside_dir.iterdir()) == []


def test_build_refuses_symlinked_plugin_config(tmp_path: Path) -> None:
    root = _init(tmp_path, "acme.symlink_config")
    real_config = (root / "plugin.config.mjs").read_text(encoding="utf-8")
    outside_config = tmp_path / "evil.config.mjs"
    outside_config.write_text(real_config, encoding="utf-8")
    (root / "plugin.config.mjs").unlink()
    os.symlink(outside_config, root / "plugin.config.mjs")

    code, err = _build(root)
    assert code == 1
    assert "symlinked plugin.config.mjs" in err


@pytest.mark.parametrize("artifact", ["plugin.py", "plugin.json"])
def test_backend_build_refuses_symlinked_source_or_emit_target(
    tmp_path: Path, artifact: str
) -> None:
    """Same guarantee on the backend-only path: a stray symlink at plugin.py
    or plugin.json must stop the build before it reads or writes through it."""
    root = _init_backend(tmp_path, "acme.symlinkbackend")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")

    target = root / artifact
    if target.exists():
        target.unlink()
    target.symlink_to(outside)

    code, err = _build(root)
    assert code == 1, err
    assert "symlink" in err.lower()
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert os.path.islink(target)

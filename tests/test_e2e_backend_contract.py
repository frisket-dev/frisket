from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e" / "e2e_backend.sh"


def _run_backend_entrypoint(tmp_path: Path, *, minimal: bool) -> list[str]:
    """Run the launcher against a fake uv binary and return its exact argv."""

    fixture_root = tmp_path / "fixture"
    scripts = fixture_root / "scripts" / "e2e"
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    capture = tmp_path / "uv-args"
    scripts.mkdir(parents=True)
    bin_dir.mkdir()
    home.mkdir()
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)

    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FRISKET_TEST_UV_ARGS"\n')
    uv.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FRISKET_E2E_WS": str(workspace),
        "FRISKET_E2E_BASE_WS": str(workspace),
        "FRISKET_E2E_BACKEND_PORT": "8123",
        "FRISKET_TEST_UV_ARGS": str(capture),
    }
    if minimal:
        env["FRISKET_E2E_MINIMAL_DEPS"] = "1"

    subprocess.run(
        ["bash", str(scripts / SCRIPT.name)],
        check=True,
        cwd=fixture_root,
        env=env,
    )
    return capture.read_text().splitlines()


def test_minimal_mode_uses_the_pre_synced_base_environment(tmp_path: Path) -> None:
    assert _run_backend_entrypoint(tmp_path, minimal=True) == [
        "run",
        "--no-sync",
        "frisket",
        str(tmp_path / "workspace"),
        "8123",
    ]


def test_default_mode_adds_entities_to_the_base_e2e_runtime(tmp_path: Path) -> None:
    assert _run_backend_entrypoint(tmp_path, minimal=False) == [
        "run",
        "--extra",
        "entities",
        "frisket",
        str(tmp_path / "workspace"),
        "8123",
    ]

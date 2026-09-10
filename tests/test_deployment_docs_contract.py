from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POSIX_SHELL = shutil.which("sh")


@pytest.mark.skipif(
    POSIX_SHELL is None,
    reason="canonical server installer is a POSIX shell program",
)
def test_documented_installer_flags_exist_in_installer_help() -> None:
    result = subprocess.run(
        [POSIX_SHELL, str(ROOT / "scripts" / "release" / "frisket-install"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    help_text = result.stdout + result.stderr
    for flag in (
        "--package",
        "--tunnel",
        "--domain",
        "--behind-proxy",
        "--yes",
        "--check",
        "--upgrade",
    ):
        assert flag in help_text

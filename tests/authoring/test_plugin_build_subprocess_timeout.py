from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from frisket.authoring import plugin_build as plugin_build_module
from frisket.cli import plugin_cmd as plugin_cli
from frisket.authoring.plugin_build import (
    _extract_module_exports,
    _resolve_esbuild,
    _scan_import_specifiers,
    plugin_build,
)

FRONTEND_PLUGIN_JS = "frontend/plugin.js"
FRONTEND_PLUGIN_TS = "frontend/plugin.ts"


def _init(tmp_path: Path, plugin_id: str, *features: str) -> Path:
    root = tmp_path / plugin_id.replace(".", "_")
    argv = ["init", "--id", plugin_id, "--output", str(root)]
    for feature in features:
        argv += ["--with", feature]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_cli(argv)
    assert code == 0, err.getvalue()
    return root


def _build(root: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_build(root)
    return code, out.getvalue(), err.getvalue()


def _timeout_run(cmd: list[str], **kwargs: object) -> None:
    assert kwargs.get("timeout") == plugin_build_module._SUBPROCESS_TIMEOUT_SECONDS
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])


# ---------------------------------------------------------------------------
# Whole-build timeout handling: a hung node/esbuild child must fail the build
# cleanly rather than block the CLI (or the plugin_dev watch loop) forever.
# ---------------------------------------------------------------------------


def test_config_run_timeout_fails_cleanly_without_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init(tmp_path, "demo.cfgtimeout", "view")
    monkeypatch.setattr(plugin_build_module.subprocess, "run", _timeout_run)

    code, _out, err = _build(root)

    assert code == 1
    assert "timed out" in err
    assert not (root / "plugin.json").exists()


def test_frontend_compile_timeout_restores_prior_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_id = "demo.compiletimeout"
    root = _init(tmp_path, plugin_id, "view")
    (root / FRONTEND_PLUGIN_JS).unlink(missing_ok=True)
    (root / FRONTEND_PLUGIN_TS).write_text(
        "export const MainView = ({ React }: { React: any }) =>"
        " React.createElement('section', {}, 'ok');\n",
        encoding="utf-8",
    )
    code, _out, err = _build(root)
    assert code == 0, err
    good_snapshot = (root / FRONTEND_PLUGIN_JS).read_bytes()

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if "--bundle" in cmd:
            return _timeout_run(cmd, **kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(plugin_build_module.subprocess, "run", fake_run)

    code, _out, err = _build(root)

    assert code == 1
    assert "timed out" in err
    assert (root / FRONTEND_PLUGIN_JS).read_bytes() == good_snapshot


# ---------------------------------------------------------------------------
# Post-emit validation helpers: each degrades to its existing "could not
# parse" / "threw on import" failure shape rather than crashing.
# ---------------------------------------------------------------------------


def test_scan_import_specifiers_returns_none_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / "plugin.js"
    module_path.write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(plugin_build_module.subprocess, "run", _timeout_run)

    specifiers, stderr = _scan_import_specifiers(_resolve_esbuild(), module_path)

    assert specifiers is None
    assert "timed out" in stderr


def test_extract_module_exports_returns_none_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / "plugin.js"
    module_path.write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(plugin_build_module.subprocess, "run", _timeout_run)

    exports, stderr = _extract_module_exports("node", module_path)

    assert exports is None
    assert "timed out" in stderr

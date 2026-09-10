"""Top-level `frisket` dispatch must never mistake a flag or typo for a
workspace path. Regression: `frisket --help` used to create a directory named
"--help" and boot a server instead of printing help (create_app mkdir's the
workspace). These run in an empty cwd and assert nothing gets created."""

import socket
import subprocess
import sys
from pathlib import Path

import pytest

# The venv console script for [project.scripts] frisket, invoked directly
# rather than through `uv run`: these tests run in an empty tmp cwd and
# assert nothing gets created there, and a bare `uv run` outside the project
# can emit env-manager chatter (or materialize env state) that has nothing
# to do with frisket's own dispatch behavior.
FRISKET_CLI = str(Path(sys.executable).with_name("frisket"))


def _run(args, cwd):
    return subprocess.run(
        [FRISKET_CLI, *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
    )


def test_help_prints_usage_and_creates_no_workspace(tmp_path):
    proc = _run(["--help"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Usage:" in proc.stdout
    assert "worker" in proc.stdout
    assert not (tmp_path / "--help").exists()
    assert list(tmp_path.iterdir()) == []


def test_help_lists_every_registered_subcommand():
    """Usage is generated from the subcommand registry, so every command that
    dispatches must appear in --help (guards against drift)."""
    import frisket.cli as cli

    usage = cli._usage()
    for name in cli._subcommands():
        assert f"frisket {name}" in usage, name


def test_lazy_commands_survive_import_first_order(tmp_path):
    script = """
import importlib
import sys

import frisket.cli as cli

importlib.import_module("frisket.cli.action")
action = cli.action_cmd
for _ in range(2):
    try:
        cli.action_cmd(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert cli.action_cmd is action
assert "frisket.actions.system" not in sys.modules

importlib.import_module("frisket.cli.plugin")
plugin = cli.plugin_cmd
try:
    cli.plugin_cmd(["--help"])
except SystemExit as exc:
    assert exc.code == 0
assert cli.plugin_cmd is plugin
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "usage: frisket action" in proc.stdout


def test_h_flag_alias_creates_no_workspace(tmp_path):
    proc = _run(["-h"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Usage:" in proc.stdout
    assert not (tmp_path / "-h").exists()
    assert list(tmp_path.iterdir()) == []


def test_version_flag(tmp_path):
    proc = _run(["--version"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().startswith("frisket ")
    assert list(tmp_path.iterdir()) == []


def test_unknown_option_errors_without_creating_dir(tmp_path):
    proc = _run(["--port", "9000"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unknown option" in proc.stderr
    assert not (tmp_path / "--port").exists()
    assert list(tmp_path.iterdir()) == []


def test_non_integer_port_errors_without_booting(tmp_path):
    # "./myws" (rather than bare "myws") so the unknown-command guard treats
    # this as a pathlike workspace arg and lets it through to PORT parsing.
    proc = _run(["./myws", "notaport"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "PORT" in proc.stderr
    # Port is validated before create_app, so no workspace is created.
    assert not (tmp_path / "myws").exists()


# --- workspace-creation confirmation (unit; the serve path itself boots a
#     server, so we exercise the guard helper directly) ----------------------


def test_confirm_existing_workspace_skips_prompt(tmp_path, monkeypatch):
    import frisket.cli as cli

    def boom(_prompt):
        raise AssertionError("must not prompt for an existing workspace")

    monkeypatch.setattr("builtins.input", boom)
    assert cli._confirm_create_workspace(tmp_path, assume_yes=False) is True


def test_confirm_non_interactive_proceeds_without_prompt(tmp_path, monkeypatch):
    import frisket.cli as cli

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _p: (_ for _ in ()).throw(AssertionError("no prompt off a TTY")),
    )
    assert cli._confirm_create_workspace(tmp_path / "new", assume_yes=False) is True


def test_confirm_assume_yes_proceeds(tmp_path, monkeypatch):
    import frisket.cli as cli

    monkeypatch.setattr(
        "builtins.input",
        lambda _p: (_ for _ in ()).throw(AssertionError("--yes must not prompt")),
    )
    assert cli._confirm_create_workspace(tmp_path / "new", assume_yes=True) is True


def test_confirm_interactive_yes_and_no(tmp_path, monkeypatch):
    import frisket.cli as cli

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    target = tmp_path / "new"

    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert cli._confirm_create_workspace(target, assume_yes=False) is True

    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert cli._confirm_create_workspace(target, assume_yes=False) is False


# --- mistyped subcommand vs. workspace path (BUG 1) -------------------------


def test_unknown_subcommand_errors_without_creating_dir(tmp_path):
    proc = _run(["doctro"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unknown command 'doctro'" in proc.stderr
    assert not (tmp_path / "doctro").exists()
    assert list(tmp_path.iterdir()) == []


def test_unknown_subcommand_offers_did_you_mean(tmp_path):
    proc = _run(["doctro"], tmp_path)
    assert "did you mean 'doctor'?" in proc.stderr


def test_unknown_subcommand_with_no_close_match_still_errors(tmp_path):
    proc = _run(["zzzzzzzz"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unknown command 'zzzzzzzz'" in proc.stderr
    assert not (tmp_path / "zzzzzzzz").exists()
    assert list(tmp_path.iterdir()) == []


def test_looks_pathlike():
    import frisket.cli as cli

    assert cli._looks_pathlike("./doctro") is True
    assert cli._looks_pathlike("../doctro") is True
    assert cli._looks_pathlike("~/doctro") is True
    assert cli._looks_pathlike("my.frisket") is True
    assert cli._looks_pathlike("sub/dir") is True
    assert cli._looks_pathlike("doctro") is False


def test_existing_workspace_path_reaches_serve_not_unknown_command(
    tmp_path, monkeypatch
):
    """An existing directory that happens to collide with no subcommand name
    must still be treated as a workspace, not rejected as a typo."""
    import frisket.cli as cli
    import uvicorn

    workspace = tmp_path / "myws"
    workspace.mkdir()

    monkeypatch.setattr(sys, "argv", ["frisket", str(workspace)])
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    monkeypatch.setattr(cli, "_port_available", lambda host, port: True)

    def fake_run(*_args, **_kwargs):
        raise RuntimeError("reached uvicorn.run")

    monkeypatch.setattr(uvicorn, "run", fake_run)

    with pytest.raises(RuntimeError, match="reached uvicorn.run"):
        cli.main()


def test_pathlike_missing_workspace_reaches_confirm_prompt(tmp_path, monkeypatch):
    """A pathlike-but-not-yet-existing workspace must still reach the
    existing create-workspace confirmation, not the unknown-command guard."""
    import frisket.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["frisket", "./newws"])
    monkeypatch.setattr(cli, "_port_available", lambda host, port: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    prompted = {}

    def fake_input(prompt):
        prompted["called"] = True
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert prompted.get("called") is True
    assert not (tmp_path / "newws").exists()


# --- port bind pre-check before the startup banner (BUG 2) ------------------


def test_port_available_detects_free_and_busy_port():
    import frisket.cli as cli

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        assert cli._port_available("127.0.0.1", port) is False
    finally:
        blocker.close()
    assert cli._port_available("127.0.0.1", port) is True


def test_busy_port_errors_before_banner_and_creates_no_workspace(
    tmp_path, monkeypatch, capsys
):
    import frisket.cli as cli

    workspace = tmp_path / "newws"

    monkeypatch.setattr(sys, "argv", ["frisket", str(workspace), "8123"])
    monkeypatch.setattr(cli, "_port_available", lambda host, port: False)

    def boom(*_args, **_kwargs):
        raise AssertionError("banner must not print before a successful bind")

    monkeypatch.setattr(cli, "_print_startup_banner", boom)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2
    assert not workspace.exists()
    stderr = capsys.readouterr().err
    assert "port 8123 is already in use" in stderr
    assert "try a different port" in stderr

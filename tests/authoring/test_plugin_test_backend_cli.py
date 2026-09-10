"""`frisket plugin test-backend` runs one row through a plugin's Action.

An installed plugin Action is an ordinary Action on the same native hosts as
a builtin, so this command binds the request the way the host does and calls
the Action's own row handler in process. There is no subprocess and no
redaction boundary: this is author-local tooling running the author's own
code on the author's own machine, so the real traceback is what they get.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import run_cli as _cli


def _init_and_build(tmp_path: Path, plugin_id: str) -> Path:
    package_dir = tmp_path / plugin_id.replace(".", "_")
    init = _cli("plugin", "init", "--id", plugin_id, "--output", str(package_dir))
    assert init.returncode == 0, init.stderr
    build = _cli("plugin", "build", str(package_dir))
    assert build.returncode == 0, build.stderr
    return package_dir


def _write_action_plugin(tmp_path: Path, *, plugin_id: str, body: str) -> Path:
    """A minimal backend-only workspace declaring one typed Action."""
    package_dir = tmp_path / plugin_id.replace(".", "_")
    package_dir.mkdir(parents=True)
    package_dir.joinpath("plugin.py").write_text(body, encoding="utf-8")
    build = _cli("plugin", "build", str(package_dir))
    assert build.returncode == 0, build.stderr
    return package_dir


_BOOM_SOURCE = """from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowError, RowResult


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    out: str | None


def boom(params: Params, row: Row) -> RowResult[Output]:
    if params.source.read(row) == "refuse":
        raise RowError("not_usable", "That value is not usable.")
    raise ValueError("boom-error-marker")


BOOM = action(
    name="boom",
    title="Boom",
    description="Raises.",
    category=ActionCategory.TEXT,
    run=map_rows(boom),
)

plugin = Plugin_placeholder
"""


def _boom_source(plugin_id: str) -> str:
    return _BOOM_SOURCE.replace(
        "plugin = Plugin_placeholder",
        "from frisket.plugins.sdk import Plugin\n\n"
        f"plugin = Plugin(id={plugin_id!r}, version='0.1.0',\n"
        "    capabilities=['plugin:trusted_local_backend'], actions=(BOOM,))",
    )


def test_scaffolded_action_output_appears_in_stdout_json(tmp_path: Path) -> None:
    package_dir = _init_and_build(tmp_path, "test.backendecho")

    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps({"row": {"Name": "Ada"}, "params": {"source": "Name"}}),
        encoding="utf-8",
    )

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "echo",
        "--input",
        str(fixture),
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"output": {"plugin_echo": "ADA"}}


def test_inline_json_input_and_full_action_id_both_resolve(tmp_path: Path) -> None:
    plugin_id = "test.backendinline"
    package_dir = _init_and_build(tmp_path, plugin_id)

    inline_input = json.dumps({"row": {"Name": "Grace"}, "params": {"source": "Name"}})
    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        f"{plugin_id}.echo",
        "--input",
        inline_input,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["output"]["plugin_echo"] == "GRACE"


def test_raising_handler_exits_nonzero_and_shows_the_real_traceback(
    tmp_path: Path,
) -> None:
    """No subprocess means no redaction: the author sees their own exception,
    which is the whole reason to run a row this way."""
    plugin_id = "test.backendboom"
    package_dir = _write_action_plugin(
        tmp_path, plugin_id=plugin_id, body=_boom_source(plugin_id)
    )

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "boom",
        "--input",
        json.dumps({"row": {"Name": "x"}, "params": {"source": "Name"}}),
    )
    assert run.returncode != 0
    assert run.stdout == ""
    assert "boom-error-marker" in run.stderr
    assert "ValueError" in run.stderr


def test_row_error_reports_its_declared_code_and_message(tmp_path: Path) -> None:
    plugin_id = "test.backendrowerror"
    package_dir = _write_action_plugin(
        tmp_path, plugin_id=plugin_id, body=_boom_source(plugin_id)
    )

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "boom",
        "--input",
        json.dumps({"row": {"Name": "refuse"}, "params": {"source": "Name"}}),
    )
    assert run.returncode != 0
    assert run.stdout == ""
    assert "not_usable" in run.stderr
    assert "That value is not usable." in run.stderr


def test_unknown_action_names_the_ones_that_exist(tmp_path: Path) -> None:
    plugin_id = "test.backendunknown"
    package_dir = _init_and_build(tmp_path, plugin_id)

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "does-not-exist",
        "--input",
        json.dumps({"row": {}, "params": {}}),
    )
    assert run.returncode != 0
    assert "does-not-exist" in run.stderr
    assert f"{plugin_id}.echo" in run.stderr


def test_params_that_do_not_bind_are_refused_before_the_handler_runs(
    tmp_path: Path,
) -> None:
    package_dir = _init_and_build(tmp_path, "test.backendbadparams")

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "echo",
        "--input",
        json.dumps({"row": {"Name": "Ada"}, "params": {}}),
    )
    assert run.returncode != 0
    assert run.stdout == ""
    assert "do not bind" in run.stderr


def test_workspace_without_a_backend_module_says_so(tmp_path: Path) -> None:
    package_dir = tmp_path / "no_backend"
    package_dir.mkdir()

    run = _cli(
        "plugin",
        "test-backend",
        str(package_dir),
        "--action",
        "echo",
        "--input",
        json.dumps({"row": {}, "params": {}}),
    )
    assert run.returncode != 0
    assert "plugin.py" in run.stderr

from __future__ import annotations

from pathlib import Path

from frisket.contracts.plugin import load_plugin_manifest_file
from frisket.plugins.package_identity import build_plugin_package_identity


PLUGIN_ID = "demo.package_identity"

# A real backend module: it declares the package's Action (so plugin.json can
# be GENERATED exactly as `frisket plugin build` emits it, and plugin.py earns
# its ``backend:<kind>`` role) AND pulls a constant from a sibling module the
# manifest never names, which is what these tests are about.
_PLUGIN_SOURCE = """from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin

from .helper import TOKEN


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    result: str


def run_row(params: Params, row: Row) -> RowResult[Output]:
    return RowResult(output=Output(result=f"{TOKEN}:{params.source.read(row)}"))


ACT = action(
    name="stamp",
    title="Stamp package identity fixture",
    description="Writes a constant marker column derived from a helper module.",
    category=ActionCategory.TEXT,
    run=map_rows(run_row),
)

plugin = Plugin(
    id="demo.package_identity",
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(ACT,),
)
"""


def _write_package(root: Path) -> Path:
    from frisket.plugins.manifest_generate import (
        load_generation_source,
        manifest_dict_from_plugin,
        render_manifest_json,
    )

    root.mkdir()
    (root / "plugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    (root / "helper.py").write_text("TOKEN = 'one'\n", encoding="utf-8")
    manifest_path = root / "plugin.json"
    manifest_path.write_text(
        render_manifest_json(
            manifest_dict_from_plugin(load_generation_source(root)),
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_plugin_package_identity_tracks_helper_modules_not_named_in_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = _write_package(tmp_path / "plugin")
    loaded = load_plugin_manifest_file(manifest_path)
    first = build_plugin_package_identity(loaded, manifest_path)

    helper = tmp_path / "plugin" / "helper.py"
    helper.write_text("TOKEN = 'two'\n", encoding="utf-8")
    changed = build_plugin_package_identity(loaded, manifest_path)

    assert first.package_sha256 != changed.package_sha256
    assert {item.path: item.roles for item in changed.files}["helper.py"] == (
        "package_file",
    )


def test_plugin_package_identity_ignores_python_bytecode_cache(tmp_path: Path) -> None:
    manifest_path = _write_package(tmp_path / "plugin")
    loaded = load_plugin_manifest_file(manifest_path)
    first = build_plugin_package_identity(loaded, manifest_path)

    cache = tmp_path / "plugin" / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-313.pyc").write_bytes(b"bytecode")
    changed = build_plugin_package_identity(loaded, manifest_path)

    assert first.package_sha256 == changed.package_sha256


def test_plugin_package_identity_ignores_local_environment_noise(
    tmp_path: Path,
) -> None:
    manifest_path = _write_package(tmp_path / "plugin")
    loaded = load_plugin_manifest_file(manifest_path)
    first = build_plugin_package_identity(loaded, manifest_path)

    root = tmp_path / "plugin"
    (root / ".env").write_text("SECRET=local\n", encoding="utf-8")
    (root / ".vscode").mkdir()
    (root / ".vscode" / "settings.json").write_text("{}", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("home = local\n", encoding="utf-8")
    (root / "debug.log").write_text("local\n", encoding="utf-8")
    changed = build_plugin_package_identity(loaded, manifest_path)

    assert first.package_sha256 == changed.package_sha256

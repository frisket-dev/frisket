"""Python-owned typed plugin UI build: no second schema or frontend manifest."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from frisket.authoring import plugin_build


SOURCE = """from frisket.sdk import action, map_rows, ActionParams, ColumnRef, Row, RowResult, ActionCategory
from frisket.plugins.sdk import Plugin
from pydantic import BaseModel
class Params(ActionParams):
    source: ColumnRef[str]
    prefix: str = ""
class Output(BaseModel):
    text: str
def clean(params: Params, row: Row) -> RowResult[Output]:
    raise AssertionError("build must never call handlers")
definition = action(name="clean", title="Clean", description="Clean names", category=ActionCategory.TEXT,
    run=map_rows(clean), FORM)
plugin = Plugin(id="acme.names", version="1.0.0", capabilities=["plugin:trusted_local_backend"], actions=[definition])
"""


def _package(tmp_path, *, custom=True):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.py").write_text(
        SOURCE.replace("FORM", 'form="CleanUI"' if custom else 'form="generated"')
    )
    return root


@pytest.fixture
def frontend_build_tools():
    repo = Path(__file__).resolve().parents[2]
    required = (
        repo / "sdk/node_modules/.bin/esbuild",
        repo / "sdk/node_modules/.bin/tsc",
        repo / "web/node_modules/@types/react/index.d.ts",
    )
    if shutil.which("node") is None or not all(path.is_file() for path in required):
        pytest.skip("Custom UI tests require Node and installed sdk/web dependencies")
    return repo


def test_schema_only_typed_plugin_build_needs_no_node(tmp_path, monkeypatch):
    root = _package(tmp_path, custom=False)
    monkeypatch.setattr(plugin_build.shutil, "which", lambda name: None)
    assert plugin_build.plugin_build(root) == 0
    assert sorted(path.name for path in root.iterdir()) == ["plugin.json", "plugin.py"]


def test_typed_plugin_build_compiles_ui_and_generates_local_params(
    tmp_path, frontend_build_tools
):
    root = _package(tmp_path)
    (root / "frontend").mkdir()
    (root / "frontend/plugin.tsx").write_text("""
import type { GeneratedActionParams } from './generated/actionTypes';
import type { ActionUI, ActionUIHost } from '../.frisket-sdk/action-ui';
export function CleanUI({ React }: ActionUIHost) {
  return { fields: { prefix: ({value, onChange}) => <input value={value ?? ""} onChange={e => onChange(e.target.value)} /> },
    body: ({Field}) => <><Field name="source" /><Field name="prefix" /></>
  } satisfies ActionUI<GeneratedActionParams['acme.names.clean']>;
}
""")
    assert plugin_build.plugin_build(root) == 0
    manifest = json.loads((root / "plugin.json").read_text())
    assert manifest["runtime"]["workbench_components"] == [
        {
            "contribution_id": "acme.names.clean",
            "module_key": "acme.names.frontend",
            "component_key": "CleanUI",
            "module_path": "frontend/plugin.js",
        }
    ]
    assert (
        manifest["runtime"]["actions"][0]["catalog_entry"]["input_schema"][
            "properties"
        ]["prefix"]["default"]
        == ""
    )
    generated = (root / "frontend/generated/actionTypes.ts").read_text()
    assert '"acme.names.clean": ActionParams0' in generated
    # Exercise the generated cache and the shared vendored editor typing with
    # the real compiler, including a negative field-name/value witness.
    (root / "frontend/typecheck.ts").write_text("""
import type { GeneratedActionParams } from './generated/actionTypes';
import type { ActionUI } from '../.frisket-sdk/action-ui';
type Params = GeneratedActionParams['acme.names.clean'];
const good: Params = { source: 'Original', prefix: 'Dr ' };
// @ts-expect-error Python declares prefix as string, not number.
const bad: Params = { source: 'Original', prefix: 3 };
// @ts-expect-error Unknown fields cannot acquire custom controls.
const ui: ActionUI<Params> = { fields: { typo: () => null } };
""")
    repo = frontend_build_tools
    checked = subprocess.run(
        [
            str(repo / "sdk/node_modules/.bin/tsc"),
            "--strict",
            "--noEmit",
            "--jsx",
            "react",
            "--target",
            "ES2022",
            "--moduleResolution",
            "node",
            "--skipLibCheck",
            "--typeRoots",
            str(repo / "web/node_modules/@types"),
            str(root / "frontend/plugin.tsx"),
            str(root / "frontend/typecheck.ts"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert plugin_build.plugin_build(root) == 0
    assert before == {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "export",
    [
        "export default function CleanUI() { return {}; }",
        "export function Other() { return {}; }",
    ],
)
def test_typed_plugin_build_requires_exact_declared_export_and_rolls_back(
    tmp_path, export, frontend_build_tools
):
    root = _package(tmp_path)
    (root / "frontend").mkdir()
    (root / "frontend/plugin.ts").write_text(export)
    assert plugin_build.plugin_build(root) == 1
    assert not (root / "plugin.json").exists()
    assert not (root / "frontend/plugin.js").exists()
    assert not (root / "frontend/generated/actionTypes.ts").exists()

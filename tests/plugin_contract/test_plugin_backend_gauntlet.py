from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.plugins.manifest_generate import (
    load_generation_source,
    manifest_dict_from_plugin,
    render_manifest_json,
)
from frisket.server.app import create_app


pytestmark = pytest.mark.plugin_contract

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"

# The venv console script for [project.scripts] frisket, invoked directly
# rather than through `uv run`: env-manager chatter must never pollute the
# CLI's stdout/stderr (several tests parse build/validate stdout as JSON),
# and no implicit resync may mutate the env mid-test.
FRISKET_CLI = str(Path(sys.executable).with_name("frisket"))


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _plugin_id() -> str:
    return f"p{uuid.uuid4().hex[:10]}.x{uuid.uuid4().hex[:10]}"


def _catalog_kinds(client: TestClient, project_id: str) -> set[str]:
    response = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    return {str(entry["kind"]) for entry in response.json()["actions"]}


def _project_with_people(
    client: TestClient, *, name: str
) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "people.csv",
                "name,city\nAda,London\nGrace,Arlington\nLinus,Helsinki\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id)
    row_ids = [int(row["id"]) for row in data["rows"]]
    assert len(row_ids) == 3
    return project_id, sheet_id, row_ids


def _sheet_data(client: TestClient, project_id: str, sheet_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=50"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _columns_by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(column["name"]): column for column in data["columns"]}


def _cell_values(
    data: dict[str, Any],
    *,
    column_name: str,
) -> dict[int, Any]:
    columns = _columns_by_name(data)
    column_id = int(columns[column_name]["id"])
    return {int(row["id"]): row["cells"].get(str(column_id)) for row in data["rows"]}


def _write_row_plugin_package(
    root: Path,
    *,
    plugin_id: str,
    action_name: str,
    output_key: str,
    token: str,
    marker_path: Path,
) -> str:
    """Write a NATIVE single-Action plugin package; return its action id.

    An installed Action is an ordinary Action executed in process on the same
    native hosts a builtin runs on, so plugin.py DECLARES it with `action(...)`
    and plugin.json is generated from that declaration. The manifest binding
    never re-states the Action's title/scope/inputs/writes/params -- the
    declaration is the single authority and the generated `catalog_entry` is
    what the manifest carries.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(
        f'''from pathlib import Path

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    {output_key}: str


def echo(params: Params, row: Row) -> RowResult[Output]:
    Path({str(marker_path)!r}).write_text("executed", encoding="utf-8")
    return RowResult(
        output=Output(**{{{output_key!r}: f"{token}|{{params.source.read(row)}}"}})
    )


ACT = action(
    name={action_name!r},
    title="Contract echo",
    description="Generated contract plugin action.",
    category=ActionCategory.TEXT,
    run=map_rows(echo),
)

plugin = Plugin(
    id={plugin_id!r},
    version="0.1.0",
    capabilities=[{TRUSTED_LOCAL_BACKEND_CAPABILITY!r}],
    actions=(ACT,),
)
''',
        encoding="utf-8",
    )
    source = load_generation_source(root)
    assert source is not None
    (root / "plugin.json").write_text(
        render_manifest_json(manifest_dict_from_plugin(source)), encoding="utf-8"
    )
    # Registration namespaces the Action under the plugin id, so the id is
    # derived here rather than chosen by the caller.
    registered = source.actions[0]
    assert registered.action_id == f"{plugin_id}.{action_name}"
    return registered.action_id


def _install_activate_backend(
    client: TestClient,
    project_id: str,
    *,
    plugin_id: str,
    plugin_root: Path,
    action_id: str,
    permissions_accepted: list[str] | None = None,
) -> str:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])
    assert receipt_id

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": permissions_accepted
            if permissions_accepted is not None
            else [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredRuntimeBindings"]["actions"] == [action_id]
    return receipt_id


def _run_action(
    client: TestClient,
    project_id: str,
    *,
    action_id: str,
    sheet_id: int,
    key: str,
    source_column: str = "name",
    output_names: dict[str, str] | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """POST the canonical typed action request.

    Selection is the request SCOPE, never a parameter, and output naming and
    replacement are request-level concerns rather than plugin-declared ones.
    """
    body: dict[str, Any] = {
        "action_id": action_id,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": source_column},
        "idempotency_key": key,
    }
    if output_names is not None:
        body["output_names"] = output_names
    if replace_existing:
        body["replace_existing"] = True
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    try:
        result = response.json()
    except json.JSONDecodeError:
        pytest.fail(
            f"action response was not JSON: {response.status_code} {response.text}"
        )
    assert isinstance(result, dict), response.text
    result["_http_status"] = response.status_code
    result["_response_text"] = response.text
    return result


def test_loaded_plugin_code_is_immutable_until_reload(tmp_path: Path) -> None:
    """The bytes ADMITTED by plugin.load are the only bytes dispatch may run.

    Installation is a trusted operator act, not a sandbox boundary, so the
    thing that must hold is identity: an installed Action executes the package
    the operator inspected and accepted. A package edited on disk afterwards is
    refused until an operator reloads it, and the reload is what makes the new
    code live.
    """
    client = _client(tmp_path)
    suffix = _suffix()
    plugin_id = _plugin_id()
    action_name = _identifier("echo")
    output_key = _identifier("integrity_stamp")
    original_token = _identifier("original_token")
    tampered_token = _identifier("tampered_token")
    marker_path = tmp_path / f"{suffix}.integrity.executed"
    plugin_root = tmp_path / "packages" / plugin_id
    action_id = _write_row_plugin_package(
        plugin_root,
        plugin_id=plugin_id,
        action_name=action_name,
        output_key=output_key,
        token=original_token,
        marker_path=marker_path,
    )
    project_id, sheet_id, row_ids = _project_with_people(
        client, name="plugin contract integrity"
    )
    _install_activate_backend(
        client,
        project_id,
        plugin_id=plugin_id,
        plugin_root=plugin_root,
        action_id=action_id,
    )

    admitted = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"integrity-admitted@sha256:{suffix}",
    )
    assert admitted["status"] == "completed", admitted["_response_text"]
    assert marker_path.exists()
    admitted_values = _cell_values(
        _sheet_data(client, project_id, sheet_id), column_name=output_key
    )
    assert admitted_values == {
        row_ids[0]: f"{original_token}|Ada",
        row_ids[1]: f"{original_token}|Grace",
        row_ids[2]: f"{original_token}|Linus",
    }
    marker_path.unlink()

    _write_row_plugin_package(
        plugin_root,
        plugin_id=plugin_id,
        action_name=action_name,
        output_key=output_key,
        token=tampered_token,
        marker_path=marker_path,
    )

    # replace_existing is granted so the ONLY thing that can refuse this run is
    # the drifted package, not the output column it already owns.
    drifted = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"integrity-drift@sha256:{suffix}",
        replace_existing=True,
    )
    assert not marker_path.exists()
    assert drifted["_http_status"] == 409
    assert drifted["status"] == "failed"
    refusal = drifted["errors"][0]
    assert refusal["code"] == "plugin_code_integrity_mismatch"
    assert "plugin source changed after plugin.load" in refusal["message"]
    assert refusal["field"] is None
    assert refusal["details"]["module_path"] == "plugin.py"
    assert refusal["details"]["expected_sha256"]
    assert refusal["details"]["current_sha256"]
    assert refusal["details"]["current_sha256"] != refusal["details"]["expected_sha256"]
    assert tampered_token not in drifted["_response_text"]
    assert (
        _cell_values(_sheet_data(client, project_id, sheet_id), column_name=output_key)
        == admitted_values
    )

    # ...until reload: re-running the public install/activate lifecycle admits
    # the new bytes, and only then do they execute.
    _install_activate_backend(
        client,
        project_id,
        plugin_id=plugin_id,
        plugin_root=plugin_root,
        action_id=action_id,
    )
    reloaded = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"integrity-reload@sha256:{suffix}",
        replace_existing=True,
    )
    assert reloaded["status"] == "completed", reloaded["_response_text"]
    assert marker_path.exists()
    assert _cell_values(
        _sheet_data(client, project_id, sheet_id), column_name=output_key
    ) == {
        row_ids[0]: f"{tampered_token}|Ada",
        row_ids[1]: f"{tampered_token}|Grace",
        row_ids[2]: f"{tampered_token}|Linus",
    }


def test_dispatch_requires_manifest_capability_and_project_enablement(
    tmp_path: Path,
) -> None:
    """Two independent admission gates, each proved to fail closed.

    A workspace-registered package does NOT make its Action dispatchable: the
    Action reaches a project's catalog only where that project enabled the
    plugin, and a project may enable it only by accepting the capabilities its
    manifest requires. Neither gate is a request-level claim -- the retired
    envelope let a caller assert its own capability list; the typed request has
    no such field, so the grant recorded at activation is the only authority.
    """
    client = _client(tmp_path)
    suffix = _suffix()
    plugin_id = _plugin_id()
    output_key = _identifier("dispatch_stamp")
    dispatch_token = _identifier("dispatch_token")
    marker_path = tmp_path / f"{suffix}.dispatch.executed"
    plugin_root = tmp_path / "packages" / plugin_id
    action_id = _write_row_plugin_package(
        plugin_root,
        plugin_id=plugin_id,
        action_name=_identifier("echo"),
        output_key=output_key,
        token=dispatch_token,
        marker_path=marker_path,
    )
    project_a, sheet_a, _rows_a = _project_with_people(
        client, name="plugin contract dispatch A"
    )
    project_b, sheet_b, _rows_b = _project_with_people(
        client, name="plugin contract dispatch B"
    )
    _install_activate_backend(
        client,
        project_a,
        plugin_id=plugin_id,
        plugin_root=plugin_root,
        action_id=action_id,
    )
    assert action_id in _catalog_kinds(client, project_a)
    assert action_id not in _catalog_kinds(client, project_b)

    # (1) Project enablement. The package is registered workspace-wide, but
    #     project B never enabled it, so the id resolves to nothing there.
    wrong_project = _run_action(
        client,
        project_b,
        action_id=action_id,
        sheet_id=sheet_b,
        key=f"wrong-project@sha256:{suffix}",
    )
    assert not marker_path.exists()
    assert wrong_project["status"] == "failed"
    assert wrong_project["errors"][0]["code"] == "invalid_action_request"
    assert wrong_project["errors"][0]["action_kind"] == action_id
    assert dispatch_token not in wrong_project["_response_text"]
    data_b = _sheet_data(client, project_b, sheet_b)
    assert output_key not in _columns_by_name(data_b)

    # (2) Manifest capability. Project C installs the same package but declines
    #     the capability its manifest requires: activation refuses, backend
    #     activation has nothing to enable, and the Action never becomes
    #     dispatchable there.
    project_c, sheet_c, _rows_c = _project_with_people(
        client, name="plugin contract dispatch C"
    )
    installed = client.post(
        f"/api/projects/{project_c}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    ungranted = client.post(
        f"/api/projects/{project_c}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": str(installed.json()["receiptId"]),
            "trustAcknowledged": True,
            "permissionsAccepted": [],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert ungranted.status_code == 403, ungranted.text
    assert ungranted.json()["detail"]["code"] == "plugin_activation_permissions_missing"
    backend = client.post(
        f"/api/projects/{project_c}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 409, backend.text
    assert backend.json()["detail"]["code"] == "plugin_backend_activation_not_enabled"
    assert action_id not in _catalog_kinds(client, project_c)

    missing_capability = _run_action(
        client,
        project_c,
        action_id=action_id,
        sheet_id=sheet_c,
        key=f"missing-capability@sha256:{suffix}",
    )
    assert not marker_path.exists()
    assert missing_capability["status"] == "failed"
    assert missing_capability["errors"][0]["code"] == "invalid_action_request"
    assert missing_capability["errors"][0]["action_kind"] == action_id
    assert dispatch_token not in missing_capability["_response_text"]
    data_c = _sheet_data(client, project_c, sheet_c)
    assert output_key not in _columns_by_name(data_c)


def _project_with_people_and_extra_column(
    client: TestClient,
    *,
    name: str,
    column_name: str,
    values: tuple[str, str] = ("keep-me-one", "keep-me-two"),
) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "people.csv",
                f"name,city,{column_name}\n"
                f"Ada,London,{values[0]}\n"
                f"Grace,Arlington,{values[1]}\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id)
    row_ids = [int(row["id"]) for row in data["rows"]]
    assert len(row_ids) == 2
    return project_id, sheet_id, row_ids


def test_plugin_action_writes_fail_closed_on_source_column_collision(
    tmp_path: Path,
) -> None:
    """An installed Action whose output names an existing SOURCE column
    (``ai_generated=False``) is refused by the shared typed map-rows output
    plan before the handler is invoked, rather than silently overwriting human
    data. Running in process on the native host does not soften that: the
    plugin gets exactly the builtin refusal. Post-cutover there is no plugin
    escape hatch either -- ``replace_existing`` covers generation-managed
    output columns only, so the source column stays refused -- while the same
    Action publishes normally into a name it does not already own.
    """
    client = _client(tmp_path)
    suffix = _suffix()
    plugin_id = _plugin_id()
    output_key = _identifier("collision_target")
    token = _identifier("token")
    marker_path = tmp_path / f"{suffix}.collision.executed"
    plugin_root = tmp_path / "packages" / plugin_id
    action_id = _write_row_plugin_package(
        plugin_root,
        plugin_id=plugin_id,
        action_name=_identifier("echo"),
        output_key=output_key,
        token=token,
        marker_path=marker_path,
    )
    project_id, sheet_id, row_ids = _project_with_people_and_extra_column(
        client, name="plugin contract write collision", column_name=output_key
    )
    _install_activate_backend(
        client,
        project_id,
        plugin_id=plugin_id,
        plugin_root=plugin_root,
        action_id=action_id,
    )
    before = _sheet_data(client, project_id, sheet_id)
    before_column = _columns_by_name(before)[output_key]
    assert before_column["ai_generated"] is False
    preserved = {row_ids[0]: "keep-me-one", row_ids[1]: "keep-me-two"}
    assert _cell_values(before, column_name=output_key) == preserved

    refused = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"collision-no-overwrite@sha256:{suffix}",
    )
    assert not marker_path.exists()
    assert refused["status"] == "failed"
    assert refused["errors"][0]["code"] == "output_column_exists"
    assert refused["errors"][0]["details"]["columns"] == [output_key]
    assert token not in refused["_response_text"]
    after_refusal = _sheet_data(client, project_id, sheet_id)
    after_refusal_column = _columns_by_name(after_refusal)[output_key]
    assert after_refusal_column["id"] == before_column["id"]
    assert after_refusal_column["ai_generated"] is False
    assert _cell_values(after_refusal, column_name=output_key) == preserved

    # replace_existing is not an override for human data: replacement is
    # reserved for columns a generation owns.
    replaced = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"collision-with-replace@sha256:{suffix}",
        replace_existing=True,
    )
    assert not marker_path.exists()
    assert replaced["status"] == "failed"
    assert replaced["errors"][0]["code"] == "output_column_exists"
    assert "only generated columns can be replaced" in replaced["errors"][0]["message"]
    assert replaced["errors"][0]["details"]["columns"] == [output_key]
    after_replace = _sheet_data(client, project_id, sheet_id)
    after_replace_column = _columns_by_name(after_replace)[output_key]
    assert after_replace_column["id"] == before_column["id"]
    assert after_replace_column["ai_generated"] is False
    assert _cell_values(after_replace, column_name=output_key) == preserved

    # The refusal is about the COLLISION, not the Action: renaming the output
    # at request time publishes normally and still leaves the source column
    # exactly as the import wrote it.
    fresh_name = _identifier("fresh_output")
    published = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"collision-renamed@sha256:{suffix}",
        output_names={output_key: fresh_name},
    )
    assert published["status"] == "completed", published["_response_text"]
    assert marker_path.exists()
    after_publish = _sheet_data(client, project_id, sheet_id)
    assert _columns_by_name(after_publish)[fresh_name]["ai_generated"] is True
    assert _cell_values(after_publish, column_name=fresh_name) == {
        row_ids[0]: f"{token}|Ada",
        row_ids[1]: f"{token}|Grace",
    }
    assert _columns_by_name(after_publish)[output_key]["ai_generated"] is False
    assert _cell_values(after_publish, column_name=output_key) == preserved


def test_plugin_author_cli_init_validate_creates_runnable_package(
    tmp_path: Path,
) -> None:
    plugin_id = _plugin_id()
    plugin_root = tmp_path / "author-package"

    init = subprocess.run(
        [
            FRISKET_CLI,
            "plugin",
            "init",
            "--id",
            plugin_id,
            "--output",
            str(plugin_root),
            "--with",
            "view",
            "--with",
            "panel",
        ],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert init.returncode == 0, init.stderr
    # Init emits the SDK-shaped SOURCE only (plugin-sdk-build-pipeline-v1);
    # the loader artifacts come from `frisket plugin build`.
    for relative in (
        "plugin.config.mjs",
        ".frisket-sdk/index.mjs",
        "frontend/plugin.js",
        "README.md",
    ):
        assert (plugin_root / relative).is_file(), relative
    assert not (plugin_root / "plugin.json").exists()
    assert not (plugin_root / "workbench-descriptors.json").exists()

    build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    for relative in ("plugin.json", "workbench-descriptors.json"):
        assert (plugin_root / relative).is_file(), relative

    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(plugin_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == plugin_id
    assert str(validation["manifest_sha256"]).startswith("sha256:")
    assert str(validation["package_sha256"]).startswith("sha256:")

    for path in plugin_root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "tests/fixtures/local_plugins" not in text
            assert "/Users/" not in text

    plugin_config = plugin_root / "plugin.config.mjs"
    frontend_js = plugin_root / "frontend" / "plugin.js"
    plugin_config.write_text(
        plugin_config.read_text(encoding="utf-8") + "\n// package digest probe\n",
        encoding="utf-8",
    )
    validate_after_py = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(plugin_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate_after_py.returncode == 0, validate_after_py.stderr
    validation_after_py = json.loads(validate_after_py.stdout)
    assert validation_after_py["manifest_sha256"] == validation["manifest_sha256"]
    assert validation_after_py["package_sha256"] != validation["package_sha256"]

    frontend_js.write_text(
        frontend_js.read_text(encoding="utf-8") + "\n// package digest probe\n",
        encoding="utf-8",
    )
    validate_after_frontend = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(plugin_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate_after_frontend.returncode == 0, validate_after_frontend.stderr
    validation_after_frontend = json.loads(validate_after_frontend.stdout)
    assert validation_after_frontend["manifest_sha256"] == validation["manifest_sha256"]
    assert (
        validation_after_frontend["package_sha256"]
        != validation_after_py["package_sha256"]
    )

    readme = (plugin_root / "README.md").read_text(encoding="utf-8")
    assert "frisket plugin build" in readme
    assert "install-local" in readme
    assert "backend/activate" in readme
    assert "actions/v1/run" in readme
    assert "reload" in readme.lower()

    # The SDK-shaped workspace contributes workbench surfaces, not Actions:
    # an Action is declared in plugin.py and generated straight into
    # runtime.actions[] (the backend-only path exercised by
    # test_plugin_sdk_init_build_validate_roundtrip). Running an installed
    # Action end to end is covered by
    # tests/authoring/test_native_installed_actions.py.
    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["runtime"]["actions"] == []
    assert manifest["contributes"]["actions"] == []
    assert manifest["contributes"]["workbench_views"] == [f"{plugin_id}.view.main"]
    assert manifest["contributes"]["workbench_panels"] == [
        f"{plugin_id}.panel.inspector"
    ]


def test_bottom_dock_tab_placement_round_trips_resolution_and_validate() -> None:
    """Cohort 1 (plugin-bottomdock-tab-parity-v1): a plugin panel placed in
    bottomDock:tab passes author-time validation and resolves through the
    backend reference resolver with its dock placement active."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_dock_tab"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.dock_tab"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.dock_tab"},
        trusted_plugins={"demo.dock_tab"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    contribution = resolved["demo.dock_tab.panel.dock"]
    assert contribution.status == "enabled"
    dock_placements = [
        placement
        for placement in contribution.legal_placements
        if placement.get("host") == "bottomDock" and placement.get("mode") == "tab"
    ]
    assert dock_placements, contribution.legal_placements
    assert dock_placements[0].get("placementId") == "demo-dock-tab"
    assert contribution.active_placement is not None
    assert contribution.active_placement.get("host") == "bottomDock"

    hidden = resolve_contributions(
        descriptors,
        installed_plugins={"demo.dock_tab"},
        trusted_plugins={"demo.dock_tab"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions={"demo.dock_tab.panel.dock"},
    )
    assert hidden["demo.dock_tab.panel.dock"].status == "hidden"


def test_plugin_command_contribution_round_trips_validation_and_resolution() -> None:
    """Cohort 2 (plugin-commandpalette-parity-v1): a declared workbench command
    passes author-time validation, loads as a package, and resolves through the
    backend reference resolver with its commandPalette:command placement; an
    undeclared command descriptor fails the package validation closed."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_command"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.command"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.command"},
        trusted_plugins={"demo.command"},
        host_capabilities=set(),
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    contribution = resolved["demo.command.command.hello"]
    assert contribution.status == "enabled"
    assert contribution.active_placement is not None
    assert contribution.active_placement.get("host") == "commandPalette"
    assert contribution.active_placement.get("mode") == "command"

    from frisket.contracts.plugin import PluginManifest
    from frisket.plugins.load_evidence import (
        WorkbenchDescriptorPackageLoadError,
        _validate_workbench_descriptor_package_for_manifest,
    )
    from frisket.authoring.workbench.contracts import LoadedWorkbenchDescriptorPackage

    manifest_data = json.loads(
        (fixture_root / "plugin.json").read_text(encoding="utf-8")
    )
    manifest_data["contributes"]["workbench_commands"] = []
    manifest_data["contributes"]["workbench_panels"] = ["demo.command.panel.filler"]
    manifest = PluginManifest.model_validate(manifest_data)

    class _Loaded:
        pass

    loaded = _Loaded()
    loaded.manifest = manifest
    package = LoadedWorkbenchDescriptorPackage(
        schema_version="frisket.workbench_descriptor_package.v1",
        sha256="sha256:test",
        byte_count=1,
        descriptor_manifests=descriptors,
        runtime_only_fields_stripped=[],
    )
    with pytest.raises(WorkbenchDescriptorPackageLoadError):
        _validate_workbench_descriptor_package_for_manifest(loaded, package)


def test_left_sidebar_panel_placement_round_trips_resolution_and_validate() -> None:
    """Cohort 3 (plugin-leftsidebar-panel-parity-v1): a plugin panel placed in
    leftSidebar:panel passes author-time validation and resolves through the
    backend reference resolver with its sidebar placement active."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_sidebar_panel"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.sidebar_panel"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.sidebar_panel"},
        trusted_plugins={"demo.sidebar_panel"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    contribution = resolved["demo.sidebar_panel.panel.scope"]
    assert contribution.status == "enabled"
    assert contribution.active_placement is not None
    assert contribution.active_placement.get("host") == "leftSidebar"
    assert contribution.active_placement.get("mode") == "panel"

    hidden = resolve_contributions(
        descriptors,
        installed_plugins={"demo.sidebar_panel"},
        trusted_plugins={"demo.sidebar_panel"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions={"demo.sidebar_panel.panel.scope"},
    )
    assert hidden["demo.sidebar_panel.panel.scope"].status == "hidden"


def test_detail_host_placements_round_trip_resolution_and_validate() -> None:
    """Cohort 4 (plugin-rowdetail/columndetail/entitysource-detail-parity-v1):
    plugin panels placed in the detail hosts pass author-time validation and
    resolve with their declared detail placements active."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_detail_panels"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.detail_panels"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.detail_panels"},
        trusted_plugins={"demo.detail_panels"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    expected_placements = {
        "demo.detail_panels.panel.row": ("rowDetail", "tab"),
        "demo.detail_panels.panel.column": ("columnInspector", "section"),
        "demo.detail_panels.panel.column_tab": ("columnDetail", "tab"),
        "demo.detail_panels.panel.entity": ("entityDetail", "tab"),
        "demo.detail_panels.panel.source": ("sourceDetail", "tab"),
    }
    for contribution_id, (host, mode) in expected_placements.items():
        contribution = resolved[contribution_id]
        assert contribution.status == "enabled", contribution_id
        assert contribution.active_placement is not None, contribution_id
        assert contribution.active_placement.get("host") == host, contribution_id
        assert contribution.active_placement.get("mode") == mode, contribution_id


def test_modal_or_peek_placement_round_trips_resolution_and_validate() -> None:
    """Cohort 5 (plugin-modalorpeek-peek-parity-v1): a plugin peek panel and its
    opener command pass author-time validation and resolve with their declared
    placements active."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_peek_panel"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.peek_panel"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.peek_panel"},
        trusted_plugins={"demo.peek_panel"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    peek = resolved["demo.peek_panel.panel.peek"]
    assert peek.status == "enabled"
    assert peek.active_placement is not None
    assert peek.active_placement.get("host") == "modalOrPeek"
    assert peek.active_placement.get("mode") == "peek"
    command = resolved["demo.peek_panel.command.open_peek"]
    assert command.status == "enabled"
    assert command.active_placement is not None
    assert command.active_placement.get("host") == "commandPalette"


def test_activity_rail_launcher_placement_round_trips_resolution_and_validate() -> None:
    """Cohort 6 (plugin-activityrail-launcher-parity-v1): a plugin panel with a
    bottomDock primary and an activityRail launcher secondary validates and
    resolves BOTH placements (multi-placement from Cohort 0)."""
    from frisket.authoring.workbench.contracts import resolve_contributions

    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_rail_launcher"
    )
    validate = subprocess.run(
        [FRISKET_CLI, "plugin", "validate", str(fixture_root)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.rail_launcher"

    descriptors = json.loads(
        (fixture_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]
    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"demo.rail_launcher"},
        trusted_plugins={"demo.rail_launcher"},
        host_capabilities={"sheet.active"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    contribution = resolved["demo.rail_launcher.panel.dock"]
    assert contribution.status == "enabled"
    placements = {
        (placement.get("host"), placement.get("mode"))
        for placement in contribution.legal_placements
    }
    assert ("bottomDock", "tab") in placements
    assert ("activityRail", "command") in placements


def test_plugin_sdk_init_build_validate_roundtrip(tmp_path: Path) -> None:
    """plugin-sdk-build-pipeline-v1: init emits SDK source; build emits and
    validates the loader artifacts; the emitted shapes resolve like any other
    trusted package."""
    plugin_id = _plugin_id()
    plugin_root = tmp_path / "sdk-package"
    repo_root = Path(__file__).resolve().parents[2]

    init = subprocess.run(
        [
            FRISKET_CLI,
            "plugin",
            "init",
            "--id",
            plugin_id,
            "--output",
            str(plugin_root),
            "--with",
            "view",
            "--with",
            "panel",
            "--with",
            "command",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert init.returncode == 0, init.stderr
    assert (plugin_root / "plugin.config.mjs").is_file()
    assert not (plugin_root / "plugin.json").exists()

    build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    built = json.loads(build.stdout)
    assert built["valid"] is True
    assert built["plugin_id"] == plugin_id

    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["contributes"]["workbench_views"] == [f"{plugin_id}.view.main"]
    assert manifest["contributes"]["workbench_panels"] == [
        f"{plugin_id}.panel.inspector"
    ]
    assert manifest["contributes"]["workbench_commands"] == [
        f"{plugin_id}.command.hello"
    ]
    descriptors = json.loads(
        (plugin_root / "workbench-descriptors.json").read_text(encoding="utf-8")
    )["descriptors"]

    from frisket.authoring.workbench.contracts import resolve_contributions

    resolved = resolve_contributions(
        descriptors,
        installed_plugins={plugin_id},
        trusted_plugins={plugin_id},
        host_capabilities={"sheet.active", "sheet.rows.read", "selection.rows"},
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(),
    )
    assert resolved[f"{plugin_id}.view.main"].status == "enabled"
    assert resolved[f"{plugin_id}.panel.inspector"].status == "enabled"
    assert resolved[f"{plugin_id}.command.hello"].status == "enabled"

    # The DEFAULT init shape is the backend-only Action workspace: plugin.py
    # alone, generating a manifest with no workbench descriptors at all
    # (review F1: an empty descriptor package is illegal, so build emits NONE).
    action_only_id = _plugin_id()
    action_only_root = tmp_path / "sdk-action-only"
    init_action_only = subprocess.run(
        [
            FRISKET_CLI,
            "plugin",
            "init",
            "--id",
            action_only_id,
            "--output",
            str(action_only_root),
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert init_action_only.returncode == 0, init_action_only.stderr
    build_action_only = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(action_only_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build_action_only.returncode == 0, build_action_only.stderr
    assert json.loads(build_action_only.stdout)["valid"] is True
    assert not (action_only_root / "workbench-descriptors.json").exists()
    assert not (action_only_root / "plugin.config.mjs").exists()
    action_only_manifest = json.loads(
        (action_only_root / "plugin.json").read_text(encoding="utf-8")
    )
    assert action_only_manifest["contributes"]["actions"] == [f"{action_only_id}.echo"]
    assert [
        item["handler_api"] for item in action_only_manifest["runtime"]["actions"]
    ] == ["typed_action"]


def test_reserved_plugin_ids_rejected_at_validate_and_install(tmp_path: Path) -> None:
    """IDs whose namespaces collide with first-party descriptors fail manifest
    load —
    the single gate behind validate, install-local, and serve — while the
    sanctioned frisket.geo id stays legal."""
    import shutil as _shutil

    from frisket.contracts.plugin import (
        PluginManifestLoadError,
        load_plugin_manifest_file,
    )

    # The template is a real, VALID native package -- proved valid below before
    # anything is mutated -- so re-namespacing it onto a reserved id leaves the
    # id as the single reason the manifest can be refused. (A template that was
    # invalid for some other reason would let this test pass on the wrong
    # error.)
    template_root = tmp_path / "reserved-id-template"
    template_plugin_id = _plugin_id()
    _write_row_plugin_package(
        template_root,
        plugin_id=template_plugin_id,
        action_name="probe",
        output_key="probe_stamp",
        token="reserved-id-template",
        marker_path=tmp_path / "reserved-id-template.never-executed",
    )
    template = load_plugin_manifest_file(template_root / "plugin.json")
    assert template.manifest.id == template_plugin_id
    source_text = (template_root / "plugin.json").read_text(encoding="utf-8")

    repo_root = Path(__file__).resolve().parents[2]
    for reserved in ("frisket", "frisket.core"):
        package_root = tmp_path / reserved.replace(".", "_")
        _shutil.copytree(template_root, package_root)
        manifest = json.loads(source_text.replace(template_plugin_id, reserved))
        assert manifest["id"] == reserved
        (package_root / "plugin.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        validate = subprocess.run(
            [FRISKET_CLI, "plugin", "validate", str(package_root)],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert validate.returncode != 0, reserved
        assert "reserved" in (validate.stdout + validate.stderr), reserved

        try:
            load_plugin_manifest_file(package_root / "plugin.json")
            raise AssertionError(f"install-path manifest load accepted {reserved!r}")
        except PluginManifestLoadError as error:
            assert "reserved" in str(error)

    geo_fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "frisket_geo_smoke"
    )
    geo = load_plugin_manifest_file(geo_fixture / "plugin.json")
    assert geo.manifest.id == "frisket.geosmoke"

    # Namespacing hygiene (plugin-authoring-docs-v1 review F1): contribution
    # ids outside the plugin namespace, and commandId != contribution id,
    # fail validate.
    peek_fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "local_plugins"
        / "demo_peek_panel"
    )
    for mutation, expected_message in (
        (
            lambda desc: desc.__setitem__("id", "other.vendor.panel.spoof"),
            "namespaced under the plugin id",
        ),
        (
            lambda desc: desc.__setitem__("commandId", "demo.peek_panel.command.other"),
            "commandId equal to the contribution id",
        ),
    ):
        package_root = tmp_path / f"ns-{expected_message.split()[0]}"
        if package_root.exists():
            _shutil.rmtree(package_root)
        _shutil.copytree(peek_fixture, package_root)
        descriptor_file = package_root / "workbench-descriptors.json"
        package = json.loads(descriptor_file.read_text(encoding="utf-8"))
        target = (
            package["descriptors"][1]
            if "commandId" in expected_message
            else package["descriptors"][0]
        )
        mutation(target)
        if target.get("kind") != "command" and "namespaced" in expected_message:
            # keep the manifest declaring the mutated id so ONLY the
            # namespacing rule trips (not the undeclared-id rule)
            manifest = json.loads(
                (package_root / "plugin.json").read_text(encoding="utf-8")
            )
            manifest["contributes"]["workbench_panels"] = [target["id"]]
            (package_root / "plugin.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        descriptor_file.write_text(
            json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        validate = subprocess.run(
            [FRISKET_CLI, "plugin", "validate", str(package_root)],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert validate.returncode != 0, expected_message
        assert expected_message in (validate.stdout + validate.stderr)


def test_plugin_build_fails_when_component_binding_names_a_missing_export(
    tmp_path: Path,
) -> None:
    """plugin-build-export-check-v1 (additive gauntlet revision): a
    workbench_components binding whose component_key names no export in the
    emitted frontend module fails `frisket plugin build` with an actionable
    message — the missing component_key and the exports actually found — and
    the failed build restores the prior plugin.json/workbench-descriptors.json
    byte-identically instead of leaving a manifest that promises a component
    the module doesn't export."""
    plugin_id = _plugin_id()
    plugin_root = tmp_path / "export-check-package"
    repo_root = Path(__file__).resolve().parents[2]

    init = subprocess.run(
        [
            FRISKET_CLI,
            "plugin",
            "init",
            "--id",
            plugin_id,
            "--output",
            str(plugin_root),
            "--with",
            "view",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert init.returncode == 0, init.stderr

    build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    manifest_snapshot = (plugin_root / "plugin.json").read_bytes()

    frontend_path = plugin_root / "frontend" / "plugin.js"
    source = frontend_path.read_text(encoding="utf-8")
    assert "export const MainView" in source
    frontend_path.write_text(
        source.replace("export const MainView", "export const MainViewRenamed"),
        encoding="utf-8",
    )

    failed_build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert failed_build.returncode != 0
    assert f"{plugin_id}.components.MainView" in failed_build.stderr
    assert "MainViewRenamed" in failed_build.stderr
    assert "exports found:" in failed_build.stderr
    assert (plugin_root / "plugin.json").read_bytes() == manifest_snapshot


def test_plugin_build_fails_on_bare_npm_import_in_frontend_entry(
    tmp_path: Path,
) -> None:
    """plugin-build-bare-specifier-rejection-v1 (additive gauntlet revision):
    a plain frontend/plugin.js entry (the scaffold's default, no separate
    compile step) that imports a bare npm specifier used to build green,
    because the only thing that imported the module during build was node's
    export-check runner, and node happily resolves node_modules — but the
    browser's plugin loader (web/src/workbench/trustedLocalModule.ts) has no
    import map and cannot resolve it: green build, dead plugin at runtime.
    `frisket plugin build` now scans the EMITTED module (not the source)
    with esbuild's own parser and rejects any non-relative/absolute/URL
    specifier, naming it and directing the author to a compiled
    frontend/plugin.ts(x) entry. The stub `node_modules/left-pad` package
    below is real and node-resolvable on purpose — proving the rejection
    does not depend on node-resolvability, which is exactly the false-green
    this check exists to kill."""
    plugin_id = _plugin_id()
    plugin_root = tmp_path / "bare-specifier-package"
    repo_root = Path(__file__).resolve().parents[2]

    init = subprocess.run(
        [
            FRISKET_CLI,
            "plugin",
            "init",
            "--id",
            plugin_id,
            "--output",
            str(plugin_root),
            "--with",
            "view",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert init.returncode == 0, init.stderr

    build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    manifest_snapshot = (plugin_root / "plugin.json").read_bytes()

    stub_package = plugin_root / "node_modules" / "left-pad"
    stub_package.mkdir(parents=True)
    (stub_package / "package.json").write_text(
        '{"name": "left-pad", "version": "1.0.0", "main": "index.js"}\n',
        encoding="utf-8",
    )
    (stub_package / "index.js").write_text(
        "module.exports = function leftPad(str) { return str; };\n",
        encoding="utf-8",
    )

    frontend_path = plugin_root / "frontend" / "plugin.js"
    source = frontend_path.read_text(encoding="utf-8")
    frontend_path.write_text(
        "import leftPad from 'left-pad';\n" + source, encoding="utf-8"
    )

    failed_build = subprocess.run(
        [FRISKET_CLI, "plugin", "build", str(plugin_root)],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert failed_build.returncode != 0
    assert "left-pad" in failed_build.stderr
    assert "plugin.ts" in failed_build.stderr
    assert (plugin_root / "plugin.json").read_bytes() == manifest_snapshot


def test_bundled_plugin_installs_through_public_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin-bundled-install-path-v1: a bundled plugin is a resolution
    shorthand into the SAME public plan/execute/install/load/activate
    functions trusted-local install uses -- no bespoke route, no registry
    backdoor. Proves the full lifecycle end to end: install (source kind
    'bundled') -> receipts + integrity hashes -> activate -> backend
    activate -> real dispatch through the normal action-run path -> runtime
    index shows source.kind == 'bundled'."""
    from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status

    client = _client(tmp_path)
    suffix = _suffix()
    plugin_id = _plugin_id()
    output_key = _identifier("bundled_stamp")
    dispatch_token = _identifier("bundled_token")
    marker_path = tmp_path / f"{suffix}.bundled.executed"

    bundled_root = tmp_path / "bundled_root"
    package_root = bundled_root / plugin_id
    action_id = _write_row_plugin_package(
        package_root,
        plugin_id=plugin_id,
        action_name=_identifier("bundled"),
        output_key=output_key,
        token=dispatch_token,
        marker_path=marker_path,
    )
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled_root)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
    )

    project_id, sheet_id, _row_ids = _project_with_people(
        client, name="bundled install lifecycle"
    )

    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "bundled", "value": plugin_id},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    install_payload = installed.json()
    assert install_payload["source"] == {"kind": "bundled", "value": plugin_id}
    assert install_payload["manifestSha256"].startswith("sha256:")
    assert install_payload["packageSha256"].startswith("sha256:")
    receipt_id = str(install_payload["receiptId"])
    assert receipt_id

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredRuntimeBindings"]["actions"] == [action_id]

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    plugin = index_response.json()["plugins"][0]
    assert plugin["pluginId"] == plugin_id
    assert plugin["source"] == {"kind": "bundled", "value": plugin_id}
    assert plugin["installState"] == "enabled"
    assert plugin["registryActivated"] is True

    result = _run_action(
        client,
        project_id,
        action_id=action_id,
        sheet_id=sheet_id,
        key=f"bundled-dispatch@sha256:{suffix}",
    )
    assert result["status"] == "completed", result["_response_text"]
    assert marker_path.exists()
    data = _sheet_data(client, project_id, sheet_id)
    values = _cell_values(data, column_name=output_key)
    assert all(dispatch_token in str(value) for value in values.values())

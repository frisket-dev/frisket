from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from deterministic_time import controlled_time

from frisket.engine.executor.actions import run_action_spec
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring import column_types
from frisket.engine.projections.runtime import ProjectionRuntimeError
from frisket.engine.projections.runtime import projection_runtime_binding
from frisket.engine.projections.runtime import runtime_projection_status
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
    disable_workbench_plugin,
    execute_workbench_plugin_local_install_plan,
    uninstall_workbench_plugin,
)
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    project_runtime_binding,
)
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
PLUGIN_ID = "frisket.geosmoke"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
# Twin-owned projection kind (frisket.geosmoke namespace); resolved by the
# execution role=map_points, not the bundled canonical projection-kind string.
SMOKE_PROJECTION_KIND = "frisket.geosmoke.projection.map_points"
RUNTIME_PLUGIN_ID = "frisket-runtime-demo"
RUNTIME_ACTION_KIND = "frisket.runtime_demo.action"
RUNTIME_IMPORTER_KIND = "frisket_runtime_demo_import"
RUNTIME_OPERATOR_KIND = "frisket.runtime_demo.operator"
RUNTIME_PROJECTION_KIND = "frisket.runtime_demo.projection"
PACKAGE_SCOPED_PLUGIN_ID = "frisket-runtime-package-scope"
PACKAGE_SCOPED_ACTION_KIND = f"{PACKAGE_SCOPED_PLUGIN_ID}.scoped"
AUTO_ENABLED_BUNDLED_PLUGIN_ID = "frisket.geo"


def _child_create_project(
    workspace_root: str,
    ready: Any,
    start: Any,
    attempting: Any,
    done: Any,
    result: Any,
) -> None:
    from frisket.server.workspace import Workspace

    workspace = Workspace(Path(workspace_root))
    ready.set()
    if not start.wait(10):
        result.put(("error", "start timeout"))
        done.set()
        return
    try:
        attempting.set()
        created = workspace.create("child project during uninstall")
        result.put(("ok", created["id"]))
    except BaseException as exc:
        result.put(("error", repr(exc)))
    finally:
        done.set()


def _child_hold_plugin_lifecycle_lock(workspace_root: str, acquired: Any) -> None:
    from frisket.authoring.workbench.plugin_package_catalog import (
        plugin_package_lifecycle_lock,
    )

    with plugin_package_lifecycle_lock(workspace_root):
        acquired.set()
        threading.Event().wait()


def _write_package_scoped_action_package(
    root: Path,
    *,
    marker: str,
    required_capabilities: list[str] | None = None,
    contributed_column_types: list[str] | None = None,
) -> Path:
    package_dir = root / marker
    package_dir.mkdir(parents=True, exist_ok=True)
    # A real native Action, because the manifest carries its generated catalog
    # entry and dispatch resolves the Action the module actually registers.
    # PACKAGE_MARKER stays: these tests read it to prove which package copy
    # a project resolved.
    (package_dir / "plugin.py").write_text(
        f"""from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin

PACKAGE_MARKER = {marker!r}


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    scoped: str


def scoped(params: Params, row: Row) -> RowResult[Output]:
    return RowResult(output=Output(scoped=f"{{PACKAGE_MARKER}}:{{params.source.read(row)}}"))


SCOPED = action(
    name="scoped",
    title="Package scoped action",
    description="Writes the resolved package marker.",
    category=ActionCategory.TEXT,
    run=map_rows(scoped),
)

plugin = Plugin(
    id={PACKAGE_SCOPED_PLUGIN_ID!r},
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(SCOPED,),
)
""",
        encoding="utf-8",
    )
    from frisket.plugins.manifest_generate import load_generation_source

    registered = load_generation_source(package_dir).actions[0]
    manifest = {
        "schema_version": "frisket.plugin.v1",
        "id": PACKAGE_SCOPED_PLUGIN_ID,
        "version": "0.1.0",
        "contributes": {
            "actions": [PACKAGE_SCOPED_ACTION_KIND],
            "importers": [],
            "operators": [],
            "projections": [],
            "column_types": contributed_column_types or [],
            "job_handlers": [],
        },
        "requires": {
            "capabilities": required_capabilities or [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "secrets": [],
        },
        "runtime": {
            "actions": [
                {
                    "kind": registered.action_id,
                    "handler_key": (
                        f"{PACKAGE_SCOPED_PLUGIN_ID}:{registered.definition.name}"
                    ),
                    "handler_api": "typed_action",
                    "module_path": "plugin.py",
                    "catalog_entry": registered.catalog_entry(),
                }
            ],
            "importers": [],
            "operators": [],
            "projections": [],
        },
    }
    manifest_path = package_dir / "plugin.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return manifest_path


def _install_enable_and_activate_package_backend(
    project: Project,
    *,
    project_id: str,
    manifest_path: Path,
    permissions_accepted: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Install through the trusted-local plan so the WORKSPACE catalog holds the
    # package identity; both projects in a workspace share it.
    status, payload = execute_workbench_plugin_local_install_plan(
        project,
        project_id=project_id,
        plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
        source={"kind": "localPath", "value": str(manifest_path)},
        arbitrary_package_load_allowed=False,
    )
    assert status == 200, payload
    receipt_id = str(payload["receiptId"])

    activation = activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
        receipt_id=receipt_id,
        trust_acknowledged=True,
        permissions_accepted=permissions_accepted or [TRUSTED_LOCAL_BACKEND_CAPABILITY],
        arbitrary_package_load_allowed=False,
    )
    backend = activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=True,
    )
    return activation, backend


def _create_geo_project(workspace: Path, project_id: str) -> tuple[int, int, int]:
    project = Project.create(workspace / f"{project_id}.frisket", name=project_id)
    sheet_id = project.add_sheet("places")
    name_col = project.add_column(sheet_id, "name", type="text")
    geo_col = project.add_column(sheet_id, "point", type="geo_point")
    project.add_rows(
        sheet_id,
        [{"name": "NYC", "point": {"lat": 40.7128, "lon": -74.006}}],
        {"name": name_col, "point": geo_col},
    )
    project.close()
    return sheet_id, geo_col, name_col


def _load_trust_and_activate_backend(client: TestClient, project_id: str) -> str:
    manifest_path = ROOT / "plugin.json"
    install_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert install_response.status_code == 200, install_response.text
    receipt_id = install_response.json()["receiptId"]
    assert receipt_id

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    assert activation_response.json()["installState"] == "enabled"

    backend_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend_response.status_code == 200, backend_response.text
    assert backend_response.json()["registeredRuntimeBindings"]["projections"] == [
        SMOKE_PROJECTION_KIND
    ]
    return receipt_id


def _runtime_plugin(client: TestClient, project_id: str) -> dict:
    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    return next(
        plugin
        for plugin in index_response.json()["plugins"]
        if plugin["pluginId"] == PLUGIN_ID
    )


def _map_points_response(
    client: TestClient,
    project_id: str,
    *,
    sheet_id: int,
    geo_col: int,
    name_col: int,
):
    return client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/map/points",
        params={"column_id": geo_col, "attrs": str(name_col)},
    )


def _seed_filter_project(project: Project) -> tuple[int, dict[str, int]]:
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
    }
    project.add_rows(
        sheet_id,
        [{"title": "A", "status": "todo"}, {"title": "B", "status": "done"}],
        columns,
    )
    return sheet_id, columns


def test_registry_only_runtime_bindings_do_not_dispatch_without_project_install(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        schema = payload.get("schemaVersion")
        if schema == "frisket.runtime_importer_request.v1":
            return {
                "schemaVersion": "frisket.runtime_importer_plan.v1",
                "columns": [{"name": "name", "type": "text"}],
                "rows": [{"name": "registry-only"}],
            }
        if schema == "frisket.runtime_operator_request.v1":
            return {
                "schemaVersion": "frisket.runtime_operator_plan.v1",
                "rowIds": list(payload.get("candidateRowIds", [])),
            }
        if schema == "frisket.runtime_projection_status_request.v1":
            return {
                "schemaVersion": "frisket.runtime_projection_status.v1",
                "status": "ready",
                "outputs": {"artifactRefs": [], "metrics": {}},
            }
        return {"status": "completed", "outputs": []}

    handler_keys = {
        "actions": f"{RUNTIME_PLUGIN_ID}:action",
        "importers": f"{RUNTIME_PLUGIN_ID}:importer",
        "operators": f"{RUNTIME_PLUGIN_ID}:operator",
        "projections": f"{RUNTIME_PLUGIN_ID}:projection",
    }
    try:
        for handler_key in handler_keys.values():
            register_trusted_backend_handler(handler_key, trusted_handler)
        default_registry().register_runtime_binding(
            "actions",
            RUNTIME_ACTION_KIND,
            handler_key=handler_keys["actions"],
            handler=trusted_handler,
            plugin=RUNTIME_PLUGIN_ID,
        )
        default_registry().register_runtime_binding(
            "importers",
            RUNTIME_IMPORTER_KIND,
            handler_key=handler_keys["importers"],
            handler=trusted_handler,
            plugin=RUNTIME_PLUGIN_ID,
        )
        default_registry().register_runtime_binding(
            "operators",
            RUNTIME_OPERATOR_KIND,
            handler_key=handler_keys["operators"],
            handler=trusted_handler,
            plugin=RUNTIME_PLUGIN_ID,
        )
        default_registry().register_runtime_binding(
            "projections",
            RUNTIME_PROJECTION_KIND,
            handler_key=handler_keys["projections"],
            handler=trusted_handler,
            plugin=RUNTIME_PLUGIN_ID,
        )

        project = Project.create(tmp_path / "registry-only.frisket")

        action_result = run_action_spec(
            project,
            {
                "action_id": RUNTIME_ACTION_KIND,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"value": "Ada"},
                "idempotency_key": "registry-only-action@sha256:v1",
            },
            project_id="registry-only-project",
        )
        assert action_result.status == "failed"
        # Fail-closed, and the refusal still names the action it refused. The
        # code is the generic request code rather than the old
        # `unsupported_action_kind`; see the lane report's production gap.
        assert action_result.errors[0].code == "invalid_action_request"
        assert action_result.errors[0].action_kind == RUNTIME_ACTION_KIND

        importer_result = run_action_spec(
            project,
            {
                "action_id": "import.runtime",
                "scope": {"kind": "project"},
                "sheet_name": "Registry Import",
                "params": {
                    "importer_kind": RUNTIME_IMPORTER_KIND,
                    "source": {
                        "kind": "runtime",
                        "label": "registry-only.fixture",
                        "fingerprint": "sha256:registry-only",
                    },
                    "handler_params": {},
                },
                "idempotency_key": "registry-only-import@sha256:v1",
            },
            project_id="registry-only-project",
        )
        assert importer_result.status == "failed"
        assert importer_result.errors[0].code == "unsupported_runtime_importer"

        sheet_id, _columns = _seed_filter_project(project)
        with pytest.raises(SheetRowSetError, match="unsupported filter"):
            resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=json.dumps({"status": {RUNTIME_OPERATOR_KIND: "done"}}),
                limit=50,
            )

        try:
            runtime_projection_status(
                project,
                projection_kind=RUNTIME_PROJECTION_KIND,
                project_id="registry-only-project",
                target={"sheetId": sheet_id},
                params={},
            )
        except ProjectionRuntimeError as exc:
            assert exc.code == "unsupported_runtime_projection"
        else:
            raise AssertionError("registry-only projection binding should fail closed")

        assert calls == []
    finally:
        for handler_key in handler_keys.values():
            unregister_trusted_backend_handler(handler_key)
        _reset_default_registry_for_tests()


def test_runtime_binding_dispatch_requires_manifest_runtime_declaration(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []
    declared_handler_key = f"{RUNTIME_PLUGIN_ID}:declared-action"
    injected_handler_key = f"{RUNTIME_PLUGIN_ID}:injected-action"

    def injected_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"status": "completed", "outputs": []}

    register_trusted_backend_handler(injected_handler_key, injected_handler)
    try:
        project = Project.create(tmp_path / "manifest-declaration.frisket")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=RUNTIME_PLUGIN_ID,
            runtime_bindings={"actions": {RUNTIME_ACTION_KIND: declared_handler_key}},
            project_id="manifest-declaration-project",
        )
        default_registry().register_runtime_binding(
            "actions",
            RUNTIME_ACTION_KIND,
            handler_key=injected_handler_key,
            handler=injected_handler,
            plugin=RUNTIME_PLUGIN_ID,
        )

        action_result = run_action_spec(
            project,
            {
                "action_id": RUNTIME_ACTION_KIND,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"value": "Ada"},
                "idempotency_key": "manifest-declaration-action@sha256:v1",
            },
            project_id="manifest-declaration-project",
        )

        assert action_result.status == "failed"
        # Fail-closed, and the refusal still names the action it refused. The
        # code is the generic request code rather than the old
        # `unsupported_action_kind`; see the lane report's production gap.
        assert action_result.errors[0].code == "invalid_action_request"
        assert action_result.errors[0].action_kind == RUNTIME_ACTION_KIND
        assert calls == []
    finally:
        unregister_trusted_backend_handler(injected_handler_key)
        _reset_default_registry_for_tests()


def test_runtime_binding_dispatch_uses_one_workspace_package_for_all_projects(
    tmp_path: Path,
) -> None:
    # Rewrite of the old same-manifest-different-package cross-project
    # contention test. Package identity is now WORKSPACE-owned: exactly one
    # package per plugin_id per workspace. Two projects in the same workspace
    # that enable the same plugin dispatch the SAME package — there is no
    # per-project package divergence to reconcile. ENABLEMENT stays
    # project-scoped, so disabling one project drops only that project's
    # dispatch while the shared workspace package keeps serving the other.
    _reset_default_registry_for_tests()
    try:
        project_a = Project.create(tmp_path / "project-a.frisket")
        project_b = Project.create(tmp_path / "project-b.frisket")
        manifest = _write_package_scoped_action_package(
            tmp_path / "packages", marker="package-a"
        )

        activation_a, backend_a = _install_enable_and_activate_package_backend(
            project_a,
            project_id="project-a",
            manifest_path=manifest,
        )
        assert backend_a["registeredRuntimeBindings"]["actions"] == [
            PACKAGE_SCOPED_ACTION_KIND
        ]
        activation_b, backend_b = _install_enable_and_activate_package_backend(
            project_b,
            project_id="project-b",
            manifest_path=manifest,
        )
        # ONE workspace package: identical manifest AND package digest for both.
        assert activation_b["manifestSha256"] == activation_a["manifestSha256"]
        assert activation_b["packageSha256"] == activation_a["packageSha256"]

        binding_a = project_runtime_binding(
            project_a, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
        )
        binding_b = project_runtime_binding(
            project_b, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
        )
        assert binding_a is not None and binding_b is not None
        assert (
            binding_a.metadata["package_sha256"]
            == binding_b.metadata["package_sha256"]
            == activation_a["packageSha256"]
        )

        # Enablement isolation: disabling project A drops A's dispatch; B keeps
        # serving the shared workspace package (which stays registered).
        disable_workbench_plugin(
            project_a, project_id="project-a", plugin_id=PACKAGE_SCOPED_PLUGIN_ID
        )
        assert (
            project_runtime_binding(
                project_a, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is None
        )
        assert (
            project_runtime_binding(
                project_b, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is not None
        )
    finally:
        _reset_default_registry_for_tests()


def test_workspace_package_update_requires_each_project_to_accept_new_capabilities(
    tmp_path: Path,
) -> None:
    from frisket.authoring.workbench.plugin_runtime import (
        ensure_workspace_plugin_packages,
    )
    from frisket.authoring.workbench.plugin_runtime_status import (
        workbench_plugin_runtime_index,
    )

    _reset_default_registry_for_tests()
    try:
        project_a = Project.create(tmp_path / "project-a.frisket")
        project_b = Project.create(tmp_path / "project-b.frisket")
        v1 = _write_package_scoped_action_package(
            tmp_path / "packages", marker="package-v1"
        )
        activation_a_v1, _ = _install_enable_and_activate_package_backend(
            project_a, project_id="project-a", manifest_path=v1
        )
        _install_enable_and_activate_package_backend(
            project_b, project_id="project-b", manifest_path=v1
        )

        escalated = [
            TRUSTED_LOCAL_BACKEND_CAPABILITY,
            "plugin:project_reads",
        ]
        v2 = _write_package_scoped_action_package(
            tmp_path / "packages",
            marker="package-v2",
            required_capabilities=escalated,
        )
        activation_a_v2, _ = _install_enable_and_activate_package_backend(
            project_a,
            project_id="project-a",
            manifest_path=v2,
            permissions_accepted=escalated,
        )
        assert activation_a_v2["packageSha256"] != activation_a_v1["packageSha256"]

        assert (
            project_runtime_binding(
                project_a, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is not None
        )
        assert (
            project_runtime_binding(
                project_b, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is None
        )
        b_index = workbench_plugin_runtime_index(project_b, project_id="project-b")
        b_plugin = next(
            item
            for item in b_index["plugins"]
            if item["pluginId"] == PACKAGE_SCOPED_PLUGIN_ID
        )
        assert b_plugin["installState"] == "installed"
        assert b_plugin["registryActivated"] is False

        # Restart rehydrates one workspace package, but cannot manufacture B's
        # acceptance of the escalated current manifest.
        _reset_default_registry_for_tests()
        ensure_workspace_plugin_packages(tmp_path)
        assert (
            project_runtime_binding(
                project_a, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is not None
        )
        assert (
            project_runtime_binding(
                project_b, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is None
        )

        activate_workbench_plugin_manifest(
            project_b,
            project_id="project-b",
            plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
            receipt_id=str(activation_a_v2["receiptId"]),
            trust_acknowledged=True,
            permissions_accepted=escalated,
            arbitrary_package_load_allowed=False,
        )
        activate_workbench_plugin_backend_contributions(
            project_b,
            project_id="project-b",
            plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
            trust_acknowledged=True,
            arbitrary_package_load_allowed=False,
            executable_handlers_allowed=True,
        )
        binding_b = project_runtime_binding(
            project_b, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
        )
        assert binding_b is not None
        assert binding_b.metadata["package_sha256"] == activation_a_v2["packageSha256"]
    finally:
        _reset_default_registry_for_tests()


def test_workspace_package_update_replaces_removed_contributions(
    tmp_path: Path,
) -> None:
    removed_type = "frisket_runtime_package_scope_record"
    _reset_default_registry_for_tests()
    try:
        project = Project.create(tmp_path / "project.frisket")
        v1 = _write_package_scoped_action_package(
            tmp_path / "packages",
            marker="contributions-v1",
            contributed_column_types=[removed_type],
        )
        _install_enable_and_activate_package_backend(
            project, project_id="project", manifest_path=v1
        )
        assert column_types.get_column_type(removed_type) is not None

        v2 = _write_package_scoped_action_package(
            tmp_path / "packages", marker="contributions-v2"
        )
        _install_enable_and_activate_package_backend(
            project, project_id="project", manifest_path=v2
        )
        assert column_types.get_column_type(removed_type) is None
    finally:
        _reset_default_registry_for_tests()


def test_workspace_uninstall_failure_keeps_catalog_and_retry_finishes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.authoring.workbench import plugin_runtime

    _reset_default_registry_for_tests()
    try:
        project_a = Project.create(tmp_path / "project-a.frisket")
        project_b = Project.create(tmp_path / "project-b.frisket")
        manifest = _write_package_scoped_action_package(
            tmp_path / "packages", marker="uninstall-recovery"
        )
        _install_enable_and_activate_package_backend(
            project_a, project_id="project-a", manifest_path=manifest
        )
        _install_enable_and_activate_package_backend(
            project_b, project_id="project-b", manifest_path=manifest
        )
        catalog = plugin_package_catalog_for_project(project_a)
        original = plugin_runtime._upsert_workbench_plugin_install_state

        def fail_project_b(candidate: Project, **kwargs: Any) -> None:
            if candidate.path == project_b.path:
                raise RuntimeError("project B write failed")
            original(candidate, **kwargs)

        monkeypatch.setattr(
            plugin_runtime, "_upsert_workbench_plugin_install_state", fail_project_b
        )
        with pytest.raises(RuntimeError, match="project B write failed"):
            uninstall_workbench_plugin(
                project_a,
                project_id="project-a",
                plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
                workspace_projects=(project_a, project_b),
            )
        assert catalog.get(PACKAGE_SCOPED_PLUGIN_ID) is not None
        assert (
            project_runtime_binding(
                project_a, binding_type="actions", kind=PACKAGE_SCOPED_ACTION_KIND
            )
            is None
        )

        monkeypatch.setattr(
            plugin_runtime, "_upsert_workbench_plugin_install_state", original
        )
        uninstall_workbench_plugin(
            project_a,
            project_id="project-a",
            plugin_id=PACKAGE_SCOPED_PLUGIN_ID,
            workspace_projects=(project_a, project_b),
        )
        assert catalog.get(PACKAGE_SCOPED_PLUGIN_ID) is None
        assert catalog.is_deleted(PACKAGE_SCOPED_PLUGIN_ID)
        for candidate in (project_a, project_b):
            row = candidate.db.execute(
                "SELECT install_state, permissions_accepted, "
                "executable_handlers_allowed FROM workbench_plugin_installs "
                "WHERE plugin_id=?",
                (PACKAGE_SCOPED_PLUGIN_ID,),
            ).fetchone()
            assert row is not None
            assert row["install_state"] == "uninstalled"
            assert json.loads(row["permissions_accepted"]) == []
            assert row["executable_handlers_allowed"] == 0
    finally:
        _reset_default_registry_for_tests()


@pytest.mark.real_bundled_plugins
def test_workspace_uninstall_serializes_concurrent_project_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.server.services import workbench as workbench_service_module
    from frisket.server.services.workbench import WorkbenchService
    from frisket.server.workspace import Workspace

    _reset_default_registry_for_tests()
    try:
        workspace = Workspace(tmp_path / "workspace")
        second_workspace = Workspace(workspace.root)
        project_id = workspace.create("project a")["id"]
        project = workspace.get(project_id)
        service = WorkbenchService(workspace)
        entered = threading.Event()
        release = threading.Event()
        create_started = threading.Event()
        created = threading.Event()
        original = workbench_service_module.uninstall_workbench_plugin

        def pause_uninstall(*args: Any, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            workbench_service_module,
            "uninstall_workbench_plugin",
            pause_uninstall,
        )
        failures: list[BaseException] = []
        created_ids: list[str] = []

        def uninstall() -> None:
            try:
                service.uninstall(project_id, plugin_id=AUTO_ENABLED_BUNDLED_PLUGIN_ID)
            except BaseException as exc:
                failures.append(exc)

        def create_project() -> None:
            try:
                create_started.set()
                created_ids.append(
                    second_workspace.create("project during uninstall")["id"]
                )
                created.set()
            except BaseException as exc:
                failures.append(exc)

        with controlled_time() as concurrency:
            uninstall_thread = concurrency.background(uninstall)
            assert entered.wait(5)
            create_thread = concurrency.background(create_project)
            assert create_started.wait(5)
            completed_before_release = created.wait(1)
            premature_state = None
            if completed_before_release:
                premature_project = second_workspace.get(created_ids[0])
                premature_state = premature_project.db.execute(
                    "SELECT install_state, permissions_accepted, "
                    "executable_handlers_allowed FROM workbench_plugin_installs "
                    "WHERE plugin_id=?",
                    (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
                ).fetchone()
            release.set()
            uninstall_thread.join(5)
            create_thread.join(5)
            assert not failures
            assert not uninstall_thread.is_alive()
            assert not create_thread.is_alive()
        if completed_before_release:
            assert premature_state is not None
            assert premature_state["install_state"] == "enabled"
            assert json.loads(premature_state["permissions_accepted"]) == [
                TRUSTED_LOCAL_BACKEND_CAPABILITY
            ]
            assert premature_state["executable_handlers_allowed"] == 1
            pytest.fail(
                "project creation escaped workspace uninstall serialization and "
                "retained the bundled plugin's enabled state and executable grant"
            )
        assert created_ids
        new_project = second_workspace.get(created_ids[0])
        row = new_project.db.execute(
            "SELECT 1 FROM workbench_plugin_installs WHERE plugin_id=?",
            (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
        ).fetchone()
        assert row is None

        status, payload = execute_workbench_plugin_local_install_plan(
            project,
            project_id=project_id,
            plugin_id=AUTO_ENABLED_BUNDLED_PLUGIN_ID,
            source={
                "kind": "bundled",
                "value": AUTO_ENABLED_BUNDLED_PLUGIN_ID,
            },
            arbitrary_package_load_allowed=False,
        )
        assert status == 200, payload
        row_after_reinstall = new_project.db.execute(
            "SELECT 1 FROM workbench_plugin_installs WHERE plugin_id=?",
            (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
        ).fetchone()
        assert row_after_reinstall is None
    finally:
        _reset_default_registry_for_tests()


@pytest.mark.real_bundled_plugins
def test_workspace_uninstall_serializes_child_process_creation_and_crash_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.server.services import workbench as workbench_service_module
    from frisket.server.services.workbench import WorkbenchService
    from frisket.server.workspace import Workspace

    _reset_default_registry_for_tests()
    child = None
    holder = None
    try:
        workspace = Workspace(tmp_path / "workspace")
        project_id = workspace.create("project a")["id"]
        project = workspace.get(project_id)
        service = WorkbenchService(workspace)

        process_context = multiprocessing.get_context("spawn")
        ready = process_context.Event()
        start = process_context.Event()
        attempting = process_context.Event()
        done = process_context.Event()
        result = process_context.Queue()
        child = process_context.Process(
            target=_child_create_project,
            args=(str(workspace.root), ready, start, attempting, done, result),
        )
        child.start()
        assert ready.wait(15)

        entered = threading.Event()
        release = threading.Event()
        original = workbench_service_module.uninstall_workbench_plugin

        def pause_after_enumeration(*args: Any, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            assert release.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            workbench_service_module,
            "uninstall_workbench_plugin",
            pause_after_enumeration,
        )
        failures: list[BaseException] = []

        def uninstall() -> None:
            try:
                service.uninstall(project_id, plugin_id=AUTO_ENABLED_BUNDLED_PLUGIN_ID)
            except BaseException as exc:
                failures.append(exc)

        with controlled_time() as concurrency:
            uninstall_thread = concurrency.background(uninstall)
            assert entered.wait(10)
            start.set()
            assert attempting.wait(10)
            completed_before_release = done.wait(1)
            premature_result = (
                result.get(timeout=2) if completed_before_release else None
            )
            premature_state = None
            if premature_result is not None:
                status, premature_project_id = premature_result
                assert status == "ok", premature_project_id
                premature_project = Workspace(workspace.root).get(premature_project_id)
                premature_state = premature_project.db.execute(
                    "SELECT install_state, permissions_accepted, "
                    "executable_handlers_allowed FROM workbench_plugin_installs "
                    "WHERE plugin_id=?",
                    (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
                ).fetchone()
            release.set()
            uninstall_thread.join(10)
            child.join(20)
            assert not failures
            assert not uninstall_thread.is_alive()
            assert child.exitcode == 0
        if completed_before_release:
            assert premature_state is not None
            assert premature_state["install_state"] == "enabled"
            assert json.loads(premature_state["permissions_accepted"]) == [
                TRUSTED_LOCAL_BACKEND_CAPABILITY
            ]
            assert premature_state["executable_handlers_allowed"] == 1
            pytest.fail(
                "child project creation escaped workspace uninstall serialization "
                "and retained the bundled plugin's enabled state and executable grant"
            )
        status, child_project_id = result.get(timeout=2)
        assert status == "ok", child_project_id

        child_project = Workspace(workspace.root).get(child_project_id)
        assert (
            child_project.db.execute(
                "SELECT 1 FROM workbench_plugin_installs WHERE plugin_id=?",
                (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
            ).fetchone()
            is None
        )
        status, payload = execute_workbench_plugin_local_install_plan(
            project,
            project_id=project_id,
            plugin_id=AUTO_ENABLED_BUNDLED_PLUGIN_ID,
            source={
                "kind": "bundled",
                "value": AUTO_ENABLED_BUNDLED_PLUGIN_ID,
            },
            arbitrary_package_load_allowed=False,
        )
        assert status == 200, payload
        assert (
            child_project.db.execute(
                "SELECT 1 FROM workbench_plugin_installs WHERE plugin_id=?",
                (AUTO_ENABLED_BUNDLED_PLUGIN_ID,),
            ).fetchone()
            is None
        )

        acquired = process_context.Event()
        holder = process_context.Process(
            target=_child_hold_plugin_lifecycle_lock,
            args=(str(workspace.root), acquired),
        )
        holder.start()
        assert acquired.wait(10)
        from filelock import FileLock, Timeout

        timed_probe = FileLock(
            str(workspace.root / ".plugin-package-lifecycle.lock"), timeout=0.1
        )
        with pytest.raises(Timeout):
            with timed_probe:
                pass
        holder.terminate()
        holder.join(10)
        assert holder.exitcode is not None
        recovered = Workspace(workspace.root).create("after crashed lock holder")
        assert recovered["id"]
    finally:
        if child is not None and child.is_alive():
            child.terminate()
            child.join(10)
        if holder is not None and holder.is_alive():
            holder.terminate()
            holder.join(10)
        _reset_default_registry_for_tests()


def test_workbench_plugin_runtime_dispatch_is_project_scoped_after_disable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        a_sheet, a_geo_col, a_name_col = _create_geo_project(workspace, "project-a")
        b_sheet, b_geo_col, b_name_col = _create_geo_project(workspace, "project-b")
        client = TestClient(create_app(workspace))

        _load_trust_and_activate_backend(client, "project-a")
        _load_trust_and_activate_backend(client, "project-b")
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None

        before_disable_b = _map_points_response(
            client,
            "project-b",
            sheet_id=b_sheet,
            geo_col=b_geo_col,
            name_col=b_name_col,
        )
        assert before_disable_b.status_code == 200, before_disable_b.text
        assert (
            before_disable_b.headers["x-frisket-runtime-projection-kind"]
            == SMOKE_PROJECTION_KIND
        )

        disabled_a = client.post(
            f"/api/projects/project-a/workbench/plugins/{PLUGIN_ID}/disable"
        )
        assert disabled_a.status_code == 200, disabled_a.text
        assert disabled_a.json()["installState"] == "disabled"
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None

        plugin_b = _runtime_plugin(client, "project-b")
        assert plugin_b["installState"] == "enabled"
        assert plugin_b["registryActivated"] is True

        after_disable_b = _map_points_response(
            client,
            "project-b",
            sheet_id=b_sheet,
            geo_col=b_geo_col,
            name_col=b_name_col,
        )
        assert after_disable_b.status_code == 200, after_disable_b.text
        assert (
            after_disable_b.headers["x-frisket-runtime-projection-kind"]
            == SMOKE_PROJECTION_KIND
        )

        after_disable_a = _map_points_response(
            client,
            "project-a",
            sheet_id=a_sheet,
            geo_col=a_geo_col,
            name_col=a_name_col,
        )
        # With
        # MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED=True the disabled
        # project gets the typed refusal instead of the bridge world's
        # header-less 200 — "disabled geo plugin means no map". Project B
        # (still enabled) kept serving above, so per-project isolation is
        # proven by the CONTRAST, exactly what this test exists for.
        assert after_disable_a.status_code == 409, after_disable_a.text
        assert after_disable_a.json()["detail"]["code"] == "map_points_binding_missing"

        uninstalled_a = client.post(
            f"/api/projects/project-a/workbench/plugins/{PLUGIN_ID}/uninstall"
        )
        assert uninstalled_a.status_code == 200, uninstalled_a.text
        assert uninstalled_a.json()["installState"] == "uninstalled"
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is None
        b_index = client.get("/api/projects/project-b/workbench/plugins")
        assert b_index.status_code == 200, b_index.text
        assert PLUGIN_ID not in {item["pluginId"] for item in b_index.json()["plugins"]}
        project_b = Project(workspace / "project-b.frisket")
        try:
            b_row = project_b.db.execute(
                "SELECT install_state, permissions_accepted, "
                "executable_handlers_allowed FROM workbench_plugin_installs "
                "WHERE plugin_id=?",
                (PLUGIN_ID,),
            ).fetchone()
            assert b_row is not None
            assert b_row["install_state"] == "uninstalled"
            assert b_row["permissions_accepted"] == "[]"
            assert b_row["executable_handlers_allowed"] == 0
        finally:
            project_b.close()
    finally:
        _reset_default_registry_for_tests()


def test_workbench_plugin_disable_leaves_shared_registration_registered(
    tmp_path: Path, monkeypatch
) -> None:
    # Rewrite of the old cross-workspace-root claim/teardown test. Each
    # workspace OWNS its package catalog and re-registers its packages ONCE at
    # construction; the process registry is shared. Disabling a plugin in one
    # project — or in every project — NEVER unregisters the shared package
    # (the root-claim / unregister-on-last-disable machinery was deleted).
    # Per-project ENABLEMENT is what gates dispatch.
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        workspace_a.mkdir(parents=True, exist_ok=True)
        workspace_b.mkdir(parents=True, exist_ok=True)
        _create_geo_project(workspace_a, "project-a")
        b_sheet, b_geo_col, b_name_col = _create_geo_project(workspace_b, "project-b")
        client_a = TestClient(create_app(workspace_a))
        client_b = TestClient(create_app(workspace_b))

        _load_trust_and_activate_backend(client_a, "project-a")
        _load_trust_and_activate_backend(client_b, "project-b")
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None

        # A plain process restart drops the shared registry. Each workspace
        # re-registers its own catalog package at construction.
        _reset_default_registry_for_tests()
        client_a = TestClient(create_app(workspace_a))
        client_b = TestClient(create_app(workspace_b))
        assert _runtime_plugin(client_a, "project-a")["registryActivated"] is True
        assert _runtime_plugin(client_b, "project-b")["registryActivated"] is True
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None

        disabled_a = client_a.post(
            f"/api/projects/project-a/workbench/plugins/{PLUGIN_ID}/disable"
        )
        assert disabled_a.status_code == 200, disabled_a.text
        assert disabled_a.json()["installState"] == "disabled"

        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None
        assert _runtime_plugin(client_b, "project-b")["registryActivated"] is True
        response_b = _map_points_response(
            client_b,
            "project-b",
            sheet_id=b_sheet,
            geo_col=b_geo_col,
            name_col=b_name_col,
        )
        assert response_b.status_code == 200, response_b.text
        assert (
            response_b.headers["x-frisket-runtime-projection-kind"]
            == SMOKE_PROJECTION_KIND
        )

        # Disabling the LAST enabling project no longer tears down the shared
        # package: it stays registered (workspace-owned identity), and B's own
        # disable simply flips B's enablement.
        disabled_b = client_b.post(
            f"/api/projects/project-b/workbench/plugins/{PLUGIN_ID}/disable"
        )
        assert disabled_b.status_code == 200, disabled_b.text
        assert disabled_b.json()["installState"] == "disabled"
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None
    finally:
        _reset_default_registry_for_tests()

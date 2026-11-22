"""Workbench plugin generic runtime bindings, activation atomicity, trusted job handler bindings, and project isolation tests."""

from __future__ import annotations

import json
import multiprocessing
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from deterministic_time import controlled_time
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project

from frisket.authoring import column_types
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
    plugin_package_lifecycle_lock,
)
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
    disable_workbench_plugin,
    ensure_workspace_plugin_packages,
    execute_workbench_plugin_local_install_plan,
    uninstall_workbench_plugin,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    project_runtime_binding,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    workbench_plugin_runtime_index,
)
from frisket.contracts.action import ActionResult
from frisket.engine import executor
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.engine.projections.runtime import (
    ProjectionRuntimeError,
    projection_runtime_binding,
    runtime_projection_status,
)
from frisket.engine.store import Project
from frisket.plugins.manifest_generate import load_generation_source
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.server.app import create_app
from frisket.server.services import workbench as workbench_service_module
from frisket.server.services.workbench import WorkbenchService
from frisket.server.workspace import Workspace
from frisket.url_classification import (
    classify_url,
    plugin_matchers,
    registered_matchers,
)

# --- generic bindings ---

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
GENERIC_BINDINGS_PLUGIN_ID = "frisket-runtime-demo"
ACTION_KIND = f"{GENERIC_BINDINGS_PLUGIN_ID}.demo_action"
IMPORTER_KIND = "frisket_runtime_demo_import"
OPERATOR_KIND = "frisket.runtime_demo.operator"
PROJECTION_KIND = "frisket.runtime_demo.projection"
ATOMIC_COLUMN_TYPE = "frisket_runtime_demo_atomic_record"
ATOMIC_JOB_KIND = "frisket.runtime_demo.atomic_job"
ATOMIC_JOB_HANDLER_KEY = f"{GENERIC_BINDINGS_PLUGIN_ID}:atomic_job"
ATOMIC_MATCHER_ID = "frisket.runtime_demo.atomic_matcher"
ATOMIC_MATCHER_URL = "https://runtime-demo-atomic.example/item/1"
ACTIVATION_FAULTS = (
    "after_manifest_publication",
    "after_metadata_publication",
    "after_matcher_publication",
    "after_executable_handler_publication",
    "after_first_runtime_binding_publication",
    "before_executable_grant_recording",
)


def _trusted_handler(payload: dict[str, Any]) -> dict[str, Any]:
    return {"handled": True, "payload": payload}


def _handler_key(kind: str) -> str:
    return f"{GENERIC_BINDINGS_PLUGIN_ID}:{kind}"


def _generic_bindings_write_manifest(
    tmp_path: Path,
    *,
    name: str = GENERIC_BINDINGS_PLUGIN_ID,
    runtime_override: dict[str, Any] | None = None,
    contributes_override: dict[str, Any] | None = None,
) -> Path:
    package_dir = tmp_path / "plugin_packages" / name
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / "plugin.json"
    # A real native Action: the manifest carries its generated catalog entry,
    # and dispatch resolves the Action the module actually registers. The
    # declaration uses the manifest id, not the directory name, because the
    # handler key must be namespaced by the id the manifest publishes.
    plugin_id = GENERIC_BINDINGS_PLUGIN_ID
    (package_dir / "plugin.py").write_text(
        f"""from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class Params(ActionParams):
    name: ColumnRef[str]


class Output(BaseModel):
    demo: str


def demo_action(params: Params, row: Row) -> RowResult[Output]:
    return RowResult(output=Output(demo=str(params.name.read(row))))


DEMO = action(
    name="demo_action",
    title="Runtime demo action",
    description="Echoes one text column.",
    category=ActionCategory.TEXT,
    run=map_rows(demo_action),
)

plugin = Plugin(
    id={plugin_id!r},
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(DEMO,),
)
""",
        encoding="utf-8",
    )

    registered = load_generation_source(package_dir).actions[0]
    contributes = {
        "actions": [ACTION_KIND],
        "importers": [IMPORTER_KIND],
        "operators": [OPERATOR_KIND],
        "projections": [PROJECTION_KIND],
        "column_types": [],
        "job_handlers": [],
    }
    if contributes_override is not None:
        contributes.update(contributes_override)
    runtime = {
        "actions": [
            {
                "kind": registered.action_id,
                "handler_key": _handler_key(registered.definition.name),
                "handler_api": "typed_action",
                "module_path": "plugin.py",
                "catalog_entry": registered.catalog_entry(),
            }
        ],
        "importers": [{"kind": IMPORTER_KIND, "handler_key": _handler_key("importer")}],
        "operators": [{"kind": OPERATOR_KIND, "handler_key": _handler_key("operator")}],
        "projections": [
            {"kind": PROJECTION_KIND, "handler_key": _handler_key("projection")}
        ],
    }
    if runtime_override is not None:
        runtime.update(runtime_override)
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": GENERIC_BINDINGS_PLUGIN_ID,
                "version": "0.1.0",
                "contributes": contributes,
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
                "runtime": runtime,
            }
        ),
        encoding="utf-8",
    )
    return path


def _generic_bindings_load_and_enable(
    client: TestClient, project_id: str, manifest_path: Path
) -> tuple[str, str]:
    receipt_id = _load_manifest(client, project_id, manifest_path)

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    return receipt_id, activation_response.json()["manifestSha256"]


def _load_manifest(client: TestClient, project_id: str, manifest_path: Path) -> str:
    # Install-local writes the workspace catalog package identity
    # and this project's "installed" enablement row, and returns the catalog
    # receipt id used to bind a later activation.
    install_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert install_response.status_code == 200, install_response.text
    receipt_id = install_response.json()["receiptId"]
    assert receipt_id
    return receipt_id


def test_trusted_backend_activation_records_generic_runtime_bindings(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    keys = [
        _handler_key("importer"),
        _handler_key("operator"),
        _handler_key("projection"),
    ]
    for key in keys:
        register_trusted_backend_handler(key, _trusted_handler)
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Generic runtime bindings"}
        ).json()["id"]
        manifest_path = _generic_bindings_write_manifest(tmp_path)
        receipt_id, manifest_sha = _generic_bindings_load_and_enable(
            client, project_id, manifest_path
        )

        metadata_only = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert metadata_only.status_code == 200, metadata_only.text
        assert metadata_only.json()["trustedRuntimeBindingsRegistered"] is False
        assert metadata_only.json()["registeredRuntimeBindings"] == {
            "actions": [],
            "importers": [],
            "operators": [],
            "projections": [],
            "jobHandlers": [],
        }

        executable = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert executable.status_code == 200, executable.text
        body = executable.json()
        assert body["schemaVersion"] == "frisket.workbench_plugin_backend_activation.v1"
        assert body["projectId"] == project_id
        assert body["pluginId"] == GENERIC_BINDINGS_PLUGIN_ID
        assert body["receiptId"] == receipt_id
        assert body["manifestSha256"] == manifest_sha
        assert body["arbitraryPackageLoadAllowed"] is False
        assert body["trustedRuntimeBindingsRegistered"] is True
        assert body["registeredRuntimeBindings"] == {
            "actions": [ACTION_KIND],
            "importers": [IMPORTER_KIND],
            "operators": [OPERATOR_KIND],
            "projections": [PROJECTION_KIND],
            "jobHandlers": [],
        }

        registry = default_registry()
        assert {
            spec.kind: (spec.handler_key, spec.handler_api)
            for spec in registry.runtime_binding_specs("actions")
        } == {ACTION_KIND: (_handler_key("demo_action"), "plugin_typed_action_native")}
        assert {
            spec.kind: spec.handler_key
            for spec in registry.runtime_binding_specs("importers")
        } == {IMPORTER_KIND: _handler_key("importer")}
        assert {
            spec.kind: spec.handler_key
            for spec in registry.runtime_binding_specs("operators")
        } == {OPERATOR_KIND: _handler_key("operator")}
        assert {
            spec.kind: spec.handler_key
            for spec in registry.runtime_binding_specs("projections")
        } == {PROJECTION_KIND: _handler_key("projection")}
    finally:
        for key in keys:
            unregister_trusted_backend_handler(key)
        _reset_default_registry_for_tests()


def test_generic_runtime_bindings_reject_unknown_or_undeclared_keys(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Rejected runtime bindings"}
        ).json()["id"]

        unknown_manifest = _generic_bindings_write_manifest(
            tmp_path,
            name="unknown-runtime-binding",
            runtime_override={
                "actions": [],
                "importers": [],
                "operators": [
                    {
                        "kind": OPERATOR_KIND,
                        "handler_key": _handler_key("unknown_operator"),
                    }
                ],
                "projections": [],
            },
        )
        _generic_bindings_load_and_enable(client, project_id, unknown_manifest)
        unknown = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert unknown.status_code == 409, unknown.text
        assert (
            unknown.json()["detail"]["code"]
            == "plugin_backend_activation_runtime_handler_key_unknown"
        )

        _reset_default_registry_for_tests()
        client = TestClient(create_app(tmp_path / "workspace-undeclared"))
        project_id = client.post(
            "/api/projects", json={"name": "Undeclared runtime binding"}
        ).json()["id"]
        key = _handler_key("action")
        register_trusted_backend_handler(key, _trusted_handler)
        undeclared_manifest = _generic_bindings_write_manifest(
            tmp_path,
            name="undeclared-runtime-binding",
            runtime_override={
                "actions": [
                    {
                        "kind": "frisket.runtime_demo.not_declared",
                        "handler_key": key,
                    }
                ],
                "importers": [],
                "operators": [],
                "projections": [],
            },
        )
        _generic_bindings_load_and_enable(client, project_id, undeclared_manifest)
        undeclared = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert undeclared.status_code == 409, undeclared.text
        assert (
            undeclared.json()["detail"]["code"]
            == "plugin_backend_activation_runtime_binding_invalid"
        )
    finally:
        unregister_trusted_backend_handler(_handler_key("action"))
        _reset_default_registry_for_tests()


def test_generic_runtime_bindings_reject_cross_plugin_binding_collision(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    key = _handler_key("action")
    register_trusted_backend_handler(key, _trusted_handler)
    try:
        default_registry().register_runtime_binding(
            "actions",
            ACTION_KIND,
            handler_key="frisket-other-runtime:action",
            plugin="frisket-other-runtime",
        )
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Runtime binding collision"}
        ).json()["id"]
        manifest_path = _generic_bindings_write_manifest(
            tmp_path,
            name="runtime-binding-collision",
            runtime_override={
                "actions": [{"kind": ACTION_KIND, "handler_key": key}],
                "importers": [],
                "operators": [],
                "projections": [],
            },
        )
        _generic_bindings_load_and_enable(client, project_id, manifest_path)

        response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{GENERIC_BINDINGS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "plugin_backend_activation_conflict"
    finally:
        unregister_trusted_backend_handler(key)
        _reset_default_registry_for_tests()


def _write_atomicity_manifest(tmp_path: Path) -> Path:
    return _generic_bindings_write_manifest(
        tmp_path,
        name="activation-atomicity",
        contributes_override={
            "column_types": [ATOMIC_COLUMN_TYPE],
            "job_handlers": [ATOMIC_JOB_KIND],
            "matchers": [ATOMIC_MATCHER_ID],
        },
        runtime_override={
            "job_handlers": [
                {
                    "kind": ATOMIC_JOB_KIND,
                    "handler_key": ATOMIC_JOB_HANDLER_KEY,
                }
            ],
            "matchers": [
                {
                    "matcher_id": ATOMIC_MATCHER_ID,
                    "provider": "runtime-demo-atomic",
                    "domains": ["runtime-demo-atomic.example"],
                    "kind": "scraper",
                    "handler_action_kind": "media.fetch_url",
                    "priority": 1,
                }
            ],
        },
    )


def _atomic_handler_keys() -> tuple[str, ...]:
    return (
        _handler_key("importer"),
        _handler_key("operator"),
        _handler_key("projection"),
        ATOMIC_JOB_HANDLER_KEY,
    )


def _clean_atomic_publication() -> None:
    plugin_matchers.unregister_plugin_matchers(GENERIC_BINDINGS_PLUGIN_ID)
    default_registry().unload_plugin_entries(GENERIC_BINDINGS_PLUGIN_ID)
    for key in _atomic_handler_keys():
        unregister_trusted_backend_handler(key)
    _reset_default_registry_for_tests()


@pytest.fixture()
def loaded_atomic_plugin(tmp_path: Path) -> Iterator[tuple[Project, str, str]]:
    _clean_atomic_publication()
    try:
        client = TestClient(create_app(tmp_path / "workspace-atomicity"))
        project_id = client.post(
            "/api/projects", json={"name": "Plugin activation atomicity"}
        ).json()["id"]
        receipt_id = _load_manifest(
            client, project_id, _write_atomicity_manifest(tmp_path)
        )
        for key in _atomic_handler_keys():
            register_trusted_backend_handler(key, _trusted_handler)
        yield client.app.state.workspace.get(project_id), project_id, receipt_id
    finally:
        _clean_atomic_publication()


def _activate_manifest(
    project: Project, project_id: str, receipt_id: str
) -> dict[str, Any]:
    return plugin_runtime.activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=GENERIC_BINDINGS_PLUGIN_ID,
        receipt_id=receipt_id,
        trust_acknowledged=True,
        permissions_accepted=[TRUSTED_LOCAL_BACKEND_CAPABILITY],
        arbitrary_package_load_allowed=False,
    )


def _atomic_runtime_bindings() -> tuple[tuple[str, str], ...]:
    return (
        ("actions", ACTION_KIND),
        ("importers", IMPORTER_KIND),
        ("operators", OPERATOR_KIND),
        ("projections", PROJECTION_KIND),
    )


def _publication_snapshot(project: Project) -> Counter[tuple[str, ...]]:
    registry = default_registry()
    worker_handlers = HandlerRegistry()
    registry.install_job_handlers(worker_handlers)
    classification = classify_url(
        ATOMIC_MATCHER_URL, enabled_plugin_ids={GENERIC_BINDINGS_PLUGIN_ID}
    )
    # Enablement (install_state/activation/grant) is the project row's; package
    # identity (receipt/manifest/package sha) is the workspace catalog's.
    # Merge both so the snapshot still detects partial state.
    install = project.db.execute(
        "SELECT install_state, activation, executable_handlers_allowed "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (GENERIC_BINDINGS_PLUGIN_ID,),
    ).fetchone()
    catalog_entry = plugin_package_catalog_for_project(project).get(
        GENERIC_BINDINGS_PLUGIN_ID
    )
    items: list[tuple[str, ...]] = [
        ("manifest", item.manifest.id, item.sha256)
        for item in registry.plugin_manifests()
        if item.manifest.id == GENERIC_BINDINGS_PLUGIN_ID
    ]
    column_type = column_types.get_column_type(ATOMIC_COLUMN_TYPE)
    if column_type is not None and column_type.plugin == GENERIC_BINDINGS_PLUGIN_ID:
        items.append(("column_type", ATOMIC_COLUMN_TYPE))
    items.extend(
        ("importer", item.name)
        for item in registry.importer_specs()
        if item.plugin == GENERIC_BINDINGS_PLUGIN_ID
    )
    items.extend(
        ("job_handler", item.kind)
        for item in registry.job_handler_specs()
        if item.plugin == GENERIC_BINDINGS_PLUGIN_ID
    )
    if worker_handlers.get(ATOMIC_JOB_KIND) is not None:
        items.append(("executable_job_handler", ATOMIC_JOB_KIND))
    items.extend(
        ("matcher", item.matcher_id)
        for item in registered_matchers()
        if item.origin_plugin_id == GENERIC_BINDINGS_PLUGIN_ID
    )
    if (
        classification is not None
        and classification.origin_plugin_id == GENERIC_BINDINGS_PLUGIN_ID
    ):
        items.append(("matcher_dispatch", classification.matcher_id))
    for binding_type, kind in _atomic_runtime_bindings():
        items.extend(
            ("runtime_binding", binding_type, item.kind)
            for item in registry.runtime_binding_specs(binding_type)
            if item.plugin == GENERIC_BINDINGS_PLUGIN_ID
        )
        if (
            project_runtime_binding(project, binding_type=binding_type, kind=kind)
            is not None
        ):
            items.append(("runtime_dispatch", binding_type, kind))
    if install is not None:
        items.append(
            (
                "install_state",
                str((catalog_entry or {}).get("receipt_id") or ""),
                str((catalog_entry or {}).get("manifest_sha256") or ""),
                str((catalog_entry or {}).get("package_sha256") or ""),
                str(install["install_state"] or ""),
                str(install["activation"] or ""),
                "1" if install["executable_handlers_allowed"] else "0",
            )
        )
    if install is not None and install["executable_handlers_allowed"]:
        items.append(("executable_grant", GENERIC_BINDINGS_PLUGIN_ID))
    return Counter(items)


def _install_state_publication(
    activated: dict[str, Any], *, executable_handlers_allowed: bool
) -> tuple[str, ...]:
    return (
        "install_state",
        activated["receiptId"],
        activated["manifestSha256"],
        activated["packageSha256"],
        "enabled",
        "registryManifestRegistered",
        "1" if executable_handlers_allowed else "0",
    )


def _manifest_only_publication(
    activated: dict[str, Any],
) -> Counter[tuple[str, ...]]:
    return Counter(
        [
            ("manifest", GENERIC_BINDINGS_PLUGIN_ID, activated["manifestSha256"]),
            _install_state_publication(activated, executable_handlers_allowed=False),
        ]
    )


def _complete_publication(activated: dict[str, Any]) -> Counter[tuple[str, ...]]:
    return Counter(
        [
            ("manifest", GENERIC_BINDINGS_PLUGIN_ID, activated["manifestSha256"]),
            _install_state_publication(activated, executable_handlers_allowed=True),
            ("column_type", ATOMIC_COLUMN_TYPE),
            ("importer", IMPORTER_KIND),
            ("job_handler", ATOMIC_JOB_KIND),
            ("executable_job_handler", ATOMIC_JOB_KIND),
            ("matcher", ATOMIC_MATCHER_ID),
            ("matcher_dispatch", ATOMIC_MATCHER_ID),
            ("executable_grant", GENERIC_BINDINGS_PLUGIN_ID),
            *(
                ("runtime_binding", binding_type, kind)
                for binding_type, kind in _atomic_runtime_bindings()
            ),
            *(
                ("runtime_dispatch", binding_type, kind)
                for binding_type, kind in _atomic_runtime_bindings()
            ),
        ]
    )


def _raise_fault(name: str) -> Any:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"fault:{name}")

    return fail


def _inject_fault(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    call_through_targets = {
        "after_matcher_publication": "_register_backend_contribution_metadata",
        "after_executable_handler_publication": "_register_executable_job_handlers",
    }
    if target := call_through_targets.get(fault):
        publish = getattr(plugin_runtime, target)

        def fail_after_publication(*args: Any, **kwargs: Any) -> None:
            publish(*args, **kwargs)
            raise RuntimeError(f"fault:{fault}")

        monkeypatch.setattr(plugin_runtime, target, fail_after_publication)
        return
    if fault == "after_metadata_publication":
        monkeypatch.setattr(
            plugin_matchers, "register_plugin_matchers", _raise_fault(fault)
        )
        return
    if fault == "after_first_runtime_binding_publication":
        registry = default_registry()
        register = registry.register_runtime_binding

        def fail_after_first(*args: Any, **kwargs: Any) -> None:
            register(*args, **kwargs)
            raise RuntimeError(f"fault:{fault}")

        monkeypatch.setattr(registry, "register_runtime_binding", fail_after_first)
        return
    if fault == "before_executable_grant_recording":
        monkeypatch.setattr(
            plugin_runtime,
            "_record_backend_activation_executable_handlers_grant",
            _raise_fault(fault),
        )
        return
    raise AssertionError(f"unknown fault: {fault}")


def _activate_backend(project: Project, project_id: str) -> dict[str, Any]:
    return plugin_runtime.activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=GENERIC_BINDINGS_PLUGIN_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=True,
    )


@pytest.mark.parametrize("fault", ACTIVATION_FAULTS)
def test_initial_plugin_activation_fault_is_atomic_and_retries_cleanly(
    loaded_atomic_plugin: tuple[Project, str, str],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    if fault == "after_manifest_publication":
        with monkeypatch.context() as fault_patch:
            fault_patch.setattr(
                plugin_runtime,
                "_upsert_workbench_plugin_install_state",
                _raise_fault(fault),
            )
            with pytest.raises(RuntimeError, match=f"fault:{fault}"):
                _activate_manifest(project, project_id, receipt_id)
        failed_publication = _publication_snapshot(project)
        activated = _activate_manifest(project, project_id, receipt_id)
        # Single authority: install-local already published the workspace
        # catalog identity and this project's "installed" enablement row BEFORE
        # activation, so those survive a failed manifest activation (only the
        # enablement flip to "enabled" is rolled back — it never committed). The
        # shared manifest registration itself is idempotent and workspace-owned;
        # it needs no rollback because dispatch is gated by the project's
        # enablement row (still "installed", not "enabled"), not by registry
        # presence. So the failed window's durable state is exactly the
        # installed baseline, not an empty publication.
        expected_failed_publication: Counter[tuple[str, ...]] = Counter(
            [
                ("manifest", GENERIC_BINDINGS_PLUGIN_ID, activated["manifestSha256"]),
                (
                    "install_state",
                    activated["receiptId"],
                    activated["manifestSha256"],
                    activated["packageSha256"],
                    "installed",
                    "manifestLoaded",
                    "0",
                ),
            ]
        )
    else:
        activated = _activate_manifest(project, project_id, receipt_id)
        with monkeypatch.context() as fault_patch:
            _inject_fault(fault_patch, fault)
            with pytest.raises(RuntimeError, match=f"fault:{fault}"):
                _activate_backend(project, project_id)
        failed_publication = _publication_snapshot(project)
        expected_failed_publication = _manifest_only_publication(activated)

    assert activated["registryActivated"] is True
    complete_publication = _complete_publication(activated)
    retried = _activate_backend(project, project_id)
    assert retried["executableHandlersRegistered"] is True
    assert retried["trustedRuntimeBindingsRegistered"] is True
    assert _publication_snapshot(project) == complete_publication

    assert failed_publication == expected_failed_publication


# --- atomicity ---


def _install_row(project: Any) -> tuple[int, str]:
    row = project.db.execute(
        "SELECT executable_handlers_allowed, updated_at "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (GENERIC_BINDINGS_PLUGIN_ID,),
    ).fetchone()
    assert row is not None
    return int(row["executable_handlers_allowed"]), str(row["updated_at"])


def test_failed_reactivation_restores_exact_prior_handlers(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    _activate_backend(project, project_id)
    registry = default_registry()
    prior_job = next(
        item for item in registry.job_handler_specs() if item.kind == ATOMIC_JOB_KIND
    )
    prior_bindings = {
        (binding_type, kind): next(
            item
            for item in registry.runtime_binding_specs(binding_type)
            if item.kind == kind
        )
        for binding_type, kind in _atomic_runtime_bindings()
    }
    prior_matchers = {
        item.matcher_id: item
        for item in registered_matchers()
        if item.origin_plugin_id == GENERIC_BINDINGS_PLUGIN_ID
    }
    prior_install = _install_row(project)

    def replacement(payload: dict[str, Any]) -> dict[str, Any]:
        return {"replacement": True, "payload": payload}

    for key in _atomic_handler_keys():
        register_trusted_backend_handler(key, replacement, replace=True)

    register = registry.register_runtime_binding

    def fail_after_publish(*args: Any, **kwargs: Any) -> None:
        register(*args, **kwargs)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(registry, "register_runtime_binding", fail_after_publish)
    with pytest.raises(RuntimeError, match="PRIMARY"):
        _activate_backend(project, project_id)

    current_job = next(
        item for item in registry.job_handler_specs() if item.kind == ATOMIC_JOB_KIND
    )
    assert current_job is prior_job
    for (binding_type, kind), prior in prior_bindings.items():
        current = next(
            item
            for item in registry.runtime_binding_specs(binding_type)
            if item.kind == kind
        )
        assert current is prior
    current_matchers = {
        item.matcher_id: item
        for item in registered_matchers()
        if item.origin_plugin_id == GENERIC_BINDINGS_PLUGIN_ID
    }
    assert current_matchers == prior_matchers
    assert all(current_matchers[key] is prior_matchers[key] for key in prior_matchers)
    assert _install_row(project) == prior_install


def test_backend_activation_serializes_failure_before_following_success(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    activated = _activate_manifest(project, project_id, receipt_id)
    published = threading.Event()
    release = threading.Event()
    follower_started = threading.Event()
    follower_done = threading.Event()
    failures: list[BaseException] = []
    successes: list[dict[str, Any]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_failing_activation(*args: Any, **kwargs: Any) -> dict[str, list[str]]:
        result = publish(*args, **kwargs)
        if threading.current_thread().name == "failing-activation":
            published.set()
            assert release.wait(5)
            raise RuntimeError("PRIMARY")
        return result

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_failing_activation,
    )

    def fail() -> None:
        try:
            _activate_backend(project, project_id)
        except BaseException as exc:
            failures.append(exc)

    def follow() -> None:
        follower_started.set()
        successes.append(_activate_backend(project, project_id))
        follower_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    failing = threading.Thread(target=fail, name="failing-activation")
    # realtime: Event-gated thread handshake, not a clock race
    follower = threading.Thread(target=follow, name="following-activation")
    failing.start()
    assert published.wait(5)
    follower.start()
    assert follower_started.wait(5)
    assert not follower_done.wait(0.1)
    release.set()
    failing.join(5)
    follower.join(5)

    assert not failing.is_alive()
    assert not follower.is_alive()
    assert len(failures) == 1 and str(failures[0]) == "PRIMARY"
    assert len(successes) == 1
    assert _publication_snapshot(project) == _complete_publication(activated)


def test_manifest_activation_serializes_failed_publisher_and_follower(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    published = threading.Event()
    release = threading.Event()
    follower_started = threading.Event()
    follower_done = threading.Event()
    failures: list[BaseException] = []
    successes: list[dict[str, Any]] = []
    upsert = plugin_runtime._upsert_workbench_plugin_install_state

    def pause_after_manifest(*args: Any, **kwargs: Any) -> None:
        if threading.current_thread().name == "failing-manifest":
            published.set()
            assert release.wait(5)
            raise RuntimeError("PRIMARY")
        upsert(*args, **kwargs)

    monkeypatch.setattr(
        plugin_runtime, "_upsert_workbench_plugin_install_state", pause_after_manifest
    )

    def fail() -> None:
        try:
            _activate_manifest(project, project_id, receipt_id)
        except BaseException as exc:
            failures.append(exc)

    def follow() -> None:
        follower_started.set()
        successes.append(_activate_manifest(project, project_id, receipt_id))
        follower_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    failing = threading.Thread(target=fail, name="failing-manifest")
    # realtime: Event-gated thread handshake, not a clock race
    follower = threading.Thread(target=follow, name="following-manifest")
    failing.start()
    assert published.wait(5)
    follower.start()
    assert follower_started.wait(5)
    assert not follower_done.wait(0.1)
    release.set()
    failing.join(5)
    follower.join(5)

    assert not failing.is_alive()
    assert not follower.is_alive()
    assert len(failures) == 1 and str(failures[0]) == "PRIMARY"
    assert len(successes) == 1
    assert any(
        item.manifest.id == GENERIC_BINDINGS_PLUGIN_ID
        for item in default_registry().plugin_manifests()
    )
    row = project.db.execute(
        "SELECT install_state FROM workbench_plugin_installs WHERE plugin_id=?",
        (GENERIC_BINDINGS_PLUGIN_ID,),
    ).fetchone()
    assert row is not None and row["install_state"] == "enabled"


@pytest.mark.parametrize(
    ("lifecycle", "expected_state"),
    (
        (plugin_runtime.disable_workbench_plugin, "disabled"),
        (plugin_runtime.uninstall_workbench_plugin, "uninstalled"),
    ),
)
def test_backend_activation_serializes_lifecycle_unload(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: Any,
    expected_state: str,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    published = threading.Event()
    release = threading.Event()
    lifecycle_started = threading.Event()
    lifecycle_done = threading.Event()
    backend_results: list[dict[str, Any]] = []
    lifecycle_results: list[dict[str, Any]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_after_metadata(*args: Any, **kwargs: Any) -> dict[str, list[str]]:
        result = publish(*args, **kwargs)
        published.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_after_metadata,
    )

    def activate() -> None:
        backend_results.append(_activate_backend(project, project_id))

    def mutate() -> None:
        lifecycle_started.set()
        extra = (
            {"workspace_projects": (project,)}
            if lifecycle is plugin_runtime.uninstall_workbench_plugin
            else {}
        )
        lifecycle_results.append(
            lifecycle(
                project,
                project_id=project_id,
                plugin_id=GENERIC_BINDINGS_PLUGIN_ID,
                **extra,
            )
        )
        lifecycle_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    activating = threading.Thread(target=activate)
    # realtime: Event-gated thread handshake, not a clock race
    mutating = threading.Thread(target=mutate)
    activating.start()
    assert published.wait(5)
    mutating.start()
    assert lifecycle_started.wait(5)
    assert not lifecycle_done.wait(0.1)
    release.set()
    activating.join(5)
    mutating.join(5)

    assert not activating.is_alive()
    assert not mutating.is_alive()
    assert len(backend_results) == 1
    assert len(lifecycle_results) == 1
    snapshot = _publication_snapshot(project)
    install = next(item for item in snapshot if item[0] == "install_state")
    # The lifecycle transition serialized behind the activation lock and applied.
    assert install[4] == expected_state
    # Disable is project-local; uninstall deletes the workspace package and all
    # derived registry entries.
    if lifecycle is plugin_runtime.disable_workbench_plugin:
        assert ("manifest", GENERIC_BINDINGS_PLUGIN_ID, install[2]) in snapshot
    else:
        assert not [item for item in snapshot if item[0] == "manifest"]
        assert not [item for item in snapshot if item[0] == "matcher"]
    assert not [item for item in snapshot if item[0] == "runtime_dispatch"]


def test_backend_failure_serializes_local_install_state_commit(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    sentinel = "2001-02-03 04:05:06"
    project.db.execute(
        "UPDATE workbench_plugin_installs SET updated_at=? WHERE plugin_id=?",
        (sentinel, GENERIC_BINDINGS_PLUGIN_ID),
    )
    project.db.commit()
    published = threading.Event()
    release = threading.Event()
    plugin_load_done = threading.Event()
    install_done = threading.Event()
    backend_failures: list[BaseException] = []
    install_failures: list[BaseException] = []
    install_results: list[tuple[int, dict[str, Any]]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_then_fail(*args: Any, **kwargs: Any) -> None:
        publish(*args, **kwargs)
        published.set()
        assert release.wait(5)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_then_fail,
    )

    run_action_spec = executor.run_action_spec

    def track_plugin_load(*args: Any, **kwargs: Any) -> Any:
        result = run_action_spec(*args, **kwargs)
        plugin_load_done.set()
        return result

    monkeypatch.setattr(executor, "run_action_spec", track_plugin_load)

    def activate() -> None:
        try:
            _activate_backend(project, project_id)
        except BaseException as exc:
            backend_failures.append(exc)

    def install() -> None:
        try:
            install_results.append(
                plugin_runtime.execute_workbench_plugin_local_install_plan(
                    project,
                    project_id=project_id,
                    plugin_id=GENERIC_BINDINGS_PLUGIN_ID,
                    source={
                        "kind": "localPath",
                        "value": str(
                            tmp_path
                            / "plugin_packages"
                            / "activation-atomicity"
                            / "plugin.json"
                        ),
                    },
                    arbitrary_package_load_allowed=False,
                )
            )
        except BaseException as exc:
            install_failures.append(exc)
        finally:
            install_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    activating = threading.Thread(target=activate)
    # realtime: Event-gated thread handshake, not a clock race
    installing = threading.Thread(target=install)
    activating.start()
    assert published.wait(5)
    installing.start()
    # Package validation and its catalog commit share one lifecycle boundary;
    # an install may not stage through plugin.load while backend publication is
    # still inside that boundary.
    assert not plugin_load_done.wait(0.1)
    assert not install_done.wait(0.1)
    release.set()
    activating.join(5)
    installing.join(5)

    assert not activating.is_alive()
    assert not installing.is_alive()
    assert plugin_load_done.is_set()
    assert len(backend_failures) == 1 and str(backend_failures[0]) == "PRIMARY"
    assert install_failures == []
    assert len(install_results) == 1 and install_results[0][0] == 200
    row = project.db.execute(
        "SELECT install_state, activation, updated_at "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (GENERIC_BINDINGS_PLUGIN_ID,),
    ).fetchone()
    assert row is not None
    assert (row["install_state"], row["activation"]) == (
        "installed",
        "manifestLoaded",
    )
    assert row["updated_at"] != sentinel


def test_cleanup_failures_do_not_mask_primary_or_skip_later_cleanup(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    calls: list[str] = []

    def primary(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("PRIMARY")

    def cleanup_failure(*_args: Any, **_kwargs: Any) -> None:
        calls.append("grant")
        raise RuntimeError("CLEANUP")

    def contribution_cleanup(*_args: Any, **_kwargs: Any) -> None:
        calls.append("contributions")

    registry = default_registry()
    restore_manifest = registry._restore_plugin_manifest

    def manifest_cleanup(*args: Any, **kwargs: Any) -> None:
        calls.append("manifest")
        restore_manifest(*args, **kwargs)

    monkeypatch.setattr(
        plugin_runtime, "_register_backend_contribution_metadata", primary
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "_restore_backend_activation_executable_handlers_grant",
        cleanup_failure,
    )
    monkeypatch.setattr(
        plugin_runtime, "_restore_backend_contributions", contribution_cleanup
    )
    monkeypatch.setattr(registry, "_restore_plugin_manifest", manifest_cleanup)

    with pytest.raises(RuntimeError, match="PRIMARY") as caught:
        _activate_backend(project, project_id)

    assert calls == ["grant", "contributions", "manifest"]
    assert any("CLEANUP" in note for note in getattr(caught.value, "__notes__", ()))


def test_grant_rollback_restores_value_and_timestamp_exactly(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    project.db.execute(
        "UPDATE workbench_plugin_installs "
        "SET executable_handlers_allowed=0, updated_at='2001-02-03 04:05:06' "
        "WHERE plugin_id=?",
        (GENERIC_BINDINGS_PLUGIN_ID,),
    )
    project.db.commit()
    prior = _install_row(project)

    plugin_runtime.activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=GENERIC_BINDINGS_PLUGIN_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=False,
    )
    assert _install_row(project) == prior

    record = plugin_runtime._record_backend_activation_executable_handlers_grant

    def fail_after_commit(*args: Any, **kwargs: Any) -> None:
        record(*args, **kwargs)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(
        plugin_runtime,
        "_record_backend_activation_executable_handlers_grant",
        fail_after_commit,
    )
    with pytest.raises(RuntimeError, match="PRIMARY"):
        _activate_backend(project, project_id)

    assert _install_row(project) == prior


# --- trusted handlers ---

TRUSTED_HANDLERS_PLUGIN_ID = "frisket-handler-demo"
JOB_KIND = "frisket.handler_demo.job"
HANDLER_KEY = f"{TRUSTED_HANDLERS_PLUGIN_ID}:handler_demo.echo"
UNDECLARED_HANDLER_KEY = "frisket-undeclared-handler-kind:handler_demo.echo"


def _trusted_handlers_write_manifest(
    tmp_path: Path,
    *,
    plugin_id: str = TRUSTED_HANDLERS_PLUGIN_ID,
    job_kind: str = JOB_KIND,
    handler_key: str = HANDLER_KEY,
) -> Path:
    package_dir = tmp_path / plugin_id
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / "plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": "0.1.0",
                "contributes": {
                    "actions": [],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [job_kind],
                },
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
                "runtime": {
                    "job_handlers": [{"kind": job_kind, "handler_key": handler_key}]
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _plugin_load_action(manifest_path: Path) -> dict[str, Any]:
    return {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        "params": {"manifest": {"kind": "local_file", "path": str(manifest_path)}},
        "idempotency_key": f"trusted-job-handler-bindings@{manifest_path.name}",
    }


def _trusted_handlers_load_and_enable(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> tuple[str, str]:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    return receipt_id, activation_response.json()["manifestSha256"]


def test_trusted_backend_activation_binds_declared_job_handler_without_package_loading(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    register_trusted_backend_handler(HANDLER_KEY, _trusted_handler)
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Trusted job handler bindings"}
        ).json()["id"]
        manifest_path = _trusted_handlers_write_manifest(tmp_path)
        receipt_id, manifest_sha = _trusted_handlers_load_and_enable(
            client, project_id, manifest_path, plugin_id=TRUSTED_HANDLERS_PLUGIN_ID
        )

        metadata_only = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{TRUSTED_HANDLERS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert metadata_only.status_code == 200, metadata_only.text
        assert metadata_only.json()["executableHandlersRegistered"] is False
        assert metadata_only.json()["registeredExecutableHandlers"] == {
            "jobHandlers": []
        }
        metadata_spec = next(
            spec
            for spec in default_registry().job_handler_specs()
            if spec.kind == JOB_KIND
        )
        assert metadata_spec.handler is None

        executable = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{TRUSTED_HANDLERS_PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert executable.status_code == 200, executable.text
        body = executable.json()
        assert body["schemaVersion"] == "frisket.workbench_plugin_backend_activation.v1"
        assert body["projectId"] == project_id
        assert body["pluginId"] == TRUSTED_HANDLERS_PLUGIN_ID
        assert body["receiptId"] == receipt_id
        assert body["manifestSha256"] == manifest_sha
        assert body["arbitraryPackageLoadAllowed"] is False
        assert body["executableHandlersRegistered"] is True
        assert body["registeredExecutableHandlers"] == {"jobHandlers": [JOB_KIND]}

        worker_handlers = HandlerRegistry()
        default_registry().install_job_handlers(worker_handlers)
        handler = worker_handlers.get(JOB_KIND)
        assert handler is not None
        assert handler({"row": 7}, JobHandlerContext.without_job_row()) == {
            "handled": True,
            "payload": {"row": 7},
        }
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_trusted_backend_activation_rejects_unknown_or_undeclared_handler_bindings(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Rejected handler bindings"}
        ).json()["id"]

        unknown_key_manifest = _trusted_handlers_write_manifest(
            tmp_path,
            plugin_id="frisket-unknown-handler-key",
            job_kind="frisket.unknown.job",
            handler_key="frisket-unknown-handler-key:no_such_key",
        )
        _trusted_handlers_load_and_enable(
            client,
            project_id,
            unknown_key_manifest,
            plugin_id="frisket-unknown-handler-key",
        )
        unknown_key = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-unknown-handler-key/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert unknown_key.status_code == 409, unknown_key.text
        assert (
            unknown_key.json()["detail"]["code"]
            == "plugin_backend_activation_handler_key_unknown"
        )

        register_trusted_backend_handler(UNDECLARED_HANDLER_KEY, _trusted_handler)
        undeclared_manifest = _trusted_handlers_write_manifest(
            tmp_path,
            plugin_id="frisket-undeclared-handler-kind",
            job_kind="frisket.declared.job",
            handler_key=UNDECLARED_HANDLER_KEY,
        )
        payload = json.loads(undeclared_manifest.read_text(encoding="utf-8"))
        payload["runtime"]["job_handlers"][0]["kind"] = "frisket.not_declared.job"
        undeclared_manifest.write_text(json.dumps(payload), encoding="utf-8")
        _trusted_handlers_load_and_enable(
            client,
            project_id,
            undeclared_manifest,
            plugin_id="frisket-undeclared-handler-kind",
        )
        undeclared = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-undeclared-handler-kind/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert undeclared.status_code == 409, undeclared.text
        assert (
            undeclared.json()["detail"]["code"]
            == "plugin_backend_activation_handler_binding_invalid"
        )
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        unregister_trusted_backend_handler(UNDECLARED_HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_runtime_handler_bindings_require_trusted_capability_and_namespaced_keys(
    tmp_path: Path,
    client,
) -> None:
    _reset_default_registry_for_tests()
    with pytest.raises(ValueError, match="namespaced"):
        register_trusted_backend_handler("not-namespaced", _trusted_handler)
    for invalid_prefix in [
        "Frisket.Geo",
        "frisket geo",
        "frisket/geo",
        "frisket..geo",
        ".frisket.geo",
        "frisket.geo.",
        "-frisket",
        "frisket-",
        "frisket._geo",
    ]:
        with pytest.raises(ValueError, match="canonical plugin id"):
            register_trusted_backend_handler(
                f"{invalid_prefix}:handler_demo.echo", _trusted_handler
            )

    project_id = client.post(
        "/api/projects", json={"name": "Runtime binding capability gate"}
    ).json()["id"]
    manifest_path = _trusted_handlers_write_manifest(
        tmp_path, plugin_id="frisket-no-runtime-capability"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["requires"]["capabilities"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    load_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_plugin_load_action(manifest_path),
    )
    assert load_response.status_code == 400, load_response.text
    load_result = ActionResult.model_validate(load_response.json())
    assert load_result.status == "failed"
    assert len(load_result.errors) == 1
    assert load_result.errors[0].code == "invalid_plugin_manifest"
    assert load_result.errors[0].field == "params.manifest.runtime.job_handlers"


# --- runtime isolation ---

ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
RUNTIME_ISOLATION_PLUGIN_ID = "frisket.geosmoke"
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


def _await_spawned_start_up(child: Any, started: Any) -> None:
    """A spawn child signals only after a fresh interpreter has imported this
    module's server graph, which takes tens of seconds on a loaded host. That
    start-up is not under test: the bound is a failure bound only, and a child
    that dies first fails at once."""

    def booted() -> bool:
        if started.is_set():
            return True
        if not child.is_alive():
            raise AssertionError(
                f"spawned child exited during start-up (exit {child.exitcode})"
            )
        return False

    with controlled_time(timeout=120) as boot:
        boot.wait_until(booted, message="spawned child never finished start-up")


def _child_hold_plugin_lifecycle_lock(workspace_root: str, acquired: Any) -> None:

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
        f"/api/projects/{project_id}/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert install_response.status_code == 200, install_response.text
    receipt_id = install_response.json()["receiptId"]
    assert receipt_id

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/activate",
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
        f"/api/projects/{project_id}/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/backend/activate",
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
        if plugin["pluginId"] == RUNTIME_ISOLATION_PLUGIN_ID
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
    project: Project,
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
    project: Project,
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
    make_project,
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
        project_a = make_project("project-a")
        project_b = make_project("project-b")
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
    make_project,
) -> None:

    _reset_default_registry_for_tests()
    try:
        project_a = make_project("project-a")
        project_b = make_project("project-b")
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
    project: Project,
) -> None:
    removed_type = "frisket_runtime_package_scope_record"
    _reset_default_registry_for_tests()
    try:
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
    tmp_path: Path, make_project, monkeypatch: pytest.MonkeyPatch
) -> None:

    _reset_default_registry_for_tests()
    try:
        project_a = make_project("project-a")
        project_b = make_project("project-b")
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
        _await_spawned_start_up(child, ready)

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
        _await_spawned_start_up(holder, acquired)

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
            f"/api/projects/project-a/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/disable"
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
            f"/api/projects/project-a/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/uninstall"
        )
        assert uninstalled_a.status_code == 200, uninstalled_a.text
        assert uninstalled_a.json()["installState"] == "uninstalled"
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is None
        b_index = client.get("/api/projects/project-b/workbench/plugins")
        assert b_index.status_code == 200, b_index.text
        assert RUNTIME_ISOLATION_PLUGIN_ID not in {
            item["pluginId"] for item in b_index.json()["plugins"]
        }
        with closing(Project(workspace / "project-b.frisket")) as project_b:
            b_row = project_b.db.execute(
                "SELECT install_state, permissions_accepted, "
                "executable_handlers_allowed FROM workbench_plugin_installs "
                "WHERE plugin_id=?",
                (RUNTIME_ISOLATION_PLUGIN_ID,),
            ).fetchone()
            assert b_row is not None
            assert b_row["install_state"] == "uninstalled"
            assert b_row["permissions_accepted"] == "[]"
            assert b_row["executable_handlers_allowed"] == 0
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
            f"/api/projects/project-a/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/disable"
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
            f"/api/projects/project-b/workbench/plugins/{RUNTIME_ISOLATION_PLUGIN_ID}/disable"
        )
        assert disabled_b.status_code == 200, disabled_b.text
        assert disabled_b.json()["installState"] == "disabled"
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None
    finally:
        _reset_default_registry_for_tests()

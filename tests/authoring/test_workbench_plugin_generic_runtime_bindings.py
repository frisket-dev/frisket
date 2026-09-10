from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring import column_types
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring.workbench import plugin_runtime
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    project_runtime_binding,
)
from frisket.engine.jobs import HandlerRegistry
from frisket.engine.store import Project
from frisket.features.url_classification import (
    classify_url,
    plugin_matchers,
    registered_matchers,
)
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "frisket-runtime-demo"
ACTION_KIND = f"{PLUGIN_ID}.demo_action"
IMPORTER_KIND = "frisket_runtime_demo_import"
OPERATOR_KIND = "frisket.runtime_demo.operator"
PROJECTION_KIND = "frisket.runtime_demo.projection"
ATOMIC_COLUMN_TYPE = "frisket_runtime_demo_atomic_record"
ATOMIC_JOB_KIND = "frisket.runtime_demo.atomic_job"
ATOMIC_JOB_HANDLER_KEY = f"{PLUGIN_ID}:atomic_job"
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
    return f"{PLUGIN_ID}:{kind}"


def _write_manifest(
    tmp_path: Path,
    *,
    name: str = PLUGIN_ID,
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
    plugin_id = PLUGIN_ID
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
    from frisket.plugins.manifest_generate import load_generation_source

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
                "id": PLUGIN_ID,
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


def _load_and_enable(
    client: TestClient, project_id: str, manifest_path: Path
) -> tuple[str, str]:
    receipt_id = _load_manifest(client, project_id, manifest_path)

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
    return receipt_id, activation_response.json()["manifestSha256"]


def _load_manifest(client: TestClient, project_id: str, manifest_path: Path) -> str:
    # Install-local writes the workspace catalog package identity
    # and this project's "installed" enablement row, and returns the catalog
    # receipt id used to bind a later activation.
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
        manifest_path = _write_manifest(tmp_path)
        receipt_id, manifest_sha = _load_and_enable(client, project_id, manifest_path)

        metadata_only = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
        assert body["pluginId"] == PLUGIN_ID
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

        unknown_manifest = _write_manifest(
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
        _load_and_enable(client, project_id, unknown_manifest)
        unknown = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
        undeclared_manifest = _write_manifest(
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
        _load_and_enable(client, project_id, undeclared_manifest)
        undeclared = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
        manifest_path = _write_manifest(
            tmp_path,
            name="runtime-binding-collision",
            runtime_override={
                "actions": [{"kind": ACTION_KIND, "handler_key": key}],
                "importers": [],
                "operators": [],
                "projections": [],
            },
        )
        _load_and_enable(client, project_id, manifest_path)

        response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
    return _write_manifest(
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
    plugin_matchers.unregister_plugin_matchers(PLUGIN_ID)
    default_registry().unload_plugin_entries(PLUGIN_ID)
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
        plugin_id=PLUGIN_ID,
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
    classification = classify_url(ATOMIC_MATCHER_URL, enabled_plugin_ids={PLUGIN_ID})
    # Enablement (install_state/activation/grant) is the project row's; package
    # identity (receipt/manifest/package sha) is the workspace catalog's.
    # Merge both so the snapshot still detects partial state.
    install = project.db.execute(
        "SELECT install_state, activation, executable_handlers_allowed "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (PLUGIN_ID,),
    ).fetchone()
    catalog_entry = plugin_package_catalog_for_project(project).get(PLUGIN_ID)
    items: list[tuple[str, ...]] = [
        ("manifest", item.manifest.id, item.sha256)
        for item in registry.plugin_manifests()
        if item.manifest.id == PLUGIN_ID
    ]
    column_type = column_types.get_column_type(ATOMIC_COLUMN_TYPE)
    if column_type is not None and column_type.plugin == PLUGIN_ID:
        items.append(("column_type", ATOMIC_COLUMN_TYPE))
    items.extend(
        ("importer", item.name)
        for item in registry.importer_specs()
        if item.plugin == PLUGIN_ID
    )
    items.extend(
        ("job_handler", item.kind)
        for item in registry.job_handler_specs()
        if item.plugin == PLUGIN_ID
    )
    if worker_handlers.get(ATOMIC_JOB_KIND) is not None:
        items.append(("executable_job_handler", ATOMIC_JOB_KIND))
    items.extend(
        ("matcher", item.matcher_id)
        for item in registered_matchers()
        if item.origin_plugin_id == PLUGIN_ID
    )
    if classification is not None and classification.origin_plugin_id == PLUGIN_ID:
        items.append(("matcher_dispatch", classification.matcher_id))
    for binding_type, kind in _atomic_runtime_bindings():
        items.extend(
            ("runtime_binding", binding_type, item.kind)
            for item in registry.runtime_binding_specs(binding_type)
            if item.plugin == PLUGIN_ID
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
        items.append(("executable_grant", PLUGIN_ID))
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
            ("manifest", PLUGIN_ID, activated["manifestSha256"]),
            _install_state_publication(activated, executable_handlers_allowed=False),
        ]
    )


def _complete_publication(activated: dict[str, Any]) -> Counter[tuple[str, ...]]:
    return Counter(
        [
            ("manifest", PLUGIN_ID, activated["manifestSha256"]),
            _install_state_publication(activated, executable_handlers_allowed=True),
            ("column_type", ATOMIC_COLUMN_TYPE),
            ("importer", IMPORTER_KIND),
            ("job_handler", ATOMIC_JOB_KIND),
            ("executable_job_handler", ATOMIC_JOB_KIND),
            ("matcher", ATOMIC_MATCHER_ID),
            ("matcher_dispatch", ATOMIC_MATCHER_ID),
            ("executable_grant", PLUGIN_ID),
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
        plugin_id=PLUGIN_ID,
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
                ("manifest", PLUGIN_ID, activated["manifestSha256"]),
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

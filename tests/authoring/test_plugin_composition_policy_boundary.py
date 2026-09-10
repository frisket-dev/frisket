"""Boundary proofs for the plugin composition policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
    bootstrap_project_bundled_plugins,
    disable_workbench_plugin,
    execute_workbench_plugin_local_install_plan,
    ensure_workspace_plugin_packages,
    uninstall_workbench_plugin,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    enabled_workbench_plugin_ids,
    project_runtime_binding,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    PluginCompositionPolicy,
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
    install_plugin_composition_policy,
)
from frisket.authoring.workbench.plugin_runtime_settings import (
    delete_workbench_plugin_env_var,
    list_workbench_plugin_env_vars,
    list_workbench_plugin_settings,
    patch_workbench_plugin_settings,
    set_workbench_plugin_env_var,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _shipped_bundled_package_identity,
    _workbench_plugin_install_state,
    catalog_entry_is_reviewed_bundled,
    workbench_plugin_frontend_component_module,
    workbench_plugin_runtime_index,
)
from frisket.engine.store.project import Project

GEO = "frisket.geo"
GEO_VIEW = "frisket.geo.view.map"
GEO_PROJECTION = "frisket.geo.projection.map_points"

OTHER_SHIPPED_BUNDLE = "frisket.opencorporates"
OTHER_SHIPPED_BUNDLE_ACTION = "frisket.opencorporates.match_company"

ZERO_POLICY = PluginCompositionPolicy(
    allowed_bundled_ids=frozenset(), nonbundled_enabled=False
)
SPECIFIC_POLICY_OMITTING_GEO = PluginCompositionPolicy(
    allowed_bundled_ids=frozenset({OTHER_SHIPPED_BUNDLE}), nonbundled_enabled=True
)


def _seed_geo_under_default_policy(tmp_path: Path) -> tuple[Project, str]:
    project = Project.create(tmp_path / "proj.frisket", name="proj")
    ensure_workspace_plugin_packages(tmp_path)
    bootstrap_project_bundled_plugins(project, project_id="proj")
    index = workbench_plugin_runtime_index(project, project_id="proj")
    seeded = {p["pluginId"] for p in index["plugins"]}
    assert GEO in seeded, seeded
    assert GEO in enabled_workbench_plugin_ids(project)
    assert GEO in {m.manifest.id for m in default_registry().plugin_manifests()}
    entry = plugin_package_catalog_for_project(project).get(GEO)
    assert entry is not None
    return project, str(entry["receipt_id"])


def _assert_geo_fully_excluded(project: Project, receipt_id: str) -> None:
    index = workbench_plugin_runtime_index(project, project_id="proj")
    assert GEO not in {p["pluginId"] for p in index["plugins"]}

    assert GEO not in enabled_workbench_plugin_ids(project)
    assert (
        project_runtime_binding(
            project, binding_type="projections", kind=GEO_PROJECTION
        )
        is None
    )

    with pytest.raises(WorkbenchPluginActivationError) as frontend_err:
        workbench_plugin_frontend_component_module(
            project, plugin_id=GEO, contribution_id=GEO_VIEW
        )
    assert "not_available" in frontend_err.value.code

    with pytest.raises(WorkbenchPluginActivationError) as manifest_err:
        activate_workbench_plugin_manifest(
            project,
            project_id="proj",
            plugin_id=GEO,
            receipt_id=receipt_id,
            trust_acknowledged=True,
            permissions_accepted=[],
            arbitrary_package_load_allowed=False,
        )
    assert "not_available" in manifest_err.value.code

    with pytest.raises(WorkbenchPluginActivationError) as backend_err:
        activate_workbench_plugin_backend_contributions(
            project,
            project_id="proj",
            plugin_id=GEO,
            trust_acknowledged=True,
            arbitrary_package_load_allowed=False,
            executable_handlers_allowed=True,
        )
    assert "not_available" in backend_err.value.code

    settings_calls = (
        lambda: list_workbench_plugin_env_vars(
            project, project_id="proj", plugin_id=GEO
        ),
        lambda: set_workbench_plugin_env_var(
            project,
            project_id="proj",
            plugin_id=GEO,
            name="PLUGIN_TEST_KEY",
            value="value",
        ),
        lambda: delete_workbench_plugin_env_var(
            project,
            project_id="proj",
            plugin_id=GEO,
            name="PLUGIN_TEST_KEY",
        ),
        lambda: list_workbench_plugin_settings(
            project, project_id="proj", plugin_id=GEO
        ),
        lambda: patch_workbench_plugin_settings(
            project, project_id="proj", plugin_id=GEO, values={}
        ),
    )
    for access in settings_calls:
        with pytest.raises(WorkbenchPluginLifecycleError) as settings_err:
            access()
        assert "not_available" in settings_err.value.code

    original_state = dict(_workbench_plugin_install_state(project, plugin_id=GEO) or {})
    lifecycle_calls = (
        lambda: disable_workbench_plugin(project, project_id="proj", plugin_id=GEO),
        lambda: uninstall_workbench_plugin(
            project,
            project_id="proj",
            plugin_id=GEO,
            workspace_projects=(project,),
        ),
    )
    for mutate in lifecycle_calls:
        with pytest.raises(WorkbenchPluginLifecycleError) as lifecycle_err:
            mutate()
        assert "not_available" in lifecycle_err.value.code
        assert _workbench_plugin_install_state(project, plugin_id=GEO) == original_state


@pytest.mark.real_bundled_plugins
@pytest.mark.parametrize(
    "policy", [ZERO_POLICY, SPECIFIC_POLICY_OMITTING_GEO], ids=["zero", "selected"]
)
def test_persisted_bundle_is_inaccessible_when_policy_omits_it(
    tmp_path, policy: PluginCompositionPolicy
) -> None:
    project, receipt_id = _seed_geo_under_default_policy(tmp_path)
    install_plugin_composition_policy(policy)
    _assert_geo_fully_excluded(project, receipt_id)


@pytest.mark.real_bundled_plugins
def test_settings_mutation_checks_reviewed_package_once(tmp_path, monkeypatch) -> None:
    project, _ = _seed_geo_under_default_policy(tmp_path)
    install_plugin_composition_policy(
        PluginCompositionPolicy(
            allowed_bundled_ids=frozenset({GEO}), nonbundled_enabled=False
        )
    )
    from frisket.authoring.workbench import plugin_runtime_status

    original = plugin_runtime_status._shipped_bundled_package_identity
    calls = 0

    def counted(plugin_id: str):
        nonlocal calls
        calls += 1
        return original(plugin_id)

    monkeypatch.setattr(
        plugin_runtime_status, "_shipped_bundled_package_identity", counted
    )
    patch_workbench_plugin_settings(
        project, project_id="proj", plugin_id=GEO, values={}
    )
    assert calls == 1


@pytest.mark.real_bundled_plugins
def test_reviewed_bundled_identity_needs_trusted_source_and_matching_sha(
    tmp_path,
) -> None:
    shipped = _shipped_bundled_package_identity(GEO)
    assert shipped is not None
    manifest_sha, package_sha = shipped
    state = {
        "install_source": {"kind": "bundled", "value": GEO},
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
    }
    assert catalog_entry_is_reviewed_bundled(state)
    assert not catalog_entry_is_reviewed_bundled(
        {**state, "package_sha256": "sha256:" + "0" * 64}
    )
    assert not catalog_entry_is_reviewed_bundled(
        {**state, "install_source": {"kind": "localPath", "value": "/tmp/evil"}}
    )


@pytest.mark.real_bundled_plugins
def test_workspace_uninstall_suppresses_bundled_reseed_after_restart(tmp_path) -> None:
    project, _ = _seed_geo_under_default_policy(tmp_path)
    uninstall_workbench_plugin(
        project,
        project_id="proj",
        plugin_id=GEO,
        workspace_projects=(project,),
    )
    catalog = plugin_package_catalog_for_project(project)
    assert catalog.get(GEO) is None
    assert catalog.is_deleted(GEO)

    _reset_default_registry_for_tests()
    ensure_workspace_plugin_packages(tmp_path)
    assert catalog.get(GEO) is None
    assert GEO not in {
        loaded.manifest.id for loaded in default_registry().plugin_manifests()
    }


@pytest.mark.real_bundled_plugins
def test_workspace_startup_is_the_only_catalog_ensure_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.authoring.workbench import plugin_runtime
    from frisket.server.workspace import Workspace

    calls: list[Path] = []
    original = plugin_runtime.ensure_workspace_plugin_packages

    def counted(root: str | Path, **kwargs):
        calls.append(Path(root))
        return original(root, **kwargs)

    monkeypatch.setattr(plugin_runtime, "ensure_workspace_plugin_packages", counted)
    workspace = Workspace(tmp_path / "workspace")
    assert calls == [workspace.root]
    created = workspace.create("catalog owner")
    project = workspace.get(created["id"])
    workbench_plugin_runtime_index(project, project_id=created["id"])
    bootstrap_project_bundled_plugins(project, project_id=created["id"])
    assert calls == [workspace.root]


@pytest.mark.real_bundled_plugins
def test_modified_local_bytes_under_allowed_bundled_id_are_refused(
    tmp_path,
) -> None:
    install_plugin_composition_policy(
        PluginCompositionPolicy(
            allowed_bundled_ids=frozenset({GEO}), nonbundled_enabled=False
        )
    )
    project = Project.create(tmp_path / "proj.frisket", name="proj")

    from frisket.authoring.workbench.plugin_runtime_status import _bundled_plugins_root

    modified = tmp_path / "modified-geo"
    shutil.copytree(_bundled_plugins_root() / GEO, modified)
    (modified / "plugin.py").write_text(
        (modified / "plugin.py").read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )

    status, payload = execute_workbench_plugin_local_install_plan(
        project,
        project_id="proj",
        plugin_id=GEO,
        source={"kind": "localPath", "value": str(modified)},
        arbitrary_package_load_allowed=False,
    )
    assert status == 403, payload
    assert (
        payload["installFailure"]["code"]
        == "plugin_install_not_available_under_composition_policy"
    )
    assert GEO not in enabled_workbench_plugin_ids(project)


def _has_workbench_plugin_route(app) -> bool:
    return any(
        "workbench/plugins" in getattr(route, "path", "") for route in app.routes
    )


def test_zero_policy_registers_no_workbench_routes_and_reports_unavailable(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    app = create_app(tmp_path / "ws", plugin_composition_policy=ZERO_POLICY)
    assert not _has_workbench_plugin_route(app)
    assert not _registered_plugin_route_pairs(app)
    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["plugins_available"] is False
        assert config["plugin_management_available"] is False


@pytest.mark.real_bundled_plugins
def test_specific_policy_keeps_routes_and_reports_available(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    app = create_app(
        tmp_path / "ws", plugin_composition_policy=SPECIFIC_POLICY_OMITTING_GEO
    )
    assert _has_workbench_plugin_route(app)
    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["plugins_available"] is True
        assert config["plugin_management_available"] is True


BUNDLED_ONLY_ROUTES = {
    ("GET", "/api/projects/{pid}/workbench/plugins"),
    (
        "GET",
        "/api/projects/{pid}/workbench/plugins/{plugin_id}"
        "/frontend-components/{contribution_id}/module.js",
    ),
}
NONBUNDLED_ROUTES = {
    ("GET", "/api/projects/{pid}/workbench/plugins/{plugin_id}/env"),
    ("GET", "/api/projects/{pid}/workbench/plugins/{plugin_id}/settings"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/activate"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/backend/activate"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/disable"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/uninstall"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/env"),
    ("DELETE", "/api/projects/{pid}/workbench/plugins/{plugin_id}/env/{name}"),
    ("PATCH", "/api/projects/{pid}/workbench/plugins/{plugin_id}/settings"),
    ("GET", "/api/projects/{pid}/workbench/marketplace"),
    ("POST", "/api/projects/{pid}/workbench/marketplace/install-attempt"),
    ("POST", "/api/projects/{pid}/workbench/plugins/{plugin_id}/install-local"),
}


def _registered_route_pairs(app) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        pairs.update((method, path) for method in methods)
    return pairs


def _registered_plugin_route_pairs(app) -> set[tuple[str, str]]:
    return {
        pair
        for pair in _registered_route_pairs(app)
        if "/workbench/plugins" in pair[1] or "/workbench/marketplace" in pair[1]
    }


@pytest.mark.real_bundled_plugins
def test_bundled_only_registers_exact_index_and_frontend_route_set(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    policy = PluginCompositionPolicy(
        allowed_bundled_ids=frozenset({GEO}), nonbundled_enabled=False
    )
    app = create_app(tmp_path / "ws", plugin_composition_policy=policy)
    assert _registered_plugin_route_pairs(app) == BUNDLED_ONLY_ROUTES

    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["plugins_available"] is True
        assert config["plugin_management_available"] is False
        created = client.post("/api/projects", json={"name": "selected bundle"})
        assert created.status_code == 200, created.text
        project_id = created.json()["id"]
        index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index.status_code == 200, index.text
        geo = next(
            plugin for plugin in index.json()["plugins"] if plugin["pluginId"] == GEO
        )
        module_url = next(
            binding["moduleUrl"]
            for binding in geo["frontendComponentBindings"]
            if binding["contributionId"] == GEO_VIEW
        )
        module = client.get(module_url)
        assert module.status_code == 200, module.text
        assert module.headers["content-type"].startswith("application/javascript")


@pytest.mark.real_bundled_plugins
def test_bundled_only_backend_activation_is_absent_and_cannot_bind(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    policy = PluginCompositionPolicy(
        allowed_bundled_ids=frozenset({OTHER_SHIPPED_BUNDLE}), nonbundled_enabled=False
    )
    app = create_app(tmp_path / "ws", plugin_composition_policy=policy)
    with TestClient(app) as client:
        created = client.post("/api/projects", json={"name": "selected bundle"})
        assert created.status_code == 200, created.text
        project_id = created.json()["id"]

        index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index.status_code == 200, index.text
        opencorporates = next(
            plugin
            for plugin in index.json()["plugins"]
            if plugin["pluginId"] == OTHER_SHIPPED_BUNDLE
        )
        assert opencorporates["installState"] == "installed"
        project = app.state.workspace.get(project_id)
        assert (
            project_runtime_binding(
                project,
                binding_type="actions",
                kind=OTHER_SHIPPED_BUNDLE_ACTION,
            )
            is None
        )

        activation = client.post(
            f"/api/projects/{project_id}/workbench/plugins/"
            f"{OTHER_SHIPPED_BUNDLE}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert activation.status_code == 404, activation.text
        assert (
            project_runtime_binding(
                project,
                binding_type="actions",
                kind=OTHER_SHIPPED_BUNDLE_ACTION,
            )
            is None
        )


def test_nonbundled_on_registers_every_route(tmp_path) -> None:
    from frisket.server.app import create_app

    app = create_app(tmp_path / "ws")  # default policy: nonbundled_enabled=True
    assert _registered_plugin_route_pairs(app) == (
        BUNDLED_ONLY_ROUTES | NONBUNDLED_ROUTES
    )


def test_install_plugin_composition_policy_rejects_unknown_bundled_id() -> None:
    from frisket.authoring.workbench.plugin_runtime_shared import (
        UnknownBundledPluginIdsError,
    )

    with pytest.raises(UnknownBundledPluginIdsError) as excinfo:
        install_plugin_composition_policy(
            PluginCompositionPolicy(
                allowed_bundled_ids=frozenset({"frisket.typo-does-not-exist"})
            )
        )
    assert "frisket.typo-does-not-exist" in str(excinfo.value)


def test_mutable_input_set_cannot_retroactively_change_permissions() -> None:
    raw = {"frisket.geo"}
    policy = PluginCompositionPolicy(allowed_bundled_ids=raw, nonbundled_enabled=False)
    raw.add("frisket.injected")

    assert isinstance(policy.allowed_bundled_ids, frozenset)
    assert "frisket.injected" not in policy.allowed_bundled_ids
    assert policy.permits("frisket.geo", is_reviewed_bundled=True)
    assert not policy.permits("frisket.injected", is_reviewed_bundled=True)


@pytest.mark.real_bundled_plugins
def test_fresh_worker_process_installs_restricted_policy_before_dispatch(
    tmp_path,
) -> None:
    """A separately launched worker receives deployment plugin policy explicitly."""
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        from frisket.authoring.workbench.plugin_runtime import (
            bootstrap_project_bundled_plugins,
            ensure_workspace_plugin_packages,
            execute_workbench_plugin_local_install_plan,
        )
        from frisket.authoring.workbench.plugin_package_catalog import plugin_package_catalog_for_project
        from frisket.authoring.workbench.plugin_runtime_capabilities import project_runtime_binding
        from frisket.authoring.workbench.plugin_runtime_shared import (
            PluginCompositionPolicy,
            active_plugin_composition_policy,
        )
        from frisket.authoring.plugin_registry import (
            _reset_default_registry_for_tests,
            default_registry as plugin_registry,
        )
        from frisket.engine.jobs import default_registry, open_queue, register_production_handlers
        from frisket.engine.store.project import Project

        root = Path(sys.argv[1])
        project = Project.create(root / "proj.frisket", name="proj")
        ensure_workspace_plugin_packages(root)
        bootstrap_project_bundled_plugins(project, project_id="proj")
        assert project_runtime_binding(
            project, binding_type="projections", kind="frisket.geo.projection.map_points"
        ) is not None
        status, payload = execute_workbench_plugin_local_install_plan(
            project,
            project_id="proj",
            plugin_id="demo.selection_summary",
            source={"kind": "localPath", "value": sys.argv[2]},
            arbitrary_package_load_allowed=False,
        )
        assert status == 200, payload
        assert plugin_package_catalog_for_project(project).get("demo.selection_summary")
        _reset_default_registry_for_tests()

        restricted = PluginCompositionPolicy(
            allowed_bundled_ids=frozenset(), nonbundled_enabled=False
        )
        register_production_handlers(
            default_registry(),
            workspace_root=root,
            queue=open_queue(workspace=root),
            plugin_composition_policy=restricted,
        )
        assert active_plugin_composition_policy() == restricted
        assert "frisket.geo" not in {
            item.manifest.id for item in plugin_registry().plugin_manifests()
        }
        assert "demo.selection_summary" not in {
            item.manifest.id for item in plugin_registry().plugin_manifests()
        }
        assert project_runtime_binding(
            project, binding_type="projections", kind="frisket.geo.projection.map_points"
        ) is None
        """
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        # subprocess-boundary: prove a separately launched worker installs deployment policy before dispatch
        [
            sys.executable,  # subprocess-boundary: separately launched worker policy
            "-c",
            script,
            str(tmp_path / "worker"),
            str(
                Path(__file__).resolve().parents[1]
                / "fixtures/local_plugins/demo_selection_summary"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

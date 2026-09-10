from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status

from plugin_clean_cutover_assertions import (
    assert_bundled_internal_source_rejected,
    assert_internal_bootstrap_route_removed,
)


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


_BUNDLED_FIXTURE_SOURCE = """from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class Params(ActionParams):
    name: ColumnRef[str]


class Output(BaseModel):
    bundled_stamp: str | None


def stamp(params: Params, row: Row) -> RowResult[Output]:
    del row
    return RowResult(output=Output(bundled_stamp="bundled"))


STAMP = action(
    name="stamp",
    title="Stamp bundled fixture",
    description=(
        "Writes a constant marker column to prove the bundled install path "
        "routes through the real native runtime."
    ),
    category=ActionCategory.TEXT,
    run=map_rows(stamp),
)

plugin = Plugin(
    id={plugin_id!r},
    version="0.1.0",
    capabilities=[{capability!r}],
    actions=(STAMP,),
)
"""


def _write_bundled_fixture_package(root: Path, *, plugin_id: str) -> Path:
    """Write a minimal but real bundled plugin package into
    ``root / plugin_id``. Tests point ``plugin_runtime._bundled_plugins_root``
    at ``root`` (monkeypatched) instead of writing into the real
    src/frisket/authoring/bundled_plugins/ tree, so production project bootstrap
    (which reads that real tree) stays untouched by this fixture and every
    other test's "a fresh project has no plugins" assumption keeps holding.

    plugin.json is GENERATED from the declaration, exactly as
    `frisket plugin build` would emit it, so the fixture cannot drift from
    what the loader accepts.
    """
    from frisket.plugins.manifest_generate import (
        load_generation_source,
        manifest_dict_from_plugin,
        render_manifest_json,
    )

    package_root = root / plugin_id
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "plugin.py").write_text(
        _BUNDLED_FIXTURE_SOURCE.format(
            plugin_id=plugin_id, capability=TRUSTED_LOCAL_BACKEND_CAPABILITY
        ),
        encoding="utf-8",
    )
    (package_root / "plugin.json").write_text(
        render_manifest_json(
            manifest_dict_from_plugin(load_generation_source(package_root))
        ),
        encoding="utf-8",
    )
    return package_root


def _bundled_fixture_action_kind(plugin_id: str) -> str:
    """Registration namespaces the Action under the plugin id."""
    return f"{plugin_id}.stamp"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_id(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _install_bundled(client: TestClient, project_id: str, *, plugin_id: str) -> Any:
    return client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "bundled", "value": plugin_id},
            "arbitraryPackageLoadAllowed": False,
        },
    )


def _activate_bundled(
    client: TestClient, project_id: str, *, plugin_id: str, receipt_id: str
) -> Any:
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
    return backend


def test_bundled_source_installs_through_public_lifecycle_with_receipts_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED first: 'bundled' does not exist as a source kind before the fix,
    so install-local 400s with plugin_install_source_invalid. After the fix
    it must resolve host-side to the fixture package and run through the
    exact same plugin.load action / receipt / integrity-hash path as
    trusted-local localPath install."""
    _reset_default_registry_for_tests()
    try:
        bundled_root = tmp_path / "bundled_root"
        bundled_root.mkdir()
        plugin_id = "demo.bundled_fixture"
        action_kind = _bundled_fixture_action_kind(plugin_id)
        _write_bundled_fixture_package(bundled_root, plugin_id=plugin_id)
        monkeypatch.setattr(
            plugin_runtime, "_bundled_plugins_root", lambda: bundled_root
        )
        monkeypatch.setattr(
            plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
        )

        client = _client(tmp_path)
        project_id = _project_id(client, "bundled install path")

        response = _install_bundled(client, project_id, plugin_id=plugin_id)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
        assert payload["pluginId"] == plugin_id
        assert payload["installState"] == "installed"
        assert payload["activation"] == "manifestLoaded"
        assert payload["runtimeSource"] == "plugin.load_receipt"
        assert payload["arbitraryPackageLoadAllowed"] is False
        assert payload["installFailure"] is None
        assert payload["source"] == {"kind": "bundled", "value": plugin_id}
        assert payload["receiptId"]
        assert payload["manifestSha256"].startswith("sha256:")
        assert payload["packageSha256"].startswith("sha256:")

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        plugin = index_response.json()["plugins"][0]
        assert plugin["pluginId"] == plugin_id
        assert plugin["installState"] == "installed"
        assert plugin["source"] == {"kind": "bundled", "value": plugin_id}
        assert plugin["manifestSha256"] == payload["manifestSha256"]
        assert plugin["packageSha256"] == payload["packageSha256"]

        backend = _activate_bundled(
            client, project_id, plugin_id=plugin_id, receipt_id=payload["receiptId"]
        )
        assert backend.json()["registeredRuntimeBindings"]["actions"] == [action_kind]

        enabled_index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        enabled_plugin = enabled_index.json()["plugins"][0]
        assert enabled_plugin["installState"] == "enabled"
        assert enabled_plugin["source"] == {"kind": "bundled", "value": plugin_id}
        assert enabled_plugin["registryActivated"] is True
    finally:
        _reset_default_registry_for_tests()


def test_bundled_disable_survives_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_default_registry_for_tests()
    try:
        bundled_root = tmp_path / "bundled_root"
        bundled_root.mkdir()
        plugin_id = "demo.bundled_fixture_disable"
        _write_bundled_fixture_package(bundled_root, plugin_id=plugin_id)
        monkeypatch.setattr(
            plugin_runtime, "_bundled_plugins_root", lambda: bundled_root
        )
        monkeypatch.setattr(
            plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
        )

        client = _client(tmp_path)
        project_id = _project_id(client, "bundled disable")
        response = _install_bundled(client, project_id, plugin_id=plugin_id)
        assert response.status_code == 200, response.text
        payload = response.json()
        _activate_bundled(
            client, project_id, plugin_id=plugin_id, receipt_id=payload["receiptId"]
        )

        disabled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/disable"
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["installState"] == "disabled"

        # Reload: a fresh process/app instance against the SAME workspace
        # directory. The runtime index is derived purely from persisted
        # receipts + install_state (workbench_plugin_runtime_index's
        # docstring), never from the process-global registry, so this is
        # the honest "restart the server" simulation used throughout the
        # plugin lifecycle suites.
        _reset_default_registry_for_tests()
        reloaded_client = _client(tmp_path)
        reloaded_index = reloaded_client.get(
            f"/api/projects/{project_id}/workbench/plugins"
        )
        assert reloaded_index.status_code == 200, reloaded_index.text
        reloaded_plugin = reloaded_index.json()["plugins"][0]
        assert reloaded_plugin["pluginId"] == plugin_id
        assert reloaded_plugin["installState"] == "disabled"
        assert reloaded_plugin["source"] == {"kind": "bundled", "value": plugin_id}
    finally:
        _reset_default_registry_for_tests()


def test_bundled_bootstrap_seeds_enabled_plugin_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project bootstrap (spec 10.3: auto-enable DECIDED) seeds bundled
    plugins installed+enabled+activated through the same functions -- and
    re-running bootstrap must not duplicate receipts or fight an explicit
    disable."""
    _reset_default_registry_for_tests()
    try:
        bundled_root = tmp_path / "bundled_root"
        bundled_root.mkdir()
        plugin_id = "demo.bundled_bootstrap_fixture"
        _write_bundled_fixture_package(bundled_root, plugin_id=plugin_id)
        monkeypatch.setattr(
            plugin_runtime, "_bundled_plugins_root", lambda: bundled_root
        )
        monkeypatch.setattr(
            plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
        )

        client = _client(tmp_path)
        project_id = _project_id(client, "bundled bootstrap")

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        plugins = index_response.json()["plugins"]
        assert [item["pluginId"] for item in plugins] == [plugin_id]
        seeded = plugins[0]
        assert seeded["installState"] == "enabled"
        assert seeded["source"] == {"kind": "bundled", "value": plugin_id}
        assert seeded["registryActivated"] is True

        project = client.app.state.workspace.get(project_id)
        receipts_before = project.db.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE action_kind='plugin.load'"
        ).fetchone()["n"]

        # Re-bootstrap the same project: no duplicate receipts, state
        # unchanged.
        results = plugin_runtime.bootstrap_project_bundled_plugins(
            project, project_id=project_id
        )
        assert results == [
            {
                "pluginId": plugin_id,
                "installState": "enabled",
                # Re-bootstrap respects ANY existing state (it only seeds
                # plugins with none) — the old 'noop' branch reinstalled first
                # and could downgrade enabled → installed.
                "bootstrap": "skipped_existing_state",
            }
        ]
        receipts_after = project.db.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE action_kind='plugin.load'"
        ).fetchone()["n"]
        assert receipts_after == receipts_before

        # Disable, then re-bootstrap: the explicit opt-out must survive.
        disabled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/disable"
        )
        assert disabled.status_code == 200, disabled.text
        plugin_runtime.bootstrap_project_bundled_plugins(project, project_id=project_id)
        post_disable_index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert post_disable_index.json()["plugins"][0]["installState"] == "disabled", (
            "re-bootstrap must not silently re-enable a plugin the operator disabled"
        )
    finally:
        _reset_default_registry_for_tests()


def test_broken_bundled_package_fails_loudly_without_bricking_project_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _reset_default_registry_for_tests()
    try:
        bundled_root = tmp_path / "bundled_root"
        bundled_root.mkdir()
        good_id = "demo.bundled_good_fixture"
        _write_bundled_fixture_package(bundled_root, plugin_id=good_id)

        broken_id = "demo.bundled_broken_fixture"
        broken_root = bundled_root / broken_id
        broken_root.mkdir()
        (broken_root / "plugin.json").write_text("{not valid json", encoding="utf-8")

        monkeypatch.setattr(
            plugin_runtime, "_bundled_plugins_root", lambda: bundled_root
        )
        monkeypatch.setattr(
            plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
        )

        with caplog.at_level(logging.ERROR):
            client = _client(tmp_path)
            project_id = _project_id(client, "bundled resilience")

        assert any(broken_id in record.getMessage() for record in caplog.records), (
            "broken bundled package must log loudly"
        )

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        plugins_by_id = {
            item["pluginId"]: item for item in index_response.json()["plugins"]
        }
        assert plugins_by_id[good_id]["installState"] == "enabled"
        # The broken package is visible with installState=="failed" (the same
        # honest-failure projection every other install failure gets, e.g.
        # test_local_install_plan_persists_failed_descriptor_state) rather
        # than silently vanishing -- "fails loudly" means loud in the
        # product surface too, not only in logs.
        assert plugins_by_id[broken_id]["installState"] == "failed"
    finally:
        _reset_default_registry_for_tests()


def test_bundled_install_source_rejects_path_traversal_by_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_root = tmp_path / "bundled_root"
    bundled_root.mkdir()
    escape_target = tmp_path / "outside_secret.json"
    escape_target.write_text('{"leaked": true}', encoding="utf-8")
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled_root)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
    )

    client = _client(tmp_path)
    project_id = _project_id(client, "bundled traversal")

    for hostile_value in (
        "../../../etc",
        "../outside_secret",
        "..",
        "/etc/passwd",
        "a/../../b",
        "",
    ):
        response = client.post(
            "/api/projects/"
            f"{project_id}/workbench/plugins/traversal-probe/install-local",
            json={
                "source": {"kind": "bundled", "value": hostile_value},
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert response.status_code == 400, (hostile_value, response.text)
        assert (
            response.json()["installFailure"]["code"] == "plugin_install_source_invalid"
        ), hostile_value


def test_bundled_install_source_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regex-legal value naming a symlink that points OUTSIDE the bundled
    root must fail closed — this exercises the resolved-path containment
    check specifically, which the regex-level payloads above never reach."""
    bundled_root = tmp_path / "bundled_root"
    bundled_root.mkdir()
    outside = tmp_path / "outside_plugin"
    outside.mkdir()
    (outside / "plugin.json").write_text("{}", encoding="utf-8")
    (bundled_root / "sneaky").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled_root)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
    )

    client = _client(tmp_path)
    project_id = _project_id(client, "bundled symlink escape")

    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/sneaky/install-local",
        json={
            "source": {"kind": "bundled", "value": "sneaky"},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["installFailure"]["code"] == "plugin_install_source_invalid"


def test_bundled_internal_still_rejected_and_bootstrap_route_still_404(
    tmp_path: Path,
) -> None:
    """The clean-cutover assertions are IMPORTED AND RE-ASSERTED here, never
    edited: the new 'bundled' resolution shorthand must not resurrect the
    old parallel bundled_internal lifecycle it replaced."""
    assert_internal_bootstrap_route_removed(tmp_path)
    assert_bundled_internal_source_rejected(tmp_path)

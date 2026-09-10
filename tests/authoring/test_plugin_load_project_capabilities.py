"""Bundled ``frisket.ftm`` capability-gate contract.

The shipped bundled ``frisket.ftm`` package
(src/frisket/authoring/bundled_plugins/frisket.ftm/) installs through the REAL bundled
bootstrap (``bootstrap_project_bundled_plugins``,
src/frisket/authoring/workbench/plugin_runtime.py) instead of resting at
``installState: failed`` / ``invalid_plugin_manifest``.

Root cause under check: ``_plugin_manifest_contribution_error``
(src/frisket/contracts/actions/runtime.py) used to reject every manifest
capability outside ``{plugin:trusted_local_backend}``, while the shipped ftm
manifest declares ``plugin:project_writes`` + ``plugin:project_reads``
(+ ``plugin:trusted_local_backend``); the capability constants live at
src/frisket/contracts/plugin_write_plan.py:29-30. ftm-bundled-plugin-v1's
check stayed green because it proves the migration harness
(tests/test_ftm_bundled_plugin.py), never a real ``plugin.load`` of the
shipped package.

The acceptance contract is:
1. A fresh project's bundled bootstrap installs frisket.ftm to
   ``installState: "installed"`` — DORMANT, NOT enabled: its manifest ships
   ``auto_enable: false`` so the plugin rests one
   operator activation away — with ``installFailure`` null, a real
   ``receiptId``, sha256-shaped ``manifestSha256``/``packageSha256``, and
   the consent surface (``requires.capabilities``) listing all three
   declared capabilities.
2. Genuinely unknown capabilities (``custom:unimplemented``) STAY rejected
   as ``invalid_plugin_manifest`` — the gate is extended, not deleted (the
   executor-level pin at the unsupported_capability gate in tests/test_plugin_load_executor.py must
   also stay green; this file re-pins the same contract at the
   bundled-bootstrap level with a mutated package copy).
3. Retryable failed installs HEAL: a project whose install state already
   records TODAY's resting failure (``install_state: failed``,
   ``install_failure.code: invalid_plugin_manifest``, ``retryable: true`` —
   the state every existing project is in) recovers to "installed" on a
   later bootstrap pass (``_bootstrap_one_bundled_plugin``'s existing-state
   guard gains the retryable-failed carve-out beside package-drift refresh).
   Without the retry the gate fix never reaches existing
   projects: the resting failed state is respected forever and the package
   bytes never drift.
4. The retry guard's other half: a failed install whose
   ``install_failure.retryable`` is false (the
   ``plugin_install_manifest_mismatch`` shape) is NOT retried — it rests
   ``failed`` permanently.

Fixture idiom follows tests/test_bundled_plugin_drift_autoreload.py: a
per-test bundled root (monkeypatching ``plugin_runtime._bundled_plugins_root``
over the suite-wide empty root from tests/conftest.py) holding a mutable COPY
of the real shipped frisket.ftm package; process-global registry reset around
each test; a later bootstrap pass is a fresh app over the same workspace
(registry reset = restart, the app-upgrade shape that must heal existing
projects). Tests 3 and 4 seed the pre-fix resting failed state directly via
the product's own ``_upsert_workbench_plugin_install_state`` (the exact
recorder production uses at plugin_runtime.py:311-324, which stores failed
installs with ``receipt_id=""`` and no receipt) so the retry carve-out is
exercised honestly in BOTH worlds — pre-fix AND post-fix — instead of only
before the gate fix lands.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status


ROOT = Path(__file__).resolve().parents[2]
REAL_BUNDLED_FTM_ROOT = (
    ROOT / "src" / "frisket" / "authoring" / "bundled_plugins" / "frisket.ftm"
)
FTM_PLUGIN_ID = "frisket.ftm"
# The three capabilities the SHIPPED manifest declares. The constants live at
# src/frisket/contracts/plugin_write_plan.py:29-30; they are spelled here as
# black-box API strings while the implementation imports the constants.
FTM_DECLARED_CAPABILITIES = {
    "plugin:trusted_local_backend",
    "plugin:project_writes",
    "plugin:project_reads",
}
# Retryable resting-failure shape seeded by the healing tests.
INVALID_MANIFEST_RESTING_FAILURE = {
    "code": "invalid_plugin_manifest",
    "message": (
        "plugin.load does not support manifest capability requirements in this v1 slice"
    ),
    "retryable": True,
    "details": {"capabilities": ["plugin:project_reads", "plugin:project_writes"]},
}


@pytest.fixture()
def ftm_bundled_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test bundled-plugins root, initially EMPTY, overriding the
    suite-wide hermetic empty root pinned by tests/conftest.py. Tests copy
    the real shipped frisket.ftm package in via _install_ftm_package when
    (and only when) the scenario wants the package on disk — the healing
    tests need a project created BEFORE the package appears."""
    bundled_root = tmp_path / "bundled_plugins"
    bundled_root.mkdir()
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled_root)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
    )
    _reset_default_registry_for_tests()
    yield bundled_root
    _reset_default_registry_for_tests()


def _install_ftm_package(bundled_root: Path) -> Path:
    package_root = bundled_root / FTM_PLUGIN_ID
    shutil.copytree(
        REAL_BUNDLED_FTM_ROOT,
        package_root,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return package_root


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def _restarted_client(workspace: Path) -> TestClient:
    """A later bootstrap pass: registry reset + fresh app over the same
    on-disk workspace — the state every existing project is in right after
    the app upgrade that ships the gate fix."""
    _reset_default_registry_for_tests()
    return _client(workspace)


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _ftm_index_entry(client: TestClient, project_id: str) -> dict:
    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}
    assert FTM_PLUGIN_ID in plugins, (
        "the bundled bootstrap must surface frisket.ftm in the runtime index "
        f"(saw: {sorted(plugins)})"
    )
    return plugins[FTM_PLUGIN_ID]


def _seed_resting_failed_install(client: TestClient, failure: dict) -> None:
    """Record the resting failed state the way production records it: a failed
    bundled catalog entry (no package identity, the failure detail attached) in
    the WORKSPACE catalog. Bundled package identity is workspace-owned now, so
    the resting failure lives once per workspace, not per project."""
    catalog = client.app.state.workspace.plugin_package_catalog
    catalog.record_failure(
        plugin_id=FTM_PLUGIN_ID,
        install_source={"kind": "bundled", "value": FTM_PLUGIN_ID},
        install_failure=failure,
    )
    seeded = catalog.get(FTM_PLUGIN_ID)
    assert seeded is not None and seeded["manifest_sha256"] == ""
    recorded_failure = json.loads(seeded["install_failure"] or "{}")
    assert recorded_failure.get("code") == failure["code"]
    assert recorded_failure.get("retryable") is failure["retryable"]


def test_bundled_bootstrap_installs_ftm_dormant_with_project_capabilities(
    tmp_path: Path, ftm_bundled_root: Path
) -> None:
    """(1) plugin.load accepts the shipped manifest's declared capability set:
    the real frisket.ftm package installs through the real bundled bootstrap
    and rests DORMANT ('installed', not enabled — auto_enable:false must
    survive)."""
    _install_ftm_package(ftm_bundled_root)
    client = _client(tmp_path / "workspace")
    project_id = _create_project(client, "ftm capability gate")

    entry = _ftm_index_entry(client, project_id)
    assert entry["installState"] == "installed", (
        "the SHIPPED frisket.ftm package must install through the bundled "
        "bootstrap and rest dormant at installState 'installed' (its manifest "
        "ships auto_enable:false — 'enabled' would be an auto-enable "
        "regression, 'failed' is the stale capability gate rejecting the "
        "declared plugin:project_writes/plugin:project_reads): got "
        f"installState={entry['installState']!r} "
        f"installFailure={entry.get('installFailure')!r}"
    )
    assert entry.get("installFailure") is None, (
        "an installed frisket.ftm must carry no installFailure: "
        f"got {entry.get('installFailure')!r}"
    )
    assert entry["registryActivated"] is False, (
        "auto_enable:false dormancy is load-bearing: bootstrap must NOT "
        "registry-activate frisket.ftm — it rests one operator activation away"
    )
    assert entry.get("receiptId"), (
        "an installed frisket.ftm must carry a real plugin.load receiptId "
        "(failed installs record none)"
    )
    assert str(entry.get("manifestSha256") or "").startswith("sha256:"), (
        f"expected real manifest evidence, got {entry.get('manifestSha256')!r}"
    )
    assert str(entry.get("packageSha256") or "").startswith("sha256:"), (
        f"expected real package evidence, got {entry.get('packageSha256')!r}"
    )
    consent_capabilities = set(entry["requires"]["capabilities"])
    assert FTM_DECLARED_CAPABILITIES <= consent_capabilities, (
        "the consent surface must list all three declared capabilities "
        "while the plugin rests one activation away: got "
        f"{sorted(consent_capabilities)}"
    )


def test_genuinely_unknown_capabilities_stay_rejected(
    tmp_path: Path, ftm_bundled_root: Path
) -> None:
    """(2) The gate is EXTENDED, not deleted: a package copy whose manifest
    declares a genuinely unknown capability still fails invalid_plugin_manifest
    through the same bundled bootstrap. Green today and green after the fix —
    this pins the boundary the executor-level test
    (the unsupported_capability gate in tests/test_plugin_load_executor.py) pins, at this level."""
    package_root = _install_ftm_package(ftm_bundled_root)
    manifest_path = package_root / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requires"]["capabilities"] = sorted(
        set(manifest["requires"]["capabilities"]) | {"custom:unimplemented"}
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    client = _client(tmp_path / "workspace")
    project_id = _create_project(client, "ftm unknown capability")

    entry = _ftm_index_entry(client, project_id)
    assert entry["installState"] == "failed", (
        "a manifest declaring a genuinely unknown capability "
        "(custom:unimplemented) must keep failing to install: got "
        f"installState={entry['installState']!r}"
    )
    failure = entry.get("installFailure") or {}
    assert failure.get("code") == "invalid_plugin_manifest", (
        f"expected invalid_plugin_manifest, got {failure!r}"
    )


def test_failed_bundled_catalog_entry_heals_on_next_construction(
    tmp_path: Path, ftm_bundled_root: Path
) -> None:
    """(3) A workspace whose ftm CATALOG entry rests at a failure heals when a
    later Workspace construction re-seeds the bundled catalog with the package
    now valid on disk.

    Note: the old per-project retryable-failed carve-out
    (`_bootstrap_one_bundled_plugin`/`_failed_bundled_install_retryable`) is
    deleted. Package identity is workspace-owned now, so healing happens ONCE at
    Workspace construction (`ensure_workspace_plugin_packages` re-attempts every
    present bundled package's evidence build and overwrites a stale failure),
    and every project's index read then surfaces the healed identity."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    # Bundled root is EMPTY at first construction, so ftm is absent; then the
    # resting workspace failure is seeded directly.
    project_id = _create_project(client, "ftm heals on next construction")
    _seed_resting_failed_install(client, INVALID_MANIFEST_RESTING_FAILURE)
    # The (fixed) package is on disk when the next construction re-seeds.
    _install_ftm_package(ftm_bundled_root)

    reloaded = _restarted_client(workspace)
    entry = _ftm_index_entry(reloaded, project_id)
    assert entry["installState"] == "installed", (
        "a bundled catalog entry resting at 'failed' must be re-attempted by "
        "the next Workspace construction and heal to 'installed' once the "
        "package validates: got "
        f"installState={entry['installState']!r} "
        f"installFailure={entry.get('installFailure')!r}"
    )
    assert entry.get("installFailure") is None
    assert entry.get("receiptId"), "a healed install must carry package identity"
    assert entry["registryActivated"] is False, (
        "healing a failed install must not skip the auto_enable:false opt-out: "
        "the healed install rests dormant, never auto-enabled"
    )


def test_still_invalid_bundled_package_stays_failed_across_construction(
    tmp_path: Path, ftm_bundled_root: Path
) -> None:
    """(4) No spurious heal: a bundled package that STILL fails validation rests
    failed across restarts.

    Note: with the retryable/non-retryable per-project retry guard deleted,
    the honest invariant is simply that re-seeding does not falsely heal — a
    package whose manifest is still invalid records the failure again at every
    construction."""
    workspace = tmp_path / "workspace"
    package_root = _install_ftm_package(ftm_bundled_root)
    manifest_path = package_root / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["requires"]["capabilities"] = sorted(
        set(manifest["requires"]["capabilities"]) | {"custom:unimplemented"}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    client = _client(workspace)
    project_id = _create_project(client, "ftm stays failed")
    entry = _ftm_index_entry(client, project_id)
    assert entry["installState"] == "failed"
    assert (entry.get("installFailure") or {}).get("code") == "invalid_plugin_manifest"

    reloaded = _restarted_client(workspace)
    entry = _ftm_index_entry(reloaded, project_id)
    assert entry["installState"] == "failed", (
        "a still-invalid bundled package must stay failed across construction, "
        f"never spuriously heal: got installState={entry['installState']!r}"
    )
    assert (entry.get("installFailure") or {}).get("code") == "invalid_plugin_manifest"

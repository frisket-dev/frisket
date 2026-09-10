from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.services.projects import (
    ProjectLifecycleService,
    ProjectRetentionError,
)
from frisket.authoring.workbench.plugin_subprocess import (
    _missing_project_plugin_env,
    _project_plugin_env,
)


PLUGIN_ID = "demo.env_settings"
PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)


def _install_and_activate(client: TestClient, pid: str) -> None:
    installed = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    activated = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": installed.json()["receiptId"],
            "trustAcknowledged": True,
            "permissionsAccepted": ["plugin:trusted_local_backend"],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text


def test_project_secrets_surface_plugin_consumers(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Secret consumers"}).json()["id"]
    _install_and_activate(client, pid)

    response = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "DEMO_API_KEY", "value": "consumer-secret"},
    )

    assert response.status_code == 200, response.text
    secret = response.json()["secrets"][0]
    assert secret["name"] == "DEMO_API_KEY"
    assert secret["consumers"] == [{"kind": "plugin", "id": PLUGIN_ID}]


def test_plugin_env_write_does_not_mirror_into_global_project_secrets(
    tmp_path,
) -> None:
    # The former dual-write mirrored a
    # plugin env value into the GLOBAL project_secrets namespace is removed. A
    # plugin secret is plugin-scoped only and must never appear in the shared
    # project-secrets surface consumed by core/sources/jobs/mcp.
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Secret bridge"}).json()["id"]
    _install_and_activate(client, pid)

    saved = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/env",
        json={"name": "DEMO_API_KEY", "value": "legacy-secret"},
    )

    assert saved.status_code == 200, saved.text
    secrets = client.get(f"/api/projects/{pid}/secrets").json()["secrets"]
    assert secrets == []
    project = client.app.state.workspace.get(pid)
    assert (
        project.db.execute(
            "SELECT 1 FROM project_secrets WHERE name=?", ("DEMO_API_KEY",)
        ).fetchone()
        is None
    )


def test_project_secret_consumer_refs_cover_source_job_and_mcp(tmp_path) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Secret refs"}).json()["id"]
    service = ProjectLifecycleService(app.state.workspace)

    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "shared_token", "value": "shared-secret-value"},
    )
    assert saved.status_code == 200, saved.text

    for kind, consumer_id in [
        ("source", "rss-feed:1"),
        ("job", "source.poll"),
        ("mcp_connector", "linear"),
    ]:
        declared = service.declare_project_secret_consumer(
            pid,
            kind=kind,
            consumer_id=consumer_id,
            name="shared_token",
        )
        assert declared == {
            "kind": kind,
            "id": consumer_id,
            "name": "SHARED_TOKEN",
        }
        assert (
            service.resolve_project_secret_for_consumer(
                pid,
                kind=kind,
                consumer_id=consumer_id,
                name="shared_token",
            )
            == "shared-secret-value"
        )

    secret = client.get(f"/api/projects/{pid}/secrets").json()["secrets"][0]
    assert secret["consumers"] == [
        {"kind": "job", "id": "source.poll"},
        {"kind": "mcp_connector", "id": "linear"},
        {"kind": "source", "id": "rss-feed:1"},
    ]
    assert "shared-secret-value" not in client.get(f"/api/projects/{pid}/secrets").text


def test_project_secret_resolution_prefers_project_secret_over_org_fallback(
    tmp_path,
) -> None:
    def fallback(project_id: str, name: str) -> str | None:
        assert project_id
        return "org-secret-value" if name == "TOKEN" else None

    app = create_app(
        tmp_path / "workspace",
        project_secret_fallback_resolver=fallback,
    )
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Secret fallback"}).json()["id"]
    service = ProjectLifecycleService(app.state.workspace)
    service.declare_project_secret_consumer(
        pid,
        kind="job",
        consumer_id="source.poll",
        name="token",
    )

    assert (
        service.resolve_project_secret_for_consumer(
            pid,
            kind="job",
            consumer_id="source.poll",
            name="token",
        )
        == "org-secret-value"
    )

    project = app.state.workspace.get(pid)
    metadata = {"requires_secrets": ["TOKEN"]}
    assert (
        _missing_project_plugin_env(
            project,
            plugin_id="demo.plugin",
            metadata=metadata,
        )
        == []
    )
    assert _project_plugin_env(
        project,
        plugin_id="demo.plugin",
        metadata=metadata,
    ) == {"TOKEN": "org-secret-value"}

    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "token", "value": "project-secret-value"},
    )
    assert saved.status_code == 200, saved.text
    # The declared job consumer resolves the newly-set project secret (the
    # shared source/job/mcp path still prefers project secret over org fallback).
    assert (
        service.resolve_project_secret_for_consumer(
            pid,
            kind="job",
            consumer_id="source.poll",
            name="token",
        )
        == "project-secret-value"
    )
    # The plugin path is scoped to the plugin's
    # own secrets and no longer reads the global project_secrets namespace, so
    # setting a core project secret does NOT leak into the plugin — it keeps
    # resolving only the org fallback it was already entitled to.
    assert _project_plugin_env(
        project,
        plugin_id="demo.plugin",
        metadata=metadata,
    ) == {"TOKEN": "org-secret-value"}


def test_project_secret_resolution_rejects_undeclared_consumer(tmp_path) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Secret rejection"}).json()["id"]
    service = ProjectLifecycleService(app.state.workspace)

    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "TOKEN", "value": "shared-secret-value"},
    )
    assert saved.status_code == 200, saved.text

    try:
        service.resolve_project_secret_for_consumer(
            pid,
            kind="source",
            consumer_id="undeclared",
            name="TOKEN",
        )
    except ProjectRetentionError as exc:
        assert "not declared" in str(exc)
    else:
        raise AssertionError("undeclared secret consumer resolved a project secret")


def test_project_secrets_malformed_plugin_does_not_blank_other_consumers(
    tmp_path, monkeypatch, caplog
) -> None:
    """A single plugin with a malformed requires.secrets shape must not
    swallow the whole plugin-consumer merge: the well-formed plugin's
    consumer entry still surfaces, and the malformed one is logged rather
    than silently dropped."""
    import frisket.server.services.projects as projects_module

    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Malformed plugin"}).json()["id"]
    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "API_KEY", "value": "value"},
    )
    assert saved.status_code == 200, saved.text

    def fake_runtime_index(project, *, project_id):
        return {
            "plugins": [
                {"pluginId": "demo.malformed", "requires": "not-a-dict"},
                {"pluginId": "demo.wellformed", "requires": {"secrets": ["API_KEY"]}},
            ]
        }

    monkeypatch.setattr(
        projects_module, "workbench_plugin_runtime_index", fake_runtime_index
    )
    service = ProjectLifecycleService(app.state.workspace)

    with caplog.at_level("WARNING"):
        result = service.project_secrets(pid)

    secret = next(s for s in result["secrets"] if s["name"] == "API_KEY")
    assert secret["consumers"] == [{"kind": "plugin", "id": "demo.wellformed"}]
    assert "demo.malformed" in caplog.text

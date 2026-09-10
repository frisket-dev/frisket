"""Plugin secret resolution must be scoped by ``plugin_id``.

Plugin env values
were written to BOTH the plugin-scoped ``workbench_plugin_env_vars`` table and
the GLOBAL ``project_secrets`` namespace, and the execution read path
(``_project_plugin_env``) decrypted required names from GLOBAL project_secrets
ignoring plugin_id. Consequences: a plugin declaring a name (e.g. a core
provider credential) siphoned the project's existing global value (disclosure),
and configuring that name through a plugin overwrote the global value consumed
by core/other plugins (cross-plugin credential poisoning).

These checks pin the closed contract:
  (a) two plugins declaring the same env name get ISOLATED values;
  (b) a plugin declaring a name it does not own does NOT automatically receive
      a core/project secret of that name;
  (c) setting a plugin secret does not mutate the global project secret;
  (d) a plugin cannot read or set a reserved core credential name.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.team.security.secrets import decrypt_secret, encrypt_secret
from frisket.server.app import create_app
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


def _write_scoped_secret(project, *, plugin_id: str, name: str, value: str) -> None:
    project.db.execute(
        "INSERT INTO workbench_plugin_env_vars "
        "(plugin_id, name, encrypted, hint, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(plugin_id, name) DO UPDATE SET encrypted=excluded.encrypted",
        (plugin_id, name, encrypt_secret(value), "..." + value[-4:]),
    )
    project.db.commit()


def test_two_plugins_same_env_name_are_isolated(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Isolation"}).json()["id"]
    project = client.app.state.workspace.get(pid)

    _write_scoped_secret(
        project, plugin_id="plugin.a", name="SHARED_KEY", value="value-a"
    )
    _write_scoped_secret(
        project, plugin_id="plugin.b", name="SHARED_KEY", value="value-b"
    )

    metadata = {"requires_secrets": ["SHARED_KEY"]}
    assert _project_plugin_env(project, plugin_id="plugin.a", metadata=metadata) == {
        "SHARED_KEY": "value-a"
    }
    assert _project_plugin_env(project, plugin_id="plugin.b", metadata=metadata) == {
        "SHARED_KEY": "value-b"
    }


def test_plugin_does_not_siphon_unowned_core_project_secret(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "No siphon"}).json()["id"]

    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "CORE_TOKEN", "value": "core-owned-value"},
    )
    assert saved.status_code == 200, saved.text

    project = client.app.state.workspace.get(pid)
    metadata = {"requires_secrets": ["CORE_TOKEN"]}
    # The plugin holds no scoped value for CORE_TOKEN: it must NOT receive the
    # core-owned project secret, and must report it missing (fail closed).
    assert _missing_project_plugin_env(
        project, plugin_id="demo.plugin", metadata=metadata
    ) == ["CORE_TOKEN"]
    resolved = _project_plugin_env(project, plugin_id="demo.plugin", metadata=metadata)
    # Fail-closed: the value is never resolved into the plugin env; the call
    # returns the missing-env error instead of the core-owned plaintext.
    assert "CORE_TOKEN" not in resolved
    assert "core-owned-value" not in str(resolved)
    assert resolved.get("_error", {}).get("errors", [{}])[0].get("code") == (
        "plugin_env_missing"
    )


def test_setting_plugin_secret_does_not_mutate_global_project_secret(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "No poison"}).json()["id"]
    _install_and_activate(client, pid)

    saved = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/env",
        json={"name": "DEMO_API_KEY", "value": "plugin-owned-value"},
    )
    assert saved.status_code == 200, saved.text

    project = client.app.state.workspace.get(pid)
    # Plugin-scoped storage holds the value...
    scoped = project.db.execute(
        "SELECT encrypted FROM workbench_plugin_env_vars WHERE plugin_id=? AND name=?",
        (PLUGIN_ID, "DEMO_API_KEY"),
    ).fetchone()
    assert scoped is not None
    assert decrypt_secret(str(scoped["encrypted"])) == "plugin-owned-value"
    # ...but the global project_secrets namespace is NOT written by the plugin.
    globalrow = project.db.execute(
        "SELECT 1 FROM project_secrets WHERE name=?", ("DEMO_API_KEY",)
    ).fetchone()
    assert globalrow is None

    # And the value still resolves for the owning plugin (no regression).
    assert _project_plugin_env(
        project,
        plugin_id=PLUGIN_ID,
        metadata={"requires_secrets": ["DEMO_API_KEY"]},
    ) == {"DEMO_API_KEY": "plugin-owned-value"}


def test_plugin_cannot_set_reserved_core_credential_name(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Reserved"}).json()["id"]
    _install_and_activate(client, pid)

    refused = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/env",
        json={"name": "OPENAI_API_KEY", "value": "sk-should-be-refused"},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["code"] == "plugin_env_name_reserved"
    assert "sk-should-be-refused" not in refused.text


def test_plugin_does_not_read_reserved_core_credential(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Reserved read"}).json()["id"]

    project = client.app.state.workspace.get(pid)
    # A core provider credential lives in the global namespace.
    project.db.execute(
        "INSERT INTO project_secrets (name, encrypted, hint, updated_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("OPENAI_API_KEY", encrypt_secret("sk-core"), "...core"),
    )
    project.db.commit()

    metadata = {"requires_secrets": ["OPENAI_API_KEY"]}
    # Reserved core credential: fail-closed missing, never resolved from the
    # global namespace or org fallback.
    assert _missing_project_plugin_env(
        project, plugin_id="demo.plugin", metadata=metadata
    ) == ["OPENAI_API_KEY"]
    resolved = _project_plugin_env(project, plugin_id="demo.plugin", metadata=metadata)
    assert "OPENAI_API_KEY" not in resolved
    assert "sk-core" not in str(resolved)

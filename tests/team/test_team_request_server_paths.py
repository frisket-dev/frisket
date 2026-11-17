"""Request-supplied server paths are refused outside the local edition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.routes.skills import register_skill_routes
from frisket.server.services.skills import SkillLibrary
from frisket.team.enforcement import protect_core_app
from tests.deterministic_time import controlled_time

EDITOR = {
    "id": 11,
    "email": "editor@example.com",
    "organization_id": 1,
    "auth": "session",
}
PROJECT = "proof"
SECRET = b"server-only-bytes"
CSV = b"name,note\nAda," + SECRET + b"\n"


EDITOR_ROLES = frozenset({"viewer", "reviewer", "editor"})


class _Access:
    def __init__(self, roles: frozenset[str]) -> None:
        self.roles = roles

    def resolve_project_for_user_id(
        self,
        user_id: int,
        slug: str,
        *,
        preferred_organization_id: int | None = None,
        scope_organization_id: int | None = None,
    ) -> dict[str, Any]:
        return {"organization_id": 1, "storage_organization_id": 1, "slug": slug}

    def can_on_project(
        self, organization_id: int, slug: str, user_id: int, need: str
    ) -> bool:
        return need in self.roles


def _team_app(root: Path, roles: frozenset[str] = EDITOR_ROLES) -> FastAPI:
    core = create_app(
        root, serve_spa=False, enable_provider_config=False, edition="team"
    )
    register_skill_routes(
        core, library=SkillLibrary(root), require_manager=lambda _request: None
    )

    def resolve_user(_request: Request) -> dict[str, Any]:
        return dict(EDITOR)

    return protect_core_app(
        app=core,
        resolve_user=resolve_user,
        admin_emails=lambda: set(),
        project_access=_Access(roles),
    )


@pytest.fixture
def team(tmp_path: Path):
    app = _team_app(tmp_path / "workspace")
    with TestClient(app) as client:
        app.state.workspace.create("Proof", project_id=PROJECT)
        yield client


@pytest.fixture
def server_files(tmp_path: Path) -> dict[str, Path]:
    """A directory outside the workspace holding a server-only file."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_bytes(CSV)
    return {"outside": outside}


def _run(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/api/projects/{PROJECT}/actions/v1/run", json=body)
    result = response.json()
    expected = 200 if result.get("status") == "completed" else 400
    assert response.status_code == expected, response.text
    return result


def _read_requests(path: str) -> list[dict[str, Any]]:
    return [
        {
            "action_id": "import.files",
            "scope": {"kind": "project"},
            "sheet_name": "loot",
            "params": {"files": [{"path": path}]},
            "idempotency_key": "read/files",
        },
        {
            "action_id": "import.csv",
            "scope": {"kind": "project"},
            "sheet_name": "loot",
            "params": {
                "sources": [{"path": path}],
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "note", "type": "text"},
                ],
            },
            "idempotency_key": "read/csv",
        },
    ]


def _write_requests(target: Path) -> list[dict[str, Any]]:
    return [
        {
            "action_id": "export.work_log",
            "scope": {"kind": "project"},
            "params": {"destination": {"kind": "local_file", "path": str(target)}},
            "idempotency_key": "write/work-log",
        },
        {
            "action_id": "export.sheet_csv",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": 1,
                "destination": {"kind": "local_file", "path": str(target)},
            },
            "idempotency_key": "write/csv",
        },
        {
            "action_id": "export.column_tables",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": 1,
                "column_id": 1,
                "destination": {"kind": "local_dir", "path": str(target.parent)},
            },
            "idempotency_key": "write/column-tables",
        },
        {
            "action_id": "embedding.index_export",
            "scope": {"kind": "project"},
            "params": {
                "index_id": "idx",
                "destination": {"kind": "local_dir", "path": str(target.parent)},
            },
            "idempotency_key": "write/index",
        },
        {
            "action_id": "plugin.load",
            "scope": {"kind": "project"},
            "params": {"manifest": {"kind": "local_file", "path": str(target)}},
            "idempotency_key": "load/plugin",
        },
    ]


def _project_dir(client: TestClient) -> Path:
    return client.app.state.workspace.get(PROJECT).path


def _assert_nothing_imported(client: TestClient) -> None:
    project = client.app.state.workspace.get(PROJECT)
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 0
    blobs = project.path / "blobs"
    stored = [p.read_bytes() for p in blobs.rglob("*") if p.is_file()]
    assert not any(SECRET in content for content in stored)


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_team_editor_cannot_read_a_server_path(team, server_files, where) -> None:
    source = (
        server_files["outside"] / "secret.csv"
        if where == "outside"
        else _project_dir(team) / "planted.csv"
    )
    source.write_bytes(CSV)
    for body in _read_requests(str(source)):
        result = _run(team, body)
        assert result["status"] == "failed", (body["action_id"], result)
        assert [error["code"] for error in result["errors"]] == [
            "invalid_file_source"
        ], result
        assert "not available on this deployment" in result["errors"][0]["message"]
    _assert_nothing_imported(team)


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_team_editor_cannot_write_or_load_a_server_path(
    team, server_files, where
) -> None:
    base = server_files["outside"] if where == "outside" else _project_dir(team)
    target = base / "written.out"
    before = sorted(p.name for p in base.iterdir())
    for body in _write_requests(target):
        result = _run(team, body)
        assert result["status"] == "failed", (body["action_id"], result)
        assert [error["code"] for error in result["errors"]] == [
            "server_path_unavailable"
        ], result
        assert result["errors"][0]["field"].startswith("params.")
        assert result.get("receipt_id") is None
    assert sorted(p.name for p in base.iterdir()) == before


def test_team_editor_cannot_preview_a_server_path(team, server_files) -> None:
    body = _read_requests(str(server_files["outside"] / "secret.csv"))[0]
    started = team.post(f"/api/projects/{PROJECT}/actions/v1/preview", json=body)
    assert started.status_code == 202, started.text
    preview = (
        f"/api/projects/{PROJECT}/actions/v1/preview/{started.json()['preview_id']}"
    )
    payload: dict[str, Any] = {}

    def finished() -> bool:
        payload.update(team.get(preview).json())
        return payload["status"] != "running"

    with controlled_time(timeout=5) as clock:
        clock.wait_until(finished, message="table preview did not finish")
    assert payload["status"] != "done", payload
    assert "not available on this deployment" in str(payload), payload


def test_code_recipes_off_leaves_no_server_path_bypass(
    team, server_files, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    secret = server_files["outside"] / "secret.csv"
    for body in _read_requests(str(secret)):
        assert _run(team, body)["errors"][0]["code"] == "invalid_file_source"
    target = server_files["outside"] / "written.out"
    for body in _write_requests(target):
        assert _run(team, body)["errors"][0]["code"] == "server_path_unavailable"
    assert not target.exists()
    _assert_nothing_imported(team)


def test_team_queued_update_cannot_read_a_server_path(team, server_files) -> None:
    secret = server_files["outside"] / "secret.csv"
    seeded = _run(
        team,
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "people",
            "params": {
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "note", "type": "text"},
                ],
                "rows": [{"name": "Ada", "note": "seed"}],
            },
            "idempotency_key": "seed/rows",
        },
    )
    assert seeded["status"] == "completed", seeded
    result = _run(
        team,
        {
            "action_id": "import.update_csv",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "sources": [{"path": str(secret)}],
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "note", "type": "text"},
                ],
                "key_columns": ["name"],
            },
            "idempotency_key": "update/csv",
        },
    )
    assert result["status"] == "failed", result
    assert "not available on this deployment" in str(result["errors"]), result
    project = team.app.state.workspace.get(PROJECT)
    values = [str(row[0]) for row in project.db.execute("SELECT value FROM cells")]
    assert not any(SECRET.decode() in value for value in values)


def test_team_upload_still_imports(team) -> None:
    response = team.post(
        f"/api/projects/{PROJECT}/import/files?sheet_name=uploads",
        files=[("files", ("notes.txt", b"uploaded notes", "text/plain"))],
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 1


def test_local_edition_reads_and_writes_operator_paths(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "notes.txt"
    source.write_bytes(b"operator notes")
    target = outside / "work-log.md"
    with TestClient(create_app(tmp_path / "workspace")) as client:
        client.app.state.workspace.create("Local", project_id=PROJECT)
        imported = _run(client, _read_requests(str(source))[0])
        assert imported["status"] == "completed", imported
        exported = _run(client, _write_requests(target)[0])
        assert exported["status"] == "completed", exported
    assert target.read_text().strip()


@pytest.mark.parametrize(
    "source_kind", [("localPath", "value"), ("local_file", "path")]
)
def test_team_project_owner_cannot_install_a_plugin_from_a_server_path(
    tmp_path: Path, server_files, source_kind
) -> None:
    kind, field = source_kind
    package = server_files["outside"] / "plugin"
    package.mkdir()
    (package / "plugin.json").write_text("{}")
    app = _team_app(tmp_path / "workspace", roles=EDITOR_ROLES | {"owner"})
    with TestClient(app) as client:
        app.state.workspace.create("Proof", project_id=PROJECT)
        response = client.post(
            f"/api/projects/{PROJECT}/workbench/plugins/demo.plugin/install-local",
            json={"source": {"kind": kind, field: str(package / "plugin.json")}},
        )
    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["installFailure"]["code"] == "plugin_install_server_path_unavailable"
    assert payload["receiptId"] is None

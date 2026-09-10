"""workbench-ia-shell-v1 (Workbench IA increment 9 — Home/projects screen):
the project list gains additive `starred`/`archived` manifest flags that scope
the Home project list (Starred / Archive). Archive is a FLAG, never a deletion —
an archived project stays on disk and in the list payload, just marked. The
flags are written through PATCH /api/projects/{pid} and read cheaply by
Workspace.list() (manifest.json only, no sqlite open).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _manifest(tmp_path, pid: str) -> dict:
    return json.loads(
        (tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json").read_text()
    )


def test_new_project_defaults_flags_false(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Fresh"}).json()["id"]

    entry = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert entry["starred"] is False
    assert entry["archived"] is False


def test_patch_sets_starred_and_archived_flags(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Flagged"}).json()["id"]

    response = client.patch(f"/api/projects/{pid}", json={"starred": True})
    assert response.status_code == 200, response.text
    assert response.json()["starred"] is True
    assert response.json()["archived"] is False
    assert _manifest(tmp_path, pid)["starred"] is True

    # The list payload reflects the flag without opening the sqlite bundle.
    entry = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert entry["starred"] is True

    # Archiving is orthogonal and independently settable.
    client.patch(f"/api/projects/{pid}", json={"archived": True})
    entry = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert entry["starred"] is True
    assert entry["archived"] is True


def test_archive_is_a_flag_not_a_deletion(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Kept"}).json()["id"]

    client.patch(f"/api/projects/{pid}", json={"archived": True})

    # The project is still present in the listing (archived, not removed)…
    listed = client.get("/api/projects").json()
    entry = next((p for p in listed if p["id"] == pid), None)
    assert entry is not None
    assert entry["archived"] is True
    # …and its bundle is still on disk.
    assert (tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json").exists()


def test_flag_patch_preserves_name_and_description(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Named"}).json()["id"]
    client.patch(
        f"/api/projects/{pid}",
        json={"name": "Renamed", "description": "Kept"},
    )

    # A flag-only patch must not clobber the existing name/description.
    body = client.patch(f"/api/projects/{pid}", json={"starred": True}).json()
    assert body["name"] == "Renamed"
    assert body["description"] == "Kept"
    assert body["starred"] is True

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store.import_sessions import ImportSessionConflict
from frisket.server.app import create_app
from frisket.server.services.import_sessions import ImportSessionService


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            ValueError("cancelled import requires a decision"),
            409,
            "cancelled import requires a decision",
        ),
        (
            ImportSessionConflict("import session writer is stale"),
            409,
            "import session writer is stale",
        ),
        (
            FileNotFoundError("/private/projects/secret/manifest.json"),
            409,
            "import session was not found",
        ),
        (
            OSError("cannot open /private/projects/secret/inventory.db"),
            500,
            "Internal Server Error",
        ),
        (
            RuntimeError("backend failed at /private/projects/secret"),
            500,
            "Internal Server Error",
        ),
    ],
)
def test_import_session_routes_only_echo_expected_conflicts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    project_id = client.post("/api/projects", json={"name": "Errors"}).json()["id"]

    def fail_status(self, requested_project_id: str, ref: str):
        del self, requested_project_id, ref
        raise error

    monkeypatch.setattr(ImportSessionService, "status", fail_status)
    response = client.get(
        f"/api/projects/{project_id}/import/files/sessions/import-{'0' * 32}"
    )
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert "/private/" not in response.text

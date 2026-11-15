from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from frisket.contracts.http.onboarding_imports import (
    ImportPasteConfirmBody,
)
from frisket.server.app import create_app
from frisket.server.services.import_drafts import (
    ImportDraftRouteError,
    ImportDraftService,
)


def _reviewed_paste_body(
    *,
    raw: str,
    draft_id: str,
    destination_sheet_id: int | None = None,
    key_columns: list[str] | None = None,
    confirmation: str | None = None,
) -> ImportPasteConfirmBody:
    return ImportPasteConfirmBody(
        raw=raw,
        draft_id=draft_id,
        columns=[
            {"source_name": "id", "name": "id", "type": "text"},
            {"source_name": "score", "name": "score", "type": "integer"},
            {"source_name": "unused", "name": None, "type": "text"},
        ],
        destination_sheet_id=destination_sheet_id,
        key_columns=key_columns,
        confirmation=confirmation,
    )


def test_server_paste_confirm_replays_and_omits_unreviewed_source(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Paste"}).json()["id"]
    service = ImportDraftService(client.app.state.workspace)
    raw = "id,score,unused\na,7,secret\n"
    draft = service.paste_draft(pid, raw)
    body = _reviewed_paste_body(raw=raw, draft_id=draft["draft_id"])

    first = service.confirm(pid, body)
    second = service.confirm(pid, body)

    assert (
        first.payload
        == second.payload
        == {
            "sheet_id": 1,
            "rows": 1,
            "columns": ["id", "score"],
        }
    )
    data = client.get(f"/api/projects/{pid}/sheets/1/data").json()
    assert [column["name"] for column in data["columns"]] == ["id", "score"]
    assert client.app.state.workspace.get(pid).row_count(1) == 1


def test_server_paste_refuses_stale_draft_before_action_construction(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Paste"}).json()["id"]
    service = ImportDraftService(client.app.state.workspace)
    draft = service.paste_draft(pid, "id,score,unused\na,7,secret\n")
    body = _reviewed_paste_body(
        raw="id,score,unused\na,8,secret\n", draft_id=draft["draft_id"]
    )

    with pytest.raises(ImportDraftRouteError) as raised:
        service.confirm(pid, body)

    assert raised.value.status_code == 409
    assert client.get(f"/api/projects/{pid}/sheets").json() == []

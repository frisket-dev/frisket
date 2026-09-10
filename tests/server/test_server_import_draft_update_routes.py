from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits
from frisket.contracts.http.onboarding_imports import (
    ImportPasteConfirmBody,
    ImportUpdatePreviewBody,
)
from frisket.server.app import create_app
from frisket.server.services.import_drafts import (
    ImportDraftRouteError,
    ImportDraftService,
)


def test_pasted_update_preview_reports_live_matches_without_mutating(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Pasted update"}).json()[
        "id"
    ]
    seeded = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
                "rows": [{"id": "a", "name": "Ada"}],
            },
            "idempotency_key": "seed-pasted-update-preview",
        },
    ).json()
    sheet_id = seeded["outputs"][0]["sheet_id"]

    raw = "id,name\na,Augusta\nmissing,Nobody\n"
    draft = client.post(
        f"/api/projects/{project_id}/import/drafts/paste", json={"raw": raw}
    ).json()
    response = client.post(
        f"/api/projects/{project_id}/import/rows/update/preview",
        json={
            "raw": raw,
            "draft_id": draft["draft_id"],
            "destination_sheet_id": sheet_id,
            "columns": [
                {"source_name": "id", "name": "id", "type": "text"},
                {"source_name": "name", "name": "name", "type": "text"},
            ],
            "key_columns": ["id"],
        },
    )

    assert response.status_code == 200, response.text
    preview = response.json()
    assert (preview["matched"], preview["unmatched"], preview["changed_cells"]) == (
        1,
        1,
        1,
    )
    assert preview["confirmation"]
    data = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    assert [list(row["cells"].values()) for row in data["rows"]] == [["a", "Ada"]]


def test_pasted_update_preview_refuses_an_unknown_destination(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Unknown destination"}
    ).json()["id"]

    raw = "id,name\na,Ada\n"
    draft = client.post(
        f"/api/projects/{project_id}/import/drafts/paste", json={"raw": raw}
    ).json()
    response = client.post(
        f"/api/projects/{project_id}/import/rows/update/preview",
        json={
            "raw": raw,
            "draft_id": draft["draft_id"],
            "destination_sheet_id": 999,
            "columns": [
                {"source_name": "id", "name": "id", "type": "text"},
                {"source_name": "name", "name": "name", "type": "text"},
            ],
            "key_columns": ["id"],
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "sheet_not_found"


def test_pasted_update_preview_uses_request_composed_row_limit(tmp_path) -> None:
    requests: list[tuple[str, Any]] = []

    def executor_deps_factory(project_id: str, request: Any) -> ExecutorDeps:
        requests.append((project_id, request))
        return ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=1))

    client = TestClient(
        create_app(
            tmp_path / "workspace",
            executor_deps_factory=executor_deps_factory,
        )
    )
    project_id = client.post("/api/projects", json={"name": "Update preview"}).json()[
        "id"
    ]
    seeded = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
                "rows": [{"id": "a", "name": "Ada"}],
            },
            "idempotency_key": "seed-pasted-update-limit",
        },
    ).json()
    requests.clear()

    raw = "id,name\na,Augusta\nb,Bob\n"
    draft = client.post(
        f"/api/projects/{project_id}/import/drafts/paste", json={"raw": raw}
    ).json()
    response = client.post(
        f"/api/projects/{project_id}/import/rows/update/preview",
        json={
            "raw": raw,
            "draft_id": draft["draft_id"],
            "destination_sheet_id": seeded["outputs"][0]["sheet_id"],
            "columns": [
                {"source_name": "id", "name": "id", "type": "text"},
                {"source_name": "name", "name": "name", "type": "text"},
            ],
            "key_columns": ["id"],
        },
    )

    assert response.status_code == 400, response.text
    assert response.json() == {
        "detail": "import.update_rows row count exceeds the deployment limit of 1"
    }
    assert len(requests) == 1
    assert requests[0][0] == project_id
    assert requests[0][1].url.path.endswith("/import/rows/update/preview")


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


def test_server_paste_update_uses_live_destination_types_and_confirmation(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Paste"}).json()["id"]
    seeded = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "score", "type": "integer"},
                ],
                "rows": [{"id": "a", "score": 1}],
            },
            "idempotency_key": "paste-live-types",
        },
    ).json()
    service = ImportDraftService(client.app.state.workspace)
    raw = "id,score,unused\na,7,secret\n"
    draft = service.paste_draft(pid, raw)
    preview_body = ImportUpdatePreviewBody(
        raw=raw,
        draft_id=draft["draft_id"],
        destination_sheet_id=seeded["outputs"][0]["sheet_id"],
        columns=[
            {"source_name": "id", "name": "id", "type": "text"},
            # Client-selected type is intentionally wrong; the live sheet wins.
            {"source_name": "score", "name": "score", "type": "text"},
            {"source_name": "unused", "name": None, "type": "text"},
        ],
        key_columns=["id"],
    )
    preview = service.preview_update(pid, preview_body)
    result = service.confirm(
        pid,
        _reviewed_paste_body(
            raw=raw,
            draft_id=draft["draft_id"],
            destination_sheet_id=seeded["outputs"][0]["sheet_id"],
            key_columns=["id"],
            confirmation=preview["confirmation"],
        ),
    )

    assert result.payload["rows"] == 1
    data = client.get(
        f"/api/projects/{pid}/sheets/{seeded['outputs'][0]['sheet_id']}/data"
    ).json()
    score = next(column for column in data["columns"] if column["name"] == "score")
    assert data["rows"][0]["cells"][str(score["id"])] == 7


def test_server_paste_update_refuses_confirmation_for_changed_source(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Paste"}).json()["id"]
    seeded = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "score", "type": "integer"},
                ],
                "rows": [{"id": "a", "score": 1}],
            },
            "idempotency_key": "paste-stale-confirmation",
        },
    ).json()
    sheet_id = seeded["outputs"][0]["sheet_id"]
    service = ImportDraftService(client.app.state.workspace)
    raw = "id,score,unused\na,7,secret\n"
    draft = service.paste_draft(pid, raw)
    preview = service.preview_update(
        pid,
        ImportUpdatePreviewBody(
            raw=raw,
            draft_id=draft["draft_id"],
            destination_sheet_id=sheet_id,
            columns=[
                {"source_name": "id", "name": "id", "type": "text"},
                {"source_name": "score", "name": "score", "type": "integer"},
                {"source_name": "unused", "name": None, "type": "text"},
            ],
            key_columns=["id"],
        ),
    )
    changed = "id,score,unused\na,8,secret\n"
    changed_draft = service.paste_draft(pid, changed)
    response = service.confirm(
        pid,
        _reviewed_paste_body(
            raw=changed,
            draft_id=changed_draft["draft_id"],
            destination_sheet_id=sheet_id,
            key_columns=["id"],
            confirmation=preview["confirmation"],
        ),
    )

    assert response.force_json_response is True
    assert response.payload["errors"][0]["code"] == "stale_import_preview"

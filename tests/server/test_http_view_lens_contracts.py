"""Strict shared View shape, binding validation and paginated real HTTP reads."""

import pytest
from fastapi.testclient import TestClient
from frisket.server.app import create_app
from frisket.contracts.http.views_lenses import SavedView, SavedViewResolved
from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG


@pytest.fixture
def view_api(tmp_path):
    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "View test"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("records")
        col = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(
            sheet, [{"amount": n} for n in range(3)], {"amount": col}
        )
        selection = {
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filters": [{"column_id": col, "condition": {"gte": 1}}],
            "order": [{"column_id": col, "dir": "desc"}],
        }
        yield client, pid, project, sheet, col, rows, selection


def test_http_crud_resolves_shared_selection_and_presentation(view_api):
    client, pid, project, sheet, col, rows, selection = view_api
    prefix = f"/api/projects/{pid}/views"
    response = client.post(
        prefix,
        json={
            "name": "  High  ",
            "selection": selection,
            "presentation": {"columns": ["amount"]},
        },
    )
    assert response.status_code == 200, response.text
    view = response.json()
    SavedView.model_validate(view)
    assert view["name"] == "High"
    assert view["unavailable_reason"] is None
    assert client.get(prefix).json() == [view]
    assert client.get(prefix, params={"sheet_id": sheet + 1}).json() == []
    assert client.get(f"{prefix}/{view['id']}").json() == view
    resolved = client.get(
        f"{prefix}/{view['id']}/resolve", params={"limit": 1, "offset": 1}
    )
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    SavedViewResolved.model_validate(payload)
    assert payload["total"] == 2
    assert [row["row_id"] for row in payload["refs"]] == rows[1:2]
    assert (
        client.patch(f"{prefix}/{view['id']}", json={"name": "Renamed"}).json()["spec"]
        == view["spec"]
    )
    definition = {**selection, "limit": 1}
    changed = client.put(
        f"{prefix}/{view['id']}/definition",
        json={"selection": definition, "presentation": {}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["spec"]["presentation"] == {}
    assert client.get(f"{prefix}/{view['id']}/resolve").json()["total"] == 1
    assert client.delete(f"{prefix}/{view['id']}").json() == {
        "ok": True,
        "deleted": view["id"],
    }
    assert client.get(f"{prefix}/{view['id']}").status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "", "selection": {}},
        {"name": "View", "filter": {}},
        {"name": "View", "selection": {"scope": {"kind": "sheet", "sheet_id": True}}},
        {
            "name": "View",
            "selection": {"scope": {"kind": "project"}, "extra": "ignored?"},
        },
        {"name": "View", "selection": {"scope": {"kind": "project"}, "limit": 0}},
    ],
)
def test_invalid_request_never_creates_view(view_api, bad):
    client, pid, project, *_ = view_api
    response = client.post(f"/api/projects/{pid}/views", json=bad)
    assert response.status_code == 422, response.text
    assert project.views() == []


def test_unavailable_binding_is_explicit_not_silently_all_rows(view_api):
    client, pid, project, sheet, col, rows, selection = view_api
    invalid = {**selection, "filters": [{"column_id": 99999, "condition": {"eq": "x"}}]}
    response = client.post(
        f"/api/projects/{pid}/views", json={"name": "Bad", "selection": invalid}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] in {
        "invalid_selection",
        "selection_unavailable",
        "invalid_filter",
    }
    assert not project.views()


def test_resolver_pagination_is_bounded_and_catalog_has_no_lenses(view_api):
    client, pid, project, sheet, col, rows, selection = view_api
    view = client.post(
        f"/api/projects/{pid}/views", json={"name": "V", "selection": selection}
    ).json()
    assert (
        client.get(
            f"/api/projects/{pid}/views/{view['id']}/resolve?limit=501"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/projects/{pid}/views/{view['id']}/resolve?offset=-1"
        ).status_code
        == 422
    )
    entries = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}
    assert entries["tenant.resolve_view.get"].browser_client
    assert not any("lens" in key for key in entries)

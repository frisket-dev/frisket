"""Facet counts under every other active filter: the preview's selection scope."""

from __future__ import annotations


def _sheet(client):
    pid = client.post("/api/projects", json={"name": "facets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("filings")
    columns = {
        "kind": project.add_column(sheet, "kind"),
        "size": project.add_column(sheet, "size", type="number"),
        "body": project.add_column(sheet, "body"),
    }
    rows = [
        {"kind": "complaint", "size": 2.7, "body": "the wages"},
        {"kind": "petition", "size": 2.4, "body": "the records"},
        {"kind": "motion", "size": 2.2, "body": "the parcel"},
        {"kind": "complaint", "size": 2.1, "body": "promotion"},
        {"kind": "motion", "size": 2.1, "body": "the subpoena"},
        {"kind": "complaint", "size": 3.1, "body": "tenants"},
    ]
    project.add_rows(sheet, rows, columns)
    return pid, sheet, {name: int(column) for name, column in columns.items()}


def _preview(client, pid, sheet, column, selection=None, **options):
    body = {
        "sheet_id": sheet,
        "input_column": column,
        "value_mode": "bounded",
        **options,
    }
    if selection is not None:
        body["selection"] = selection
    return client.post(f"/api/projects/{pid}/column-values/v1/preview", json=body)


def test_scope_counts_listed_values_within_the_selection(client):
    pid, sheet, columns = _sheet(client)
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": columns["body"], "condition": {"contains": "the"}}],
        "order": [],
        "limit": None,
    }
    response = _preview(client, pid, sheet, "kind", selection)
    assert response.status_code == 200, response.text
    payload = response.json()
    # The inventory stays the whole column, so a value with no scoped match is listed.
    assert payload["values"] == [
        {"value": "complaint", "count": 3},
        {"value": "motion", "count": 2},
        {"value": "petition", "count": 1},
    ]
    assert payload["scope"] == {
        "total_rows": 4,
        "values": [
            {"value": "complaint", "count": 1},
            {"value": "motion", "count": 2},
            {"value": "petition", "count": 1},
        ],
        "distribution_counts": None,
    }


def test_scope_counts_each_histogram_bin_of_the_full_column(client):
    pid, sheet, columns = _sheet(client)
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": columns["kind"], "condition": {"eq": "complaint"}}],
        "order": [],
        "limit": None,
    }
    payload = _preview(client, pid, sheet, "size", selection).json()
    bins = payload["distribution"]["bins"]
    assert sum(item["count"] for item in bins) == 6
    scoped = payload["scope"]["distribution_counts"]
    assert len(scoped) == len(bins)
    assert sum(scoped) == 3
    assert all(0 <= count <= item["count"] for count, item in zip(scoped, bins))
    assert scoped[0] == 1 and scoped[-1] == 1


def test_scope_ignores_the_selection_limit_only_through_its_rows(client):
    pid, sheet, _ = _sheet(client)
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [],
        "order": [],
        "limit": 2,
    }
    payload = _preview(client, pid, sheet, "kind", selection).json()
    assert payload["scope"]["total_rows"] == 2


def test_without_selection_the_preview_has_no_scope(client):
    pid, sheet, _ = _sheet(client)
    payload = _preview(client, pid, sheet, "kind").json()
    assert payload.get("scope") is None


def test_selection_for_another_sheet_is_refused(client):
    pid, sheet, _ = _sheet(client)
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet + 1000},
        "filters": [],
        "order": [],
        "limit": None,
    }
    response = _preview(client, pid, sheet, "kind", selection)
    assert response.status_code == 400
    assert "selection" in response.text

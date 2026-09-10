"""Sheet-level row-title column persistence and API behavior.

Sheet-level row-title override: `sheets.title_column_id` (schema + server
persistence). Explicit UI setter is the column '...' menu's "Use as row
title" (frontend: web/src/workspace/gridMenus.tsx); the default when unset
is computed CLIENT-SIDE (first column per the grid's current drag order,
falling back to canonical order — web/src/workbench/rowTitle.ts), so the
store only persists the explicit override itself, never a computed default.

Layer 1 (store): frisket.store.project.Project.set_sheet_title_column over
the `sheets.title_column_id` column.
Layer 2 (API): PATCH /api/projects/{pid}/sheets/{sheet_id}, and
GET /api/projects/{pid}/sheets echoing `title_column_id` per entry.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store import Project


# ---------------------------------------------------------------------------
# Layer 1: store


class TestStoreSetSheetTitleColumn:
    def test_set_and_read_back(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        col = p.add_column(sheet, "name")
        p.add_column(sheet, "other")

        assert p.sheets()[0]["title_column_id"] is None

        p.set_sheet_title_column(sheet, col)
        assert p.sheets()[0]["title_column_id"] == col
        p.close()

    def test_clear_with_none(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        col = p.add_column(sheet, "name")
        p.set_sheet_title_column(sheet, col)
        assert p.sheets()[0]["title_column_id"] == col

        p.set_sheet_title_column(sheet, None)
        assert p.sheets()[0]["title_column_id"] is None
        p.close()

    def test_rejects_column_from_another_sheet(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_a = p.add_sheet("a")
        sheet_b = p.add_sheet("b")
        col_b = p.add_column(sheet_b, "name")

        with pytest.raises(ValueError, match="not in sheet"):
            p.set_sheet_title_column(sheet_a, col_b)
        p.close()

    def test_rejects_unknown_sheet(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        with pytest.raises(KeyError, match="no sheet"):
            p.set_sheet_title_column(999999, None)
        p.close()

    def test_persists_across_reopen(self, tmp_path):
        path = tmp_path / "t.frisket"
        p = Project.create(path, name="t")
        sheet = p.add_sheet("data")
        col = p.add_column(sheet, "name")
        p.set_sheet_title_column(sheet, col)
        p.close()

        reopened = Project(path)
        assert reopened.sheets()[0]["title_column_id"] == col
        reopened.close()


def _import_csv(
    client: TestClient, pid: str, csv: str, *, filename: str = "t.csv"
) -> int:
    r = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": (filename, csv, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()["sheet_id"]


def _column_ids(client: TestClient, pid: str, sheet_id: int) -> dict[str, int]:
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    return {c["name"]: c["id"] for c in data["columns"]}


class TestSheetTitleColumnRoute:
    def test_list_sheets_reports_null_title_column_by_default(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import_csv(client, pid, "name,city\nAda,NYC\n")

        sheets = client.get(f"/api/projects/{pid}/sheets").json()
        assert sheets[0]["id"] == sheet_id
        assert sheets[0]["title_column_id"] is None

    def test_patch_sets_title_column_and_round_trips(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import_csv(client, pid, "name,city\nAda,NYC\n")
        cols = _column_ids(client, pid, sheet_id)

        patched = client.patch(
            f"/api/projects/{pid}/sheets/{sheet_id}",
            json={"title_column_id": cols["city"]},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json() == {"id": sheet_id, "title_column_id": cols["city"]}

        sheets = client.get(f"/api/projects/{pid}/sheets").json()
        assert sheets[0]["title_column_id"] == cols["city"]

    def test_patch_explicit_null_clears_the_override(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import_csv(client, pid, "name,city\nAda,NYC\n")
        cols = _column_ids(client, pid, sheet_id)
        client.patch(
            f"/api/projects/{pid}/sheets/{sheet_id}",
            json={"title_column_id": cols["city"]},
        )

        cleared = client.patch(
            f"/api/projects/{pid}/sheets/{sheet_id}", json={"title_column_id": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"id": sheet_id, "title_column_id": None}

        sheets = client.get(f"/api/projects/{pid}/sheets").json()
        assert sheets[0]["title_column_id"] is None

    def test_patch_omitted_field_leaves_the_override_untouched(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import_csv(client, pid, "name,city\nAda,NYC\n")
        cols = _column_ids(client, pid, sheet_id)
        client.patch(
            f"/api/projects/{pid}/sheets/{sheet_id}",
            json={"title_column_id": cols["city"]},
        )

        # An empty body omits the field entirely -- must NOT clear it.
        untouched = client.patch(f"/api/projects/{pid}/sheets/{sheet_id}", json={})
        assert untouched.status_code == 200, untouched.text
        assert untouched.json() == {"id": sheet_id, "title_column_id": cols["city"]}

    def test_patch_rejects_column_not_in_sheet(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import_csv(client, pid, "name,city\nAda,NYC\n")
        other_sheet_id = _import_csv(
            client, pid, "vendor\nAcme\n", filename="other.csv"
        )
        other_cols = _column_ids(client, pid, other_sheet_id)

        r = client.patch(
            f"/api/projects/{pid}/sheets/{sheet_id}",
            json={"title_column_id": other_cols["vendor"]},
        )
        assert r.status_code == 400, r.text

    def test_patch_unknown_sheet_404(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        r = client.patch(
            f"/api/projects/{pid}/sheets/999999", json={"title_column_id": None}
        )
        assert r.status_code == 404, r.text

    def test_patch_unknown_project_404(self, client):
        r = client.patch(
            "/api/projects/no-such-project/sheets/1", json={"title_column_id": None}
        )
        assert r.status_code == 404, r.text

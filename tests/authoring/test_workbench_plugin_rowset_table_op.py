from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "demo.star_summary"
ACTION_KIND = "demo.star_summary.summarize_stars"
FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins"
PLUGIN_ROOT = FIXTURE_ROOT / "demo_star_summary"
SUMMARY_COLUMNS = [
    "constellation",
    "star_count",
    "average_rating",
    "top_star",
    "source_row_count",
]


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_with_stars(
    client: TestClient,
    *,
    count: int = 130,
    group_count: int = 4,
) -> tuple[str, int, list[int], list[dict[str, Any]]]:
    project_id = client.post("/api/projects", json={"name": "Star summary"}).json()[
        "id"
    ]
    rows = _star_rows(count, group_count=group_count)
    csv = "\n".join(
        [
            "star,constellation,rating",
            *[
                f'"{row["star"]}","{row["constellation"]}",{row["rating"]}'
                for row in rows
            ],
        ]
    )
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stars.csv", csv, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id, limit=count)
    row_ids = [int(row["id"]) for row in data["rows"]]
    assert len(row_ids) == count
    return project_id, sheet_id, row_ids, rows


def _star_rows(count: int, *, group_count: int = 4) -> list[dict[str, Any]]:
    named = ["Lyra", "Orion", "Cygnus", "Draco"]
    names = (
        named[:group_count]
        if group_count <= len(named)
        else [f"Constellation {index + 1:04d}" for index in range(group_count)]
    )
    base = [4.9, 3.4, 4.2, 2.6]
    return [
        {
            "star": f"Star {index + 1:03d}",
            "constellation": names[index % len(names)],
            "rating": base[index % len(base)] + (index % 3) * 0.1,
        }
        for index in range(count)
    ]


def _expected_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["constellation"]), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for constellation, items in grouped.items():
        top = sorted(items, key=lambda item: (-float(item["rating"]), item["star"]))[0]
        average = round(
            sum(float(item["rating"]) for item in items) / len(items),
            2,
        )
        out[constellation] = {
            "star_count": len(items),
            "average_rating": average,
            "top_star": top["star"],
            "source_row_count": len(items),
        }
    return out


def _install_activate_backend(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredRuntimeBindings"]["actions"] == [ACTION_KIND]


def _rowset_action(
    *,
    sheet_id: int,
    key: str,
    target_sheet_name: str = "Star summary",
    row_ids: list[int] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Selection lives in the request scope, and the child sheet's name is a
    request field. Params declare only which columns to read."""
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": ACTION_KIND,
        "scope": scope,
        "params": {
            "group_by": "constellation",
            "value_column": "rating",
            "label_column": "star",
            **(extra_params or {}),
        },
        "sheet_name": target_sheet_name,
        "idempotency_key": key,
    }


def _run_rowset_action(
    client: TestClient,
    project_id: str,
    *,
    sheet_id: int,
    key: str,
    target_sheet_name: str = "Star summary",
    row_ids: list[int] | None = None,
    extra_params: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_rowset_action(
            sheet_id=sheet_id,
            key=key,
            target_sheet_name=target_sheet_name,
            row_ids=row_ids,
            extra_params=extra_params,
        ),
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _sheet_data(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit={limit}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _receipt(client: TestClient, project_id: str, receipt_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _child_sheet(client: TestClient, project_id: str, name: str) -> dict[str, Any]:
    response = client.get(f"/api/projects/{project_id}/sheets")
    assert response.status_code == 200, response.text
    sheets = response.json()
    if isinstance(sheets, dict):
        sheets = sheets["sheets"]
    for sheet in sheets:
        if sheet["name"] == name:
            return sheet
    raise AssertionError(f"child sheet not found: {name}")


def _ref(receipt: dict[str, Any], section: str, kind: str) -> dict[str, Any]:
    return next(item["ref"] for item in receipt[section] if item["ref"]["kind"] == kind)


def _recorded_sources(receipt: dict[str, Any]) -> dict[int, set[int]]:
    """Contributor lineage as the host recorded it: one ``materialized_row_sources``
    membership row per (summary row, source row) pair, role ``aggregate_source``."""
    membership = _ref(receipt, "evidence", "materialized_row_sources")
    assert membership["row_count"] == len(membership["rows"])
    out: dict[int, set[int]] = {}
    for row in membership["rows"]:
        assert row["role"] == "aggregate_source", row
        out.setdefault(int(row["materialized_row_id"]), set()).add(
            int(row["source_row_id"])
        )
    return out


def _values_by_column(data: dict[str, Any]) -> dict[str, dict[int, Any]]:
    columns = {int(column["id"]): str(column["name"]) for column in data["columns"]}
    out = {name: {} for name in columns.values()}
    for row in data["rows"]:
        for column_id, value in row["cells"].items():
            out[columns[int(column_id)]][int(row["id"])] = value
    return out


def test_rowset_plugin_materializes_selected_child_sheet_with_membership(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids, rows = _project_with_stars(client, count=12)
    _install_activate_backend(client, project_id)
    selected_indexes = [0, 1, 4, 5, 8, 9]
    selected_row_ids = [row_ids[index] for index in selected_indexes]
    selected_rows = [rows[index] for index in selected_indexes]

    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="selected@rowset",
        target_sheet_name="Selected star summary",
        row_ids=selected_row_ids,
    )

    assert result["status"] == "completed"
    child = _child_sheet(client, project_id, "Selected star summary")
    assert child["parent_sheet_id"] == sheet_id
    data = _sheet_data(client, project_id, int(child["id"]))
    values = _values_by_column(data)
    assert set(values) >= set(SUMMARY_COLUMNS)
    expected = _expected_summary(selected_rows)
    actual_groups = {
        str(group): row_id for row_id, group in values["constellation"].items()
    }
    assert set(actual_groups) == set(expected)
    for constellation, expected_row in expected.items():
        row_id = actual_groups[constellation]
        assert values["star_count"][row_id] == expected_row["star_count"]
        assert values["top_star"][row_id] == expected_row["top_star"]
        assert values["source_row_count"][row_id] == expected_row["source_row_count"]
        assert (
            abs(
                float(values["average_rating"][row_id])
                - float(expected_row["average_rating"])
            )
            <= 0.01
        )

    receipt = _receipt(client, project_id, result["receipt_id"])
    assert receipt["status"] == "completed"

    # The child sheet, its columns and its rows are all published as refs.
    sheet_ref = _ref(receipt, "outputs", "materialized_sheet")
    assert sheet_ref["sheet_id"] == int(child["id"])
    assert sheet_ref["parent_sheet_id"] == sheet_id
    assert sheet_ref["row_count"] == len(expected)
    column_refs = {
        item["ref"]["name"]: item["ref"]
        for item in receipt["outputs"]
        if item["ref"]["kind"] == "materialized_column"
    }
    assert set(column_refs) == set(SUMMARY_COLUMNS)
    assert all(ref["sheet_id"] == int(child["id"]) for ref in column_refs.values())
    rows_ref = _ref(receipt, "outputs", "materialized_rows")
    assert sorted(rows_ref["row_ids"]) == sorted(actual_groups.values())

    # The read evidence names exactly the rows the request selected, so the
    # summary cannot have been computed over the whole sheet.
    read_ref = _ref(receipt, "inputs", "sheet_rows_read")
    assert read_ref["sheet_id"] == sheet_id
    assert read_ref["row_ids"] == selected_row_ids
    assert [column["name"] for column in read_ref["columns"]] == [
        "constellation",
        "rating",
        "star",
    ]
    assert read_ref["source_values_hash"].startswith("sha256:")

    # Contributor lineage: one membership row per (summary row, source row).
    recorded = _recorded_sources(receipt)
    expected_membership = {
        actual_groups[constellation]: {
            selected_row_ids[position]
            for position, row in enumerate(selected_rows)
            if str(row["constellation"]) == constellation
        }
        for constellation in expected
    }
    assert recorded == expected_membership
    # Every selected row contributes, nothing outside the selection does, and
    # the groups genuinely differ instead of all sharing one source set.
    assert set().union(*recorded.values()) == set(selected_row_ids)
    assert len({frozenset(sources) for sources in recorded.values()}) == len(recorded)
    membership_ref = _ref(receipt, "evidence", "materialized_row_sources")
    assert {row["source_sheet_id"] for row in membership_ref["rows"]} == {sheet_id}


def test_rowset_plugin_catalog_declares_table_output(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, _sheet_id, _row_ids, _rows = _project_with_stars(client, count=2)
    _install_activate_backend(client, project_id)

    catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
    assert catalog.status_code == 200, catalog.text
    entry = next(
        item for item in catalog.json()["actions"] if item["kind"] == ACTION_KIND
    )
    hints = entry["ui_hints"]
    assert set(hints) == {
        "category",
        "form",
        "logical_outputs",
        "semantic_controls",
        "source_requirements",
        "typed_action",
    }
    assert hints["typed_action"] == {"creates_sheet": True}
    assert [output["key"] for output in hints["logical_outputs"]] == SUMMARY_COLUMNS
    assert [output["column_type"] for output in hints["logical_outputs"]] == [
        "text",
        "integer",
        "number",
        "text",
        "integer",
    ]
    assert [item["param"] for item in hints["source_requirements"]] == [
        "group_by",
        "value_column",
        "label_column",
    ]
    assert hints["semantic_controls"] == {
        "group_by": "column",
        "value_column": "column",
        "label_column": "column",
    }
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows", "exact_membership"],
    }
    assert "create_sheet" in entry["side_effects"]


def test_rowset_plugin_rejects_invalid_table_output(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids, _rows = _project_with_stars(client, count=3)
    _install_activate_backend(client, project_id)

    # The handler emits a row whose declared integer output holds a string.
    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="invalid-output@rowset",
        extra_params={"emit_invalid_row": True},
        expected_status=400,
    )

    assert result["status"] == "failed"
    error = result["errors"][0]
    assert error["code"] == "invalid_params"
    # The refusal names the offending output field, not merely the request.
    assert "star_count" in error["details"]["reason"]
    sheets = client.get(f"/api/projects/{project_id}/sheets").json()
    if isinstance(sheets, dict):
        sheets = sheets["sheets"]
    assert all(sheet["name"] != "Star summary" for sheet in sheets)
    assert [sheet["id"] for sheet in sheets] == [sheet_id]


def test_rowset_plugin_allows_output_over_legacy_total_cap(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # One group per source row, so the produced table is larger than the 500-row
    # total the retired subprocess transport used to cap plugin output at.
    project_id, sheet_id, row_ids, _rows = _project_with_stars(
        client, count=501, group_count=501
    )
    _install_activate_backend(client, project_id)

    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="over-legacy-total@rowset",
    )

    assert result["status"] == "completed"
    child = _child_sheet(client, project_id, "Star summary")
    data = _sheet_data(client, project_id, int(child["id"]), limit=501)
    assert data["total"] == 501
    receipt = _receipt(client, project_id, result["receipt_id"])
    assert _ref(receipt, "outputs", "materialized_sheet")["row_count"] == 501
    assert _ref(receipt, "inputs", "sheet_rows_read")["row_ids"] == row_ids
    assert len(_recorded_sources(receipt)) == 501

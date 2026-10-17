from __future__ import annotations

import json
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.engine.store.value_codec import PreparedContentRef
from frisket.server.app import create_app


def _fixture(tmp_path, values, *, column_type="text"):
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "facets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("data")
    column = project.add_column(sheet, "value", type=column_type)
    rows = project.add_rows(
        sheet, [{"value": value} for value in values], {"value": column}
    )
    return client, pid, project, sheet, column, rows


def _preview(client, pid, sheet, **options):
    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet, "input_column": "value", **options},
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("column_type", ["text", "json"])
def test_automatic_facets_omit_large_cells_without_loading_exact_values(
    tmp_path, monkeypatch, column_type
):
    large = "😀" + "a" * (5 * 1024 * 1024) + "TAIL-UNIQUE"
    large_value = large if column_type == "text" else [large]
    small = "small" if column_type == "text" else ["small", True]
    client, pid, project, sheet, column, rows = _fixture(
        tmp_path, [large_value, small, small, None], column_type=column_type
    )
    # Warm the public route before measuring request allocations.
    _preview(client, pid, sheet, value_mode="bounded")

    def forbid_exact_read(*args, **kwargs):
        pytest.fail("automatic facets must not load exact column values")

    with monkeypatch.context() as patch:
        patch.setattr(ProjectReadSnapshot, "get_values", forbid_exact_read)
        tracemalloc.start()
        try:
            response = _preview(client, pid, sheet, value_mode="bounded")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    body = response.json()
    assert len(response.content) < 64 * 1024
    assert peak < 2 * 1024 * 1024
    assert body["inventory_complete"] is False
    assert body["omitted_rows"] == 1
    assert body["value_hash"] is None
    assert body["total_rows"] == 4
    assert body["missing"] == 1
    assert body["values"] == [{"value": str(small), "count": 2}]
    if column_type == "json":
        assert {choice["label"] for choice in body["list_facet"]["choices"]} == {
            "small",
            "true",
        }
    no_match = _preview(
        client, pid, sheet, value_mode="bounded", search="TAIL-UNIQUE"
    ).json()
    assert no_match["values"] == []
    assert no_match["inventory_complete"] is False
    assert no_match["omitted_rows"] == 1
    # Exact authoring and query execution still use complete value identities.
    exact = _preview(client, pid, sheet).json()
    assert {entry["value"] for entry in exact["values"]} == {
        str(large_value),
        str(small),
    }
    assert exact["value_hash"].startswith("sha256:")
    assert project.get_values(sheet, column)[rows[0]] == large_value
    filtered = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data",
        params={
            "value_mode": "bounded",
            "filter": json.dumps({"value": {"contains": "TAIL-UNIQUE"}}),
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert [row["id"] for row in filtered.json()["rows"]] == [rows[0]]


def test_prepared_large_text_uses_same_omission_contract(tmp_path):
    client, pid, project, sheet, column, _ = _fixture(tmp_path, ["ordinary"])
    artifact = record_source_artifact(
        project, artifact_kind="document", media_type="application/pdf", page_count=1
    )
    op_id = project.append_op("media.ocr")
    project.db.execute("BEGIN IMMEDIATE")
    ref = PreparedContentStore(project).stage_reference(
        source_artifact_id=artifact["id"],
        producing_op_id=op_id,
        pages=[PreparedPageDraft(1, "x" * (1024 * 1024))],
    )
    project.db.commit()
    project.add_rows(
        sheet, [{"value": PreparedContentRef(ref.ref_id)}], {"value": column}
    )
    response = _preview(client, pid, sheet, value_mode="bounded")
    assert len(response.content) < 64 * 1024
    body = response.json()
    assert body["values"] == [{"value": "ordinary", "count": 1}]
    assert body["omitted_rows"] == 1
    assert body["inventory_complete"] is False


@pytest.mark.parametrize(
    "column_type,values",
    [
        ("number", [1, 2, 3, None]),
        ("json", [["Agency", True], ["Agency", "Region"], None]),
    ],
)
def test_small_numeric_histograms_and_collection_selectors_keep_exact_semantics(
    tmp_path, column_type, values
):
    client, pid, _, sheet, _, _ = _fixture(tmp_path, values, column_type=column_type)
    exact = _preview(client, pid, sheet).json()
    bounded = _preview(client, pid, sheet, value_mode="bounded").json()
    assert bounded["inventory_complete"] is True
    assert bounded["omitted_rows"] == 0
    for field in (
        "values",
        "distinct",
        "missing",
        "total_rows",
        "distribution",
        "list_facet",
    ):
        assert bounded[field] == exact[field]


def test_response_budget_keeps_only_complete_selectable_values(tmp_path):
    values = [f"{index:03}:" + "😀" * 1000 for index in range(30)]
    client, pid, _, sheet, _, _ = _fixture(tmp_path, values)
    response = _preview(client, pid, sheet, value_mode="bounded", limit=100)
    assert len(response.content) < 64 * 1024
    body = response.json()
    assert 0 < len(body["values"]) < len(values)
    assert [entry["value"] for entry in body["values"]] == values[: len(body["values"])]
    assert body["truncated"] is True
    assert body["inventory_complete"] is False
    assert body["omitted_rows"] == 0
    exact = _preview(client, pid, sheet, limit=100).json()
    assert [entry["value"] for entry in exact["values"]] == values


def test_response_budget_never_crops_collection_selectors(tmp_path):
    labels = [f"{index:03}:" + "z" * 2000 for index in range(30)]
    client, pid, _, sheet, _, _ = _fixture(
        tmp_path, [[label] for label in labels], column_type="json"
    )
    response = _preview(client, pid, sheet, value_mode="bounded", limit=100)
    assert len(response.content) < 64 * 1024
    body = response.json()
    choices = body["list_facet"]["choices"]
    assert 0 < len(choices) < len(labels)
    assert body["list_facet"]["truncated"] is True
    assert all(choice["selector"]["value"] in labels for choice in choices)
    assert body["inventory_complete"] is False

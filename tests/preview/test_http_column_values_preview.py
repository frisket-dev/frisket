"""HTTP contract for the read-only column distinct-values preview route.

POST /api/projects/{pid}/column-values/v1/preview is the receipt-free
enumeration surface for the resolve.substitute / resolve.combine authoring
UIs. It writes nothing (no receipt, no op, no column) and returns the
frequency-sorted distinct values plus the full-column ``value_hash`` the
commits validate as ``expected_value_hash``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store.current_cells import rebuild_current_cells
from frisket.engine.store.runs import RunResultStore

from helpers import make_client as _client


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Column Values"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("orgs")
    cols = {"org": project.add_column(sheet_id, "org")}
    project.add_rows(
        sheet_id,
        [
            {"org": "ACME Corp"},
            {"org": "ACME Corp"},
            {"org": "acme corp"},
            {"org": "Banana Farms"},
            {"org": None},
        ],
        cols,
    )
    return pid, sheet_id


def test_column_values_preview_happy_path_shape_and_no_side_effects(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    project = client.app.state.workspace.get(pid)
    ops_before = len(project.history())

    resp = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "org"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "frisket.column_values_preview.v1"
    assert body["sheet_id"] == sheet_id
    assert body["input_column"] == "org"
    assert isinstance(body["column_id"], int)
    assert body["total_rows"] == 5
    assert body["distinct"] == 3
    assert body["missing"] == 1
    assert body["values"] == [
        {"value": "ACME Corp", "count": 2},
        {"value": "Banana Farms", "count": 1},
        {"value": "acme corp", "count": 1},
    ]
    assert body["offset"] == 0
    assert body["limit"] == 500
    assert body["truncated"] is False
    assert body["value_hash"].startswith("sha256:")
    assert body["search"] is None

    # read-only: no receipt, no op, no new column
    assert len(project.history()) == ops_before
    assert [c["name"] for c in project.columns(sheet_id)] == ["org"]


def test_column_values_preview_search_and_paging_over_http(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "org",
            "search": "acme",
            "limit": 1,
            "offset": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values"] == [{"value": "ACME Corp", "count": 2}]
    assert body["search"] == "acme"
    assert body["limit"] == 1
    assert body["truncated"] is True
    # unfiltered facts survive the filter + page
    assert body["distinct"] == 3
    assert body["missing"] == 1


def test_column_values_preview_bad_column_is_bare_v1_error(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    project = client.app.state.workspace.get(pid)
    ops_before = len(project.history())

    resp = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "missing"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["code"] == "invalid_input_ref"
    assert body["field"] == "input_column"

    # error path is side-effect-free too
    assert len(project.history()) == ops_before
    assert [c["name"] for c in project.columns(sheet_id)] == ["org"]


def test_column_values_preview_bad_limit_is_bare_v1_error(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "org", "limit": "lots"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["code"] == "invalid_params"
    assert body["field"] == "limit"


def _choice_by_selector(body: dict, selector: dict) -> dict:
    token = json.dumps(selector, sort_keys=True, separators=(",", ":"))
    return next(
        choice
        for choice in body["list_facet"]["choices"]
        if json.dumps(choice["selector"], sort_keys=True, separators=(",", ":"))
        == token
    )


def test_json_list_facet_counts_typed_scalar_elements_by_distinct_row(tmp_path):
    """The list facet is additive: resolve authoring keeps receiving exact
    whole-cell values while filtering receives element-level row counts."""
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "List Facets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("calls")
    tags = project.add_column(sheet_id, "tags", type="json")
    project.add_rows(
        sheet_id,
        [
            {"tags": ["NYPD", "NYPD", "FDNY", 1, True, 9007199254740992]},
            {"tags": ["NYPD", "FDNY", "1", 1, False]},
            {"tags": []},
            {"tags": {"not": "an array"}},
            {"tags": [{"type": "organization", "text": "NYPD"}]},
        ],
        {"tags": tags},
    )

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "tags"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Existing whole-cell enumeration is not repurposed into an element list.
    assert body["distinct"] == 5
    assert body["values"] == [
        {"value": "['NYPD', 'FDNY', '1', 1, False]", "count": 1},
        {
            "value": "['NYPD', 'NYPD', 'FDNY', 1, True, 9007199254740992]",
            "count": 1,
        },
        {"value": "[]", "count": 1},
        {"value": "[{'type': 'organization', 'text': 'NYPD'}]", "count": 1},
        {"value": "{'not': 'an array'}", "count": 1},
    ]

    expected = [
        ({"kind": "scalar", "value": "NYPD"}, "NYPD", 2),
        ({"kind": "scalar", "value": "FDNY"}, "FDNY", 2),
        ({"kind": "scalar", "value": 1}, "1", 2),
        ({"kind": "scalar", "value": True}, "true", 1),
        ({"kind": "scalar", "value": "1"}, "1", 1),
        ({"kind": "scalar", "value": False}, "false", 1),
    ]
    for selector, label, count in expected:
        choice = _choice_by_selector(body, selector)
        assert choice["label"] == label
        assert choice["count"] == count
        assert isinstance(choice["key"], str) and choice["key"]

    choices = body["list_facet"]["choices"]
    assert body["list_facet"] == {
        "distinct": len(expected),
        "offset": 0,
        "limit": 500,
        "truncated": False,
        "search": None,
        "choices": choices,
    }
    assert len(choices) == len(expected)
    assert len({choice["key"] for choice in choices}) == len(choices)


def test_json_list_facet_applies_search_and_paging_to_choices(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Paged Facets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("calls")
    tags = project.add_column(sheet_id, "tags", type="json")
    project.add_rows(
        sheet_id,
        [{"tags": ["NYPD"]}, {"tags": ["NYFD"]}, {"tags": ["FDNY"]}],
        {"tags": tags},
    )

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "tags",
            "search": "ny",
            "limit": 1,
            "offset": 1,
        },
    )
    assert response.status_code == 200, response.text
    facet = response.json()["list_facet"]
    assert facet == {
        "distinct": 3,
        "offset": 1,
        "limit": 1,
        "truncated": True,
        "search": "ny",
        "choices": [
            {
                "key": facet["choices"][0]["key"],
                "label": "NYFD",
                "count": 1,
                "selector": {"kind": "scalar", "value": "NYFD"},
            }
        ],
    }


def test_entity_mentions_list_facet_projects_exact_type_and_text(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Entity Facets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("documents")
    entities = project.add_column(
        sheet_id,
        "entities",
        type="json",
        semantic_type="entity_mentions",
    )
    project.add_rows(
        sheet_id,
        [
            {
                "entities": [
                    {"type": "organization", "text": "NYPD"},
                    {"type": "organization", "text": "NYPD"},
                ]
            },
            {
                "entities": [
                    {"type": "organization", "text": "NYPD"},
                    {"type": "agency", "text": "NYPD"},
                ]
            },
            {"entities": [{"type": "organization"}]},
            {"entities": ["NYPD"]},
            {"entities": {"type": "organization", "text": "NYPD"}},
        ],
        {"entities": entities},
    )

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "entities"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    organization = _choice_by_selector(
        body,
        {"kind": "entity", "type": "organization", "text": "NYPD"},
    )
    assert organization["label"] == "NYPD"
    assert organization["count"] == 2  # duplicate mention in row one counts once

    agency = _choice_by_selector(
        body,
        {"kind": "entity", "type": "agency", "text": "NYPD"},
    )
    assert agency["label"] == "NYPD"
    assert agency["count"] == 1
    assert agency["key"] != organization["key"]
    assert body["list_facet"]["distinct"] == 2
    assert body["list_facet"]["offset"] == 0
    assert body["list_facet"]["limit"] == 500
    assert body["list_facet"]["truncated"] is False
    assert body["list_facet"]["search"] is None
    assert len(body["list_facet"]["choices"]) == 2


def test_json_list_facet_invalid_stored_json_fails_closed(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Broken JSON"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    tags = project.add_column(sheet_id, "tags", type="json")
    good, invalid = project.add_rows(
        sheet_id,
        [{"tags": ["NYPD"]}, {"tags": []}],
        {"tags": tags},
    )
    # Historical corrupt JSON cannot use strict current writers; rebuild the
    # derived projection after planting the legacy payload.
    with project.db:
        project.db.execute(
            "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
            ("{not json", invalid, tags),
        )
        rebuild_current_cells(project.db)

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "tags"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["list_facet"]["distinct"] == 1
    assert len(body["list_facet"]["choices"]) == 1
    choice = _choice_by_selector(body, {"kind": "scalar", "value": "NYPD"})
    assert choice["label"] == "NYPD"
    assert choice["count"] == 1
    assert isinstance(choice["key"], str) and choice["key"]
    assert good != invalid


def test_json_list_facet_omits_huge_integer_choice_without_overflow(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Huge JSON integer"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    tags = project.add_column(sheet_id, "tags", type="json")
    project.add_rows(sheet_id, [{"tags": [10**400]}], {"tags": tags})

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "tags"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["list_facet"]["choices"] == []

    filter_response = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={
            "filter": json.dumps(
                {"tags": {"list_contains_any": [{"kind": "scalar", "value": 10**400}]}}
            )
        },
    )
    assert filter_response.status_code == 400
    assert "JavaScript-safe" in filter_response.text


def test_json_list_facet_rejects_whole_non_strict_or_too_deep_arrays(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Strict JSON arrays"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    tags = project.add_column(sheet_id, "tags", type="json")
    non_strict, too_deep = project.add_rows(
        sheet_id,
        [{"tags": []}, {"tags": []}],
        {"tags": tags},
    )
    deeply_nested = "[" * 1_100 + '"NYPD"' + "]" * 1_100
    with project.db:
        project.db.executemany(
            "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
            [
                ('["NYPD", NaN]', non_strict, tags),
                (deeply_nested, too_deep, tags),
            ],
        )
        rebuild_current_cells(project.db)

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "tags"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["list_facet"]["choices"] == []


def test_preview_tolerant_live_resolution_preserves_edit_head_source_precedence(
    tmp_path,
):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Live precedence"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    tags = project.add_column(sheet_id, "tags", type="json", ai_generated=True)
    head_row, edit_row, malformed_edit_row = project.add_rows(
        sheet_id,
        [
            {"tags": ["source-head"]},
            {"tags": ["source-edit"]},
            {"tags": ["source-hidden-by-edit"]},
        ],
        {"tags": tags},
    )

    op_id = project.append_op("map", {"action_kind": "test.list_facet"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "test.list_facet",
        row_ids=[head_row],
        total_rows=1,
    )
    project.db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,"
        "claim_token) VALUES (?,?,?,?,?,?,?)",
        (run_id, tags, "tags", "sha256:list-facet", "create", "active", "claim:test"),
    )
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,outcome,publication_effect) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, head_row, tags, json.dumps(["run-head"]), "ok", "publish_value"),
    )
    project.db.execute(
        "UPDATE run_output_generations SET state='sealed', "
        "terminal_disposition='completed', sealed_at=datetime('now') "
        "WHERE run_id=? AND column_id=?",
        (run_id, tags),
    )
    project.db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
        (tags, head_row, run_id),
    )
    project.db.commit()

    project.apply_edits(
        [
            {"row_id": edit_row, "column_id": tags, "value": ["manual-edit"]},
            {
                "row_id": malformed_edit_row,
                "column_id": tags,
                "value": ["will-be-malformed"],
            },
        ]
    )
    with project.db:
        project.db.executemany(
            "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
            [("{not json", head_row, tags), ("{not json", edit_row, tags)],
        )
        project.db.execute(
            "UPDATE edits SET value=? WHERE row_id=? AND column_id=?",
            ("{not json", malformed_edit_row, tags),
        )
        rebuild_current_cells(project.db)

    assert project.get_values(sheet_id, tags) == {
        head_row: ["run-head"],
        edit_row: ["manual-edit"],
        malformed_edit_row: None,
    }
    with pytest.raises(json.JSONDecodeError):
        project.get_values(sheet_id, tags, [malformed_edit_row], preserve_invalid=True)

    values, refs = project.get_values_with_refs(
        sheet_id,
        tags,
        tolerate_decode_errors=True,
    )
    assert values == {
        head_row: ["run-head"],
        edit_row: ["manual-edit"],
        malformed_edit_row: None,
    }
    assert refs[head_row]["kind"] == "run_result"
    assert refs[edit_row]["kind"] == "manual_edit"
    assert refs[malformed_edit_row]["kind"] == "manual_edit"

    response = client.post(
        f"/api/projects/{pid}/column-values/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "tags"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        _choice_by_selector(body, {"kind": "scalar", "value": "run-head"})["count"] == 1
    )
    assert (
        _choice_by_selector(body, {"kind": "scalar", "value": "manual-edit"})["count"]
        == 1
    )
    assert body["list_facet"]["distinct"] == 2

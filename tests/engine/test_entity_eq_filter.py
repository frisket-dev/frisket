"""The `entity_eq` builtin filter operator over a marked entity JSON column.

`{entity_col: {entity_eq: {type, text?|fingerprint?}}}` selects rows carrying
at least one matching entity, through the shared sheet-filter evaluator
(`frisket.querysets`) — so /data, row counts, saved views, CSV/dataset export,
watches, watchlists, map points, embedding scopes and the query preview all
honor it without their own branch.

Entity cells are ordinary user JSON: they can be scalar arrays, JSON strings,
NULL, or outright malformed. `json_each()` ABORTS a statement on malformed
JSON, so the operator feeds it a guarded expression instead. The proof that
the guard is load-bearing (rather than incidentally unnecessary) is
`test_unguarded_json_each_would_abort_the_statement`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results
from frisket.ops.entities import canonicalize_entities
from frisket.querysets import (
    SheetRowSetError,
    guarded_entity_array_sql,
    resolve_sheet_filter_rows,
)


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "ner.frisket", name="ner")
    yield p
    p.close()


def _entities(*surfaces: tuple[str, str]) -> list[dict]:
    return canonicalize_entities(
        [
            {"text": text, "type": entity_type, "start": index, "end": index + 1}
            for index, (entity_type, text) in enumerate(surfaces)
        ]
    )


def _seed(project: Project):
    sheet = project.add_sheet("docs")
    title = project.add_column(sheet, "title", type="text")
    entities = project.add_column(
        sheet, "entities", type="json", semantic_type="entity_mentions"
    )
    plain_json = project.add_column(sheet, "evidence", type="json")
    rows = project.add_rows(
        sheet,
        [
            {
                "title": "one",
                "entities": _entities(
                    ("organization", "ACME Corp."), ("person", "Jon Smith")
                ),
                "evidence": _entities(("organization", "ACME Corp.")),
            },
            {
                "title": "two",
                "entities": _entities(("organization", "Acme Corporation")),
            },
            {"title": "three", "entities": _entities(("organization", "Acme Inc"))},
            {"title": "four", "entities": _entities(("person", "Smith, Jon"))},
        ],
        {"title": title, "entities": entities, "evidence": plain_json},
    )
    return sheet, entities, plain_json, rows


def _filter(payload: dict, column: str = "entities") -> str:
    return json.dumps({column: {"entity_eq": payload}})


def _rows(project: Project, sheet: int, payload: dict, column: str = "entities"):
    rowset = resolve_sheet_filter_rows(project, sheet, filter_=_filter(payload, column))
    assert rowset.total == len(rowset.row_ids)
    return set(rowset.row_ids)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_type_only_selects_every_row_holding_that_type(project):
    sheet, _entities_col, _plain, rows = _seed(project)
    assert _rows(project, sheet, {"type": "organization"}) == {
        rows[0],
        rows[1],
        rows[2],
    }
    assert _rows(project, sheet, {"type": "person"}) == {rows[0], rows[3]}
    assert _rows(project, sheet, {"type": "location"}) == set()


def test_fingerprint_selects_the_deterministic_surface_variants(project):
    sheet, _entities_col, _plain, rows = _seed(project)
    # "ACME Corp." and "Acme Corporation" share a fingerprint; "Acme Inc"
    # deliberately does not.
    assert _rows(
        project, sheet, {"type": "organization", "fingerprint": "acme corp"}
    ) == {rows[0], rows[1]}
    assert _rows(
        project, sheet, {"type": "organization", "fingerprint": "acme inc"}
    ) == {rows[2]}


def test_text_comparison_is_exact_and_case_sensitive(project):
    sheet, _entities_col, _plain, rows = _seed(project)
    assert _rows(project, sheet, {"type": "organization", "text": "ACME Corp."}) == {
        rows[0]
    }
    assert (
        _rows(project, sheet, {"type": "organization", "text": "acme corp."}) == set()
    )
    assert _rows(project, sheet, {"type": "organization", "text": "ACME Corp"}) == set()


def test_type_is_part_of_the_comparison(project):
    sheet, _entities_col, _plain, _rows_ids = _seed(project)
    # right surface, wrong type
    assert _rows(project, sheet, {"type": "person", "text": "ACME Corp."}) == set()
    assert (
        _rows(project, sheet, {"type": "person", "fingerprint": "acme corp"}) == set()
    )


def test_entity_eq_survives_a_run_backed_column(project):
    """Locks the SQL placeholder order for run-backed columns: the guard emits
    sheet_live_value_sql() three times inside json_each(), and those bind
    BEFORE the selector comparisons in the EXISTS body."""
    sheet = project.add_sheet("generated")
    title = project.add_column(sheet, "title", type="text")
    entities = project.add_column(
        sheet,
        "entities",
        type="json",
        ai_generated=True,
        semantic_type="entity_mentions",
    )
    rows = project.add_rows(
        sheet, [{"title": "hit"}, {"title": "miss"}], {"title": title}
    )
    store = RunResultStore(project)
    op = project.append_op("map", {"action_kind": "map.ner"})
    run = store.start_run(op, sheet, "map.ner", total_rows=len(rows))
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": rows[0],
                "column_id": entities,
                "value": _entities(("organization", "ACME Corp.")),
            },
            {
                "row_id": rows[1],
                "column_id": entities,
                "value": _entities(("person", "Jon Smith")),
            },
        ],
    )
    store.finish_run(run)

    assert _rows(
        project, sheet, {"type": "organization", "fingerprint": "acme corp"}
    ) == {rows[0]}
    assert _rows(project, sheet, {"type": "person"}) == {rows[1]}


# ---------------------------------------------------------------------------
# the guarded json_each core
# ---------------------------------------------------------------------------


_BAD_CELLS = {
    "scalar_array": [1, 2.5, "three", True, None],
    "json_string": "a bare json string",
    "json_number": 42,
    "json_object": {"not": "an array"},
    "empty_array": [],
    "nested_arrays": [[{"type": "person", "text": "Jon Smith"}]],
    "objects_without_entity_fields": [{"foo": "bar"}],
    "object_without_text": [{"type": "person"}],
    "object_without_type": [{"text": "Jon Smith"}],
    "empty_type": [{"type": "", "text": "Jon Smith"}],
    "empty_text": [{"type": "person", "text": ""}],
    "non_string_fields": [{"type": 5, "text": 7}],
    "null_fields": [{"type": None, "text": None}],
}


@pytest.mark.parametrize("label", sorted(_BAD_CELLS))
def test_malformed_and_non_entity_cells_match_nothing_and_never_raise(project, label):
    sheet = project.add_sheet("junk")
    entities = project.add_column(
        sheet, "entities", type="json", semantic_type="entity_mentions"
    )
    good, bad = project.add_rows(
        sheet,
        [
            {"entities": _entities(("person", "Jon Smith"))},
            {"entities": _BAD_CELLS[label]},
        ],
        {"entities": entities},
    )
    assert _rows(project, sheet, {"type": "person"}) == {good}
    assert _rows(project, sheet, {"type": "person", "text": "Jon Smith"}) == {good}
    assert bad not in _rows(project, sheet, {"type": "person"})


def test_missing_cell_and_invalid_json_match_nothing_and_never_raise(project):
    sheet = project.add_sheet("junk")
    entities = project.add_column(
        sheet, "entities", type="json", semantic_type="entity_mentions"
    )
    good, no_cell, invalid = project.add_rows(
        sheet,
        [
            {"entities": _entities(("person", "Jon Smith"))},
            {"entities": None},  # add_rows writes no cell at all -> NULL value
            {"entities": []},
        ],
        {"entities": entities},
    )
    # invalid JSON can only be written past json.dumps
    project.db.execute(
        "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
        ("{not json at all", invalid, entities),
    )
    project.db.commit()

    assert _rows(project, sheet, {"type": "person"}) == {good}
    assert _rows(project, sheet, {"type": "person", "fingerprint": "jon smith"}) == {
        good
    }
    assert no_cell not in _rows(project, sheet, {"type": "person"})


def test_unguarded_json_each_would_abort_the_statement(project):
    """The guard is load-bearing, not decoration.

    SQLite raises on json_each()/json_type() over malformed JSON and yields a
    non-object scalar row for a JSON string, so a naive walk 500s on ordinary
    user data. The same values through guarded_entity_array_sql() are simply
    empty."""
    project.db.execute("CREATE TABLE probe (value TEXT)")
    project.db.executemany(
        "INSERT INTO probe VALUES (?)",
        [("{not json at all",), ('"a json string"',), (None,), ("[]",)],
    )
    with pytest.raises(Exception, match="(?i)malformed json"):
        project.db.execute("SELECT 1 FROM probe, json_each(probe.value)").fetchall()
    with pytest.raises(Exception, match="(?i)malformed json"):
        project.db.execute("SELECT json_type(value) FROM probe").fetchall()

    guarded = guarded_entity_array_sql("probe.value")
    assert (
        project.db.execute(
            f"SELECT COUNT(*) FROM probe, json_each({guarded}) je"
        ).fetchone()[0]
        == 0
    )
    # json_valid()/json_type() answers the guard relies on, pinned explicitly
    assert project.db.execute(
        "SELECT json_valid('{not json at all'), json_valid(NULL), "
        "json_valid('\"s\"'), json_type('\"s\"'), json_type('[]')"
    ).fetchone()[:] == (0, None, 1, "text", "array")


# ---------------------------------------------------------------------------
# eligibility + payload validation (loud, never silently dropped)
# ---------------------------------------------------------------------------


def test_entity_eq_requires_a_marked_entity_mentions_column(project):
    sheet, _entities_col, _plain, _rows_ids = _seed(project)
    # a json column with entity-shaped contents but no marker
    with pytest.raises(SheetRowSetError, match="entity_mentions"):
        _rows(project, sheet, {"type": "organization"}, column="evidence")
    # a text column
    with pytest.raises(SheetRowSetError, match="entity_mentions"):
        _rows(project, sheet, {"type": "organization"}, column="title")


def test_entity_eq_rejects_a_cleared_semantic_marker(project):
    sheet, entities_col, _plain, _rows_ids = _seed(project)
    project.set_column_semantic_type(entities_col, None)
    with pytest.raises(SheetRowSetError, match="entity_mentions"):
        _rows(project, sheet, {"type": "organization"})


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("organization", "must be an object"),
        ([{"type": "person"}], "must be an object"),
        ({}, "non-empty type"),
        ({"text": "Jon Smith"}, "non-empty type"),
        ({"type": ""}, "non-empty type"),
        ({"type": 5}, "non-empty type"),
        ({"type": None}, "non-empty type"),
        ({"type": "person", "text": "a", "fingerprint": "b"}, "at most one"),
        ({"type": "person", "text": ""}, "non-empty text"),
        ({"type": "person", "text": 5}, "non-empty text"),
        ({"type": "person", "text": None}, "non-empty text"),
        ({"type": "person", "fingerprint": ""}, "non-empty fingerprint"),
        ({"type": "person", "label": "x"}, "unknown keys"),
        ({"type": "person", "kind": "fingerprint"}, "unknown keys"),
    ],
)
def test_entity_eq_rejects_malformed_payloads_loudly(project, payload, match):
    sheet, _entities_col, _plain, _rows_ids = _seed(project)
    with pytest.raises(SheetRowSetError, match=match):
        resolve_sheet_filter_rows(project, sheet, filter_=_filter(payload))


def test_entity_eq_is_not_combinable_with_another_operator_on_one_column(project):
    sheet, _entities_col, _plain, _rows_ids = _seed(project)
    bad = json.dumps(
        {"entities": {"entity_eq": {"type": "person"}, "contains": "Smith"}}
    )
    with pytest.raises(SheetRowSetError, match="one operator"):
        resolve_sheet_filter_rows(project, sheet, filter_=bad)


# ---------------------------------------------------------------------------
# boundary crossings: saved views, CSV export, grid
# ---------------------------------------------------------------------------


def _seed_client(client: TestClient) -> tuple[str, int, dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Entity Filters"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        ),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "A hit",
                "entities": _entities(("organization", "ACME Corp.")),
            },
            {
                "title": "B hit",
                "entities": _entities(("organization", "Acme Corporation")),
            },
            {
                "title": "C miss",
                "entities": _entities(("person", "Jon Smith")),
            },
        ],
        columns,
    )
    return pid, sheet_id, dict(zip("abc", row_ids))


def test_saved_views_preserve_entity_eq_filter_json(tmp_path):
    """The saved-view precedent from
    tests/test_date_range_filters.py::test_saved_views_preserve_date_range_filter_json:
    the operator payload must survive save/list byte-semantically and must not
    be dropped or stringified."""
    client = _client(tmp_path)
    pid, sheet_id, _rows = _seed_client(client)
    entity_filter = {
        "entities": {"entity_eq": {"type": "organization", "fingerprint": "acme corp"}}
    }
    response = client.post(
        f"/api/projects/{pid}/views",
        json={
            "name": "Acme mentions",
            "sheet_id": sheet_id,
            "filter": entity_filter,
            "sort": [{"column": "title", "dir": "asc"}],
        },
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["spec"]["filter"] == entity_filter

    listed = client.get(
        f"/api/projects/{pid}/views", params={"sheet_id": sheet_id}
    ).json()
    saved = next(item for item in listed if item["id"] == view["id"])
    assert saved["spec"]["filter"] == entity_filter

    # and the restored filter still selects rows
    grid = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": json.dumps(saved["spec"]["filter"])},
    )
    assert grid.status_code == 200, grid.text
    assert grid.json()["total"] == 2


def test_csv_export_returns_the_same_rows_as_the_filtered_grid(tmp_path):
    """The CSV precedent from tests/engine/test_filtered_sheet_csv_export.py:
    export and grid share one filter parser, so they cannot diverge."""
    client = _client(tmp_path)
    pid, sheet_id, rows = _seed_client(client)
    params = {
        "filter": _filter({"type": "organization", "fingerprint": "acme corp"}),
        "sort": json.dumps([{"column": "title", "dir": "asc"}]),
    }

    grid = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data", params=params)
    assert grid.status_code == 200, grid.text
    assert [row["id"] for row in grid.json()["rows"]] == [rows["a"], rows["b"]]
    assert grid.json()["total"] == 2

    exported = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={**params, "sheet_id": sheet_id, "format": "csv"},
    )
    assert exported.status_code == 200, exported.text
    assert [line.split(",")[0] for line in exported.text.strip().splitlines()] == [
        "title",
        "A hit",
        "B hit",
    ]


def test_malformed_entity_eq_fails_visibly_over_http(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, _rows = _seed_client(client)
    response = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": _filter({"type": "person", "surface": "Jon Smith"})},
    )
    assert response.status_code == 400, response.text
    assert "unknown keys" in json.dumps(response.json())


def test_entity_eq_reaches_the_shared_query_and_watch_consumers(tmp_path):
    """Every filter consumer funnels through _parse_sheet_filter /
    sheet_row_scope_query, so none of them needs its own branch. Spot-check
    a query evaluator (query preview) and a filter validator (watches)."""
    client = _client(tmp_path)
    pid, sheet_id, rows = _seed_client(client)
    query = {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"entities": {"entity_eq": {"type": "organization"}}},
    }

    preview = client.post(
        f"/api/projects/{pid}/queries/v1/preview", json={"query": query}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["total"] == 2

    watch_query = {
        "kind": "filter",
        "sheet_id": sheet_id,
        "filter": {"entities": {"entity_eq": {"type": "organization"}}},
    }
    watch = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Acme rows",
            "query": watch_query,
        },
    )
    assert watch.status_code == 200, watch.text

    bad_watch = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Broken",
            "query": {
                **watch_query,
                "filter": {"entities": {"entity_eq": {"nope": 1}}},
            },
        },
    )
    assert bad_watch.status_code == 400, bad_watch.text

    # The dataset/CSV exporter is the third shape (a separate rowset reader);
    # it is covered by test_csv_export_returns_the_same_rows_as_the_filtered_grid.
    # map_points / embeddings scope / watchlists / bulk edits reach the
    # operator through these same three entry points and need no branch of
    # their own — grep resolve_sheet_filter_rows / sheet_row_scope_query /
    # validate_sheet_filter_sort.
    assert len(rows) == 3

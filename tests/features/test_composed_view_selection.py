import pytest

from frisket.engine.views.evaluate import open_selection
from frisket.engine.views.specs import membership_hash, normalize_selection
from frisket.search_index import drain_index


@pytest.fixture
def data(make_project):
    project = make_project("views")
    sheet = project.add_sheet("records")
    text = project.add_column(sheet, "text")
    amount = project.add_column(sheet, "amount", type="number")
    rows = project.add_rows(
        sheet,
        [
            {"text": "budget a", "amount": 10},
            {"text": "budget b", "amount": 30},
            {"text": "roads", "amount": 20},
            {"text": "budget c", "amount": 20},
        ],
        {"text": text, "amount": amount},
    )
    yield project, sheet, text, amount, rows


def test_order_limit_then_pagination_and_snapshot(data):
    project, sheet, _, amount, rows = data
    spec = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": amount, "condition": {"gte": 20}}],
        "order": [{"column_id": amount, "dir": "desc"}],
        "limit": 2,
    }
    with open_selection(project, spec) as selection:
        assert selection.total == 2
        assert [hit["row_id"] for hit in selection.hits()] == [rows[1], rows[2]]
        sql, params = selection.ordered_rows_sql()
        composed = selection.snapshot.db.execute(
            f"SELECT row_id FROM ({sql}) chosen WHERE row_id<>?", [*params, rows[1]]
        ).fetchall()
        assert [row["row_id"] for row in composed] == [rows[2]]
        project.apply_edits([{"row_id": rows[0], "column_id": amount, "value": 90}])
        assert [hit["row_id"] for hit in selection.hits(offset=1, limit=2)] == [rows[2]]
    with open_selection(project, spec) as current:
        assert [hit["row_id"] for hit in current.hits()] == [rows[0], rows[1]]


def test_search_and_filter_apply_before_nonsearch_order_limit(data):
    project, sheet, _, amount, rows = data
    drain_index(project)
    with open_selection(
        project,
        {
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filters": [{"column_id": amount, "condition": {"gte": 20}}],
            "search": {"text": "budget"},
            "order": [{"column_id": amount, "dir": "asc"}],
            "limit": 1,
        },
    ) as selection:
        assert selection.total == 1
        hits = list(selection.hits())
        assert [hit["row_id"] for hit in hits] == [rows[3]]
        assert hits[0]["search_score"] is not None


def test_column_identity_survives_rename_and_hide_but_not_replacement(data):
    project, sheet, _, amount, rows = data
    spec = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": amount, "condition": {"eq": 30}}],
    }
    project.db.execute(
        "UPDATE columns SET name='renamed',hidden=1 WHERE id=?", (amount,)
    )
    project.db.commit()
    with open_selection(project, spec) as selection:
        assert [hit["row_id"] for hit in selection.hits()] == rows[1:2]
    project.db.execute("UPDATE columns SET active=0 WHERE id=?", (amount,))
    project.db.commit()
    project.add_column(sheet, "amount", type="number")
    with pytest.raises(ValueError, match="unavailable"):
        with open_selection(project, spec):
            pass


def test_unlimited_order_does_not_reset_membership_hash(data):
    project, sheet, _, amount, _ = data
    spec = normalize_selection(project, {"scope": {"kind": "sheet", "sheet_id": sheet}})
    ordered = {**spec, "order": [{"column_id": amount, "dir": "desc"}]}
    assert membership_hash(spec) == membership_hash(ordered)
    assert membership_hash({**spec, "limit": 1}) != membership_hash(
        {**ordered, "limit": 1}
    )


def test_empty_explicit_cohort_needs_no_search_preparation(data):
    project, sheet, *_ = data
    with open_selection(
        project,
        {
            "scope": {"kind": "sheet", "sheet_id": sheet, "row_ids": []},
            "search": {"text": "budget"},
        },
    ) as selection:
        assert selection.total == 0
        assert list(selection.hits()) == []


def test_similarity_filters_and_sort_share_membership_without_model_calls(tmp_path):
    from tests.ai.test_alerts_embedding_similarity import (
        _client,
        _seed,
        _create_index,
        _refresh,
        _row_id,
    )

    with _client(tmp_path) as client:
        pid, project, sheet, col = _seed(client)
        index_id = _create_index(project, pid, sheet)
        _refresh(project, pid, index_id)
        cat = _row_id(project, sheet, col, "cat")
        kitten = _row_id(project, sheet, col, "kitten")
        airplane = _row_id(project, sheet, col, "airplane")
        selection = {
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filters": [{"column_id": col, "condition": {"eq": "kitten"}}],
            "similarity": {
                "kind": "embedding_similarity",
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "embedding_index_id": index_id,
                "anchor": {"kind": "row", "row_id": cat},
            },
            "order": [{"score": "similarity", "dir": "desc"}],
            "limit": 1,
        }
        # A stale source excluded by the selected filter is not needed by this view.
        project.apply_edits(
            [{"row_id": airplane, "column_id": col, "value": "changed"}]
        )
        with open_selection(project, selection) as result:
            assert [row["row_id"] for row in result.hits()] == [kitten]
        project.apply_edits([{"row_id": kitten, "column_id": col, "value": "kitten"}])
        # The edit changes authority revision; index maintenance must rebind it.
        from frisket.engine.views.specs import SelectionUnavailable

        with pytest.raises(SelectionUnavailable, match="Refresh"):
            with open_selection(project, selection):
                pass


def test_manual_similarity_needs_explicit_query_preparation(tmp_path):
    from tests.ai.test_alerts_embedding_similarity import (
        _client,
        _seed,
        _create_index,
        _refresh,
    )
    from frisket.ai.embeddings.similarity import SimilarityError

    with _client(tmp_path) as client:
        pid, project, sheet, _ = _seed(client)
        index_id = _create_index(project, pid, sheet)
        _refresh(project, pid, index_id)
        with pytest.raises(SimilarityError, match="Prepare the saved similarity query"):
            with open_selection(
                project,
                {
                    "scope": {"kind": "sheet", "sheet_id": sheet},
                    "similarity": {
                        "kind": "embedding_similarity",
                        "scope": {"kind": "sheet", "sheet_id": sheet},
                        "embedding_index_id": index_id,
                        "anchor": {"kind": "manual_text_query", "text": "new query"},
                    },
                },
            ):
                pass


def test_incomplete_search_check_never_invents_departures(data):
    from frisket.features.alerts.service import check_alert

    project, sheet, text, _, rows = data
    drain_index(project)
    view = project.add_view(
        "Budget",
        {
            "selection": {
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "search": {"text": "budget"},
            },
            "presentation": {},
        },
    )
    alert = project.add_alert(
        "Budget changes",
        view_id=view,
        detection_policy={
            "kind": "conditions",
            "entered": True,
            "exited": True,
            "count_changed": True,
        },
    )
    first = check_alert(project, project.get_alert(alert))
    assert first["run"]["matched_rows"] == 3
    project.apply_edits(
        [{"row_id": rows[0], "column_id": text, "value": "budget amended"}]
    )
    incomplete = check_alert(project, project.get_alert(alert))
    assert incomplete["run"]["status"] == "error"
    assert incomplete["run"]["error_code"] == "selection_incomplete"
    assert project.get_alert(alert)["baseline_run_id"] == first["run_id"]
    assert not project.alert_check_events(incomplete["run_id"])
    drain_index(project)
    final = check_alert(project, project.get_alert(alert))
    assert final["run"]["matched_rows"] == 3
    assert not project.alert_check_events(final["run_id"])


def test_search_selections_borrow_one_snapshot_and_cleanup_attachments(data):
    from pathlib import Path

    project, sheet, text, amount, rows = data
    drain_index(project)
    paths = []
    with project.read_snapshot() as snapshot:
        for word, expected in [
            ("budget", [rows[0], rows[1], rows[3]]),
            ("roads", [rows[2]]),
        ]:
            with open_selection(
                snapshot,
                {
                    "scope": {"kind": "sheet", "sheet_id": sheet},
                    "search": {"text": word},
                },
            ) as selected:
                assert selected.snapshot is snapshot
                sql, params = selected.ordered_rows_sql(include_ordinal=True)
                found = snapshot.db.execute(
                    f"SELECT row_id,selection_ordinal FROM ({sql}) ORDER BY selection_ordinal",
                    params,
                ).fetchall()
                assert {row["row_id"] for row in found} == set(expected)
                assert [row["selection_ordinal"] for row in found] == list(
                    range(1, len(expected) + 1)
                )
            assert snapshot.row_count(sheet) == 4
        paths = [
            Path(row[2])
            for row in snapshot.db.execute("PRAGMA database_list")
            if row[1].startswith("view_")
        ]
        assert len(paths) == 2 and all(path.exists() for path in paths)
    assert all(not path.exists() for path in paths)


def test_view_presentation_requires_stable_ids_without_changing_membership(data):
    from frisket.engine.views.specs import normalize_view_spec

    project, sheet, text, _, rows = data
    definition = {
        "selection": {
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filters": [],
            "order": [],
            "limit": None,
        },
        "presentation": {
            "columns": [text],
            "column_groups": [
                {
                    "run_id": 4,
                    "columns": [text],
                    "label": "Output",
                    "show_confidence": False,
                    "show_justification": True,
                }
            ],
        },
    }
    normalized = normalize_view_spec(project, definition)
    assert normalized["presentation"]["columns"] == [text]
    project.db.execute("UPDATE columns SET name=? WHERE id=?", ("renamed", text))
    project.db.commit()
    assert normalize_view_spec(project, definition)["presentation"]["columns"] == [text]
    with open_selection(project, normalized["selection"]) as selected:
        assert selected.total == len(rows)
    with pytest.raises(ValueError, match="column IDs"):
        normalize_view_spec(
            project, {**definition, "presentation": {"columns": ["renamed"]}}
        )

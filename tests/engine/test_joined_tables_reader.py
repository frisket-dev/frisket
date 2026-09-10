"""Actual admitted join reads; no legacy action envelope or eager fanout."""

import pytest

from frisket.actions.join_types import JoinColumnPick, JoinKeyPair
from frisket.actions.types import RowSource, SheetRef, SheetRows, TableError
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.joined_tables_read import (
    AdmittedJoinedTablesReader,
    JoinRefreshAdmission,
    validate_joined_tables_source,
)
from frisket.engine.store import Project


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "join.frisket")
    try:
        yield project
    finally:
        project.close()


def sheet(project, name, rows, types=None):
    sheet_id = project.add_sheet(name)
    columns = {
        key: project.add_column(sheet_id, key, type=kind)
        for key, kind in (types or {key: "text" for row in rows for key in row}).items()
    }
    row_ids = project.add_rows(sheet_id, rows, columns)
    return sheet_id, columns, row_ids


def reader(
    project, left, *, row_ids=None, confirmation=None, identity=None, refresh=None
):
    return AdmittedJoinedTablesReader(
        project,
        scope=SheetRows(sheet_id=left, row_ids=row_ids),
        action_kind="derive.join",
        request_identity=identity or {"action_id": "derive.join", "output_names": {}},
        confirmation=confirmation,
        refresh_admission=refresh,
    )


def read(reader, right, **options):
    return reader.read(
        SheetRef(sheet_id=right),
        join_keys=options.pop(
            "join_keys", [JoinKeyPair(left_column="k", right_column="k")]
        ),
        **options,
    )


@pytest.mark.parametrize(
    "how,expected",
    [
        ("inner", ["both"]),
        ("left", ["both", "left_only"]),
        ("right", ["both", "right_only"]),
        ("outer", ["both", "left_only", "right_only"]),
    ],
)
def test_modes_order_roles_and_nulls(project, how, expected):
    left, _, left_rows = sheet(
        project, "Left", [{"k": "a", "v": "left"}, {"k": None, "v": "null"}]
    )
    right, _, right_rows = sheet(
        project, "Right", [{"k": "a", "v": "right"}, {"k": None, "v": "null"}]
    )
    admitted = reader(project, left)
    result = read(admitted, right, how=how, indicator=True)
    assert [column.key for column in result.schema] == [
        "k",
        "v_left",
        "v_right",
        "_merge",
    ]
    rows = list(result.rows)
    assert [row.output.root["_merge"] for row in rows] == expected
    for row in rows:
        admitted.validate_lineage(row.sources, row.parent)
        assert {admitted.source_roles[source] for source in row.sources} <= {
            "join_left",
            "join_right",
        }
    assert [(source.sheet_id, source.row_id) for source in rows[0].sources] == [
        (left, left_rows[0]),
        (right, right_rows[0]),
    ]
    if how in ("right", "outer"):
        assert rows[-1].parent is None
    assert admitted.facts[0]["stats"]["null_left_rows"] == 1
    assert admitted.facts[0]["stats"]["null_right_rows"] == 1


def test_repeated_projection_reserves_natural_names(project):
    left, _, _ = sheet(
        project,
        "Left",
        [{"k": "a", "x": "L", "x_left": "natural", "x_left_2": "natural2"}],
    )
    right, _, _ = sheet(project, "Right", [{"k": "a", "x": "R"}])
    picks = [
        JoinColumnPick(side="right", column="x"),
        JoinColumnPick(side="left", column="x"),
        JoinColumnPick(side="left", column="x_left"),
        JoinColumnPick(side="left", column="x"),
        JoinColumnPick(side="left", column="x_left_2"),
    ]
    result = read(reader(project, left), right, columns=picks)
    assert [column.key for column in result.schema] == [
        "k",
        "x_right",
        "x_left_3",
        "x_left",
        "x_left_4",
        "x_left_2",
    ]
    assert list(result.rows)[0].output.root == {
        "k": "a",
        "x_right": "R",
        "x_left_3": "L",
        "x_left": "natural",
        "x_left_4": "L",
        "x_left_2": "natural2",
    }


def test_indicator_key_collision_is_resolved_without_renaming_indicator(project):
    left, _, _ = sheet(project, "Left", [{"_merge": "a"}])
    right, _, _ = sheet(project, "Right", [{"_merge": "a"}])
    result = read(
        reader(project, left),
        right,
        indicator=True,
        join_keys=[JoinKeyPair(left_column="_merge", right_column="_merge")],
    )
    assert list(result.rows)[0].output.root == {"_merge_2": "a", "_merge": "both"}


def test_duplicate_fanout_is_lazy_and_exact_confirmation_binds_sources(
    project, monkeypatch
):
    from frisket.engine.executor import joined_tables_read as module

    left, columns, left_rows = sheet(
        project, "Left", [{"k": "a", "v": "one"}, {"k": "a", "v": "two"}]
    )
    right, _, _ = sheet(project, "Right", [{"k": "a"}] * 3)
    original = module.iter_join_records
    calls = []

    def expand(**kwargs):
        calls.append(True)
        yield from original(**kwargs)

    monkeypatch.setattr(module, "iter_join_records", expand)
    produced = read(reader(project, left), right, max_output_rows=5)
    assert len(produced.schema) == 2
    assert not calls
    with pytest.raises(TableReadRefused) as refusal:
        list(produced.rows)
    error = refusal.value.error
    assert error.code == "join_fanout_requires_confirmation"
    assert error.details["estimated_rows"] == 6
    assert not calls
    token = error.details["promise_set_hash"]
    with pytest.raises(TableReadRefused):
        list(
            read(
                reader(project, left, confirmation="0" * 64), right, max_output_rows=5
            ).rows
        )
    assert (
        len(
            list(
                read(
                    reader(project, left, confirmation=token), right, max_output_rows=5
                ).rows
            )
        )
        == 6
    )
    assert len(calls) == 1
    # Same cardinality, different values: the prior confirmation is stale.
    project.apply_edits(
        [{"row_id": left_rows[0], "column_id": columns["v"], "value": "changed"}]
    )
    with pytest.raises(TableReadRefused):
        list(
            read(
                reader(project, left, confirmation=token), right, max_output_rows=5
            ).rows
        )
    assert len(calls) == 1


def test_confirmation_binds_request_identity(project):
    left, _, _ = sheet(project, "Left", [{"k": "a"}] * 2)
    right, _, _ = sheet(project, "Right", [{"k": "a"}] * 2)
    with pytest.raises(TableReadRefused) as refusal:
        list(read(reader(project, left), right, max_output_rows=1).rows)
    token = refusal.value.error.details["promise_set_hash"]
    with pytest.raises(TableReadRefused):
        list(
            read(
                reader(
                    project,
                    left,
                    confirmation=token,
                    identity={
                        "action_id": "derive.join",
                        "output_names": {"k": "other"},
                    },
                ),
                right,
                max_output_rows=1,
            ).rows
        )


def test_refresh_does_not_reuse_saved_consent_and_binds_current_target(project):
    from dataclasses import replace

    left, _, _ = sheet(project, "Left", [{"k": "a"}] * 2)
    right, _, _ = sheet(project, "Right", [{"k": "a"}] * 2)
    admission = JoinRefreshAdmission("sheet.refresh", "fresh-request", 31, 42, None)
    with pytest.raises(TableReadRefused) as refusal:
        list(
            read(
                reader(project, left, refresh=admission), right, max_output_rows=1
            ).rows
        )
    token = refusal.value.error.details["promise_set_hash"]
    assert refusal.value.error.action_kind == "sheet.refresh"
    # A valid hash supplied through the saved request is still NOT current consent.
    with pytest.raises(TableReadRefused):
        list(
            read(
                reader(project, left, confirmation=token, refresh=admission),
                right,
                max_output_rows=1,
            ).rows
        )
    approved = replace(admission, confirmation=token)
    assert (
        len(
            list(
                read(
                    reader(project, left, refresh=approved), right, max_output_rows=1
                ).rows
            )
        )
        == 4
    )
    for stale in (
        replace(approved, sheet_id=32),
        replace(approved, parent_op_id=43),
        replace(approved, request_hash="different"),
    ):
        with pytest.raises(TableReadRefused):
            list(
                read(
                    reader(project, left, refresh=stale), right, max_output_rows=1
                ).rows
            )


def test_revalidation_detects_source_type_change(project):
    left, _, _ = sheet(project, "Left", [{"k": "1"}])
    right, columns, _ = sheet(project, "Right", [{"k": "1"}])
    admitted = reader(project, left)
    assert len(list(read(admitted, right).rows)) == 1
    project.db.execute("UPDATE columns SET type='integer' WHERE id=?", (columns["k"],))
    project.db.commit()
    with pytest.raises(TableError) as refusal:
        admitted.revalidate()
    assert refusal.value.code == "stale_input"


@pytest.mark.parametrize("implicit", [True, False])
def test_column_roster_changes_only_invalidate_default_projection(project, implicit):
    left, _, _ = sheet(project, "Left", [{"k": "a", "v": "L"}] * 2)
    right, _, _ = sheet(project, "Right", [{"k": "a"}] * 2)
    picks = None if implicit else [JoinColumnPick(side="left", column="v")]
    admitted = reader(project, left)
    with pytest.raises(TableReadRefused) as refusal:
        list(read(admitted, right, columns=picks, max_output_rows=1).rows)
    token = refusal.value.error.details["promise_set_hash"]
    project.add_column(right, "new_column", type="text")
    if implicit:
        with pytest.raises(TableError):
            admitted.revalidate()
        with pytest.raises(TableReadRefused):
            list(
                read(
                    reader(project, left, confirmation=token),
                    right,
                    columns=picks,
                    max_output_rows=1,
                ).rows
            )
    else:
        admitted.revalidate()
        assert (
            len(
                list(
                    read(
                        reader(project, left, confirmation=token),
                        right,
                        columns=picks,
                        max_output_rows=1,
                    ).rows
                )
            )
            == 4
        )


def test_empty_output_still_reads_both_sheets_and_detects_new_rows(project):
    left, _, _ = sheet(project, "Left", [], {"k": "text"})
    right, columns, _ = sheet(project, "Right", [], {"k": "text"})
    admitted = reader(project, left)
    assert list(read(admitted, right).rows) == []
    fact = admitted.facts[0]
    assert (fact["left_sheet_id"], fact["right_sheet_id"]) == (left, right)
    assert fact["left"]["row_ids"] == fact["right"]["row_ids"] == []
    validate_joined_tables_source(project, fact)
    project.add_rows(right, [{"k": "new"}], columns)
    with pytest.raises(TableError, match="changed"):
        validate_joined_tables_source(project, fact)


def test_subset_limits_left_only_and_lineage_rejects_forged_pair(project):
    left, _, left_rows = sheet(project, "Left", [{"k": "a"}, {"k": "b"}])
    right, _, _ = sheet(project, "Right", [{"k": "a"}, {"k": "b"}])
    admitted = reader(project, left, row_ids=[left_rows[0]])
    rows = list(read(admitted, right, how="outer").rows)
    assert len(rows) == 2
    assert rows[1].output.root == {"k": "b"}
    with pytest.raises(TableError):
        admitted.validate_lineage((rows[0].parent, rows[1].sources[0]), rows[0].parent)
    source = rows[0].parent
    with pytest.raises(TableError):
        admitted.validate_lineage((RowSource(source.sheet_id, source.row_id),), None)


@pytest.mark.parametrize(
    "change",
    ["missing-key", "missing-projection", "self", "foreign-row", "duplicate-key"],
)
def test_invalid_references_are_refused_before_expansion(project, change):
    left, _, _ = sheet(project, "Left", [{"k": "a"}])
    right, _, right_rows = sheet(project, "Right", [{"k": "a"}])
    kwargs = {}
    if change == "missing-key":
        kwargs["join_keys"] = [JoinKeyPair(left_column="missing", right_column="k")]
    elif change == "missing-projection":
        kwargs["columns"] = [JoinColumnPick(side="right", column="missing")]
    elif change == "self":
        right = left
    elif change == "duplicate-key":
        kwargs["join_keys"] = [JoinKeyPair(left_column="k", right_column="k")] * 2
    admitted = reader(
        project, left, row_ids=right_rows if change == "foreign-row" else None
    )
    with pytest.raises(TableError) as refusal:
        read(admitted, right, **kwargs)
    assert refusal.value.code == "invalid_input_ref"


@pytest.mark.parametrize(
    "left_type,right_type,left_value,right_value,matches,output_type",
    [
        ("integer", "number", 1, 1.0, True, "number"),
        ("text", "text", "A", "a", False, "text"),
        ("boolean", "boolean", True, False, False, "boolean"),
        ("text", "integer", "1", 1, True, "text"),
    ],
)
def test_typed_key_equality(
    project, left_type, right_type, left_value, right_value, matches, output_type
):
    left, _, _ = sheet(project, "Left", [{"k": left_value}], {"k": left_type})
    right, _, _ = sheet(project, "Right", [{"k": right_value}], {"k": right_type})
    produced = read(reader(project, left), right)
    assert len(list(produced.rows)) == int(matches)
    assert produced.schema[0].type == output_type
    assert bool(produced.warnings) == (left_type == "text" and right_type == "integer")


@pytest.mark.parametrize("numeric_left", [True, False])
@pytest.mark.parametrize("how", ["inner", "outer"])
def test_widened_keys_publish_through_table_host(project, numeric_left, how):
    from frisket.actions.core import RegisteredAction
    from frisket.actions.joins import JOIN
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.table_action import run_typed_create_sheet_action

    left_values = [1, 2] if numeric_left else ["1", "2"]
    right_values = ["1", "3"] if numeric_left else [1, 3]
    left_type, right_type = ("integer", "text") if numeric_left else ("text", "integer")
    left, _, _ = sheet(
        project, "Left", [{"k": value} for value in left_values], {"k": left_type}
    )
    right, _, _ = sheet(
        project, "Right", [{"k": value} for value in right_values], {"k": right_type}
    )
    request = ActionRequest(
        action_id="derive.join",
        scope=SheetRows(sheet_id=left),
        sheet_name="Joined",
        idempotency_key="widened-join",
        params={
            "right": {"sheet_id": right},
            "join_keys": [{"left_column": "k", "right_column": "k"}],
            "how": how,
            "columns": [
                {"side": "left", "column": "k"},
                {"side": "right", "column": "k"},
            ],
        },
    )
    result = run_typed_create_sheet_action(
        project,
        "test",
        BoundTypedActionRequest.bind(RegisteredAction("derive.join", JOIN), request),
    )
    assert result.status == "completed", result.errors
    joined = next(item["id"] for item in project.sheets() if item["name"] == "Joined")
    columns = {column["name"]: column for column in project.columns(joined)}
    assert {name: column["type"] for name, column in columns.items()} == {
        "k": "text",
        "k_left": left_type,
        "k_right": right_type,
    }
    rows = project.visible_row_ids(joined)
    values = {
        name: [project.get_values(joined, column["id"]).get(row) for row in rows]
        for name, column in columns.items()
    }
    assert values["k"] == (["1"] if how == "inner" else ["1", "2", "3"])
    assert values["k_left"] == (
        [left_values[0]] if how == "inner" else [*left_values, None]
    )
    assert values["k_right"] == (
        [right_values[0]]
        if how == "inner"
        else [right_values[0], None, right_values[1]]
    )

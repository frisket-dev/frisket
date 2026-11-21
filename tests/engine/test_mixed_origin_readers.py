from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import pytest
from helpers import write_claimed_test_results

from frisket.contracts.http.history_review import ColumnRunsPage
from frisket.contracts.http.models import SheetData
from frisket.engine.executor import actions as executor_actions
from frisket.engine.store import Project
from frisket.engine.store.cells import MixedOriginReplayUnsupported
from frisket.engine.store.column_descriptors import set_descriptor
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.value_codec import encode_stored_value
from frisket.preview.entity_mentions import (
    EntityMentionsPreviewError,
    resolve_entity_mentions_preview,
)
from frisket.querysets import resolve_sheet_filter_rows
from helpers import export_sheet_csv_text
from frisket.server.run_payloads import column_run_provenance_payload
from frisket.server.services.sheet_grid import _sheet_data_payload


@dataclass(frozen=True)
class _MixedReaderFixture:
    project: Project
    sheet_id: int
    source_column_id: int
    output_column_id: int
    row_ids: list[int]
    first_run_id: int
    first_op_id: int
    second_run_id: int
    second_op_id: int
    edit_op_id: int


def test_scalar_only_ai_results_are_not_a_live_reader_lane(make_project) -> None:
    """A stray scalar pointer cannot make headless AI results current again."""

    project = make_project("scalar-only-reader")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(sheet_id, "generated", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "value"}, {"source": "failure"}],
        {"source": source_column_id},
    )
    op_id = project.append_change("map", {"phase": "retired scalar fixture"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "test.retired_scalar_fixture",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    # Results with no generation or publication effect behind a scalar
    # pointer predate the generation protocol; no current writer makes them.
    with write_transaction(project.db):
        project.db.executemany(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value_kind,value,confidence,error,outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    row_ids[0],
                    output_column_id,
                    *encode_stored_value("retired-result"),
                    0.99,
                    None,
                    "ok",
                ),
                (
                    run_id,
                    row_ids[1],
                    output_column_id,
                    None,
                    None,
                    None,
                    "retired failure",
                    "model_error",
                ),
            ],
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (run_id, output_column_id),
        )

    assert (
        resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"eq": "retired-result"}}),
        ).row_ids
        == []
    )
    assert (
        resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"failed": "any"}}),
        ).row_ids
        == []
    )

    grid = _sheet_data_payload(
        project,
        sheet_id,
        project.columns(sheet_id),
        row_ids,
        total=len(row_ids),
    )
    column = next(
        item for item in grid["columns"] if int(item["id"]) == output_column_id
    )
    assert column["generation_managed"] is False
    assert column["current_run_id"] is None
    assert column["latest_run_id"] is None
    assert column["replay_pending_count"] == 0
    for row in grid["rows"]:
        assert row["cells"][str(output_column_id)] is None
        cell_meta = row["meta"][str(output_column_id)]
        assert "state" not in cell_meta
        assert "confidence" not in cell_meta
        assert "error" not in cell_meta
        assert "pending_value" not in cell_meta


@pytest.fixture
def mixed_reader(make_project) -> _MixedReaderFixture:
    project = make_project("Mixed readers")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(sheet_id, "generated", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [
            {"source": "one", "generated": "source-value"},
            {"source": "two", "generated": "source-null"},
            {"source": "three", "generated": "source-error"},
            {"source": "four", "generated": "source-untargeted"},
        ],
        {"source": source_column_id, "generated": output_column_id},
    )

    runs = RunResultStore(project)
    first_op_id = project.append_change("map", {"phase": "first"})
    first_run_id = runs.start_run(
        first_op_id,
        sheet_id,
        "test.mixed_reader_fixture",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    first_values = [
        "first-value",
        "first-null-old",
        "first-error-old",
        "first-untargeted",
    ]
    write_claimed_test_results(
        project,
        first_run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": value,
                "confidence": 0.42 if position == 3 else None,
                "justification": (
                    "retained first-generation evidence" if position == 3 else None
                ),
            }
            for position, (row_id, value) in enumerate(
                zip(row_ids, first_values, strict=True)
            )
        ],
        output_roles={output_column_id: "generated"},
    )
    runs.finish_run(first_run_id)

    second_op_id = project.append_change("map", {"phase": "second"})
    second_run_id = runs.start_run(
        second_op_id,
        sheet_id,
        "test.mixed_reader_fixture",
        row_ids=row_ids[:3],
        total_rows=3,
    )
    write_claimed_test_results(
        project,
        second_run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_column_id,
                "value": "second-value",
                "confidence": 0.91,
                "justification": "new exact value",
            },
            {
                "row_id": row_ids[1],
                "column_id": output_column_id,
                "value": None,
                "outcome": "ok",
            },
            {
                "row_id": row_ids[2],
                "column_id": output_column_id,
                "value": None,
                "error": "deliberate mixed-reader error",
                "outcome": "model_error",
            },
        ],
    )
    runs.finish_run(second_run_id)

    # Leave the retired scalar pointed at the newest subset run. Exact readers
    # must still retain the first head on the untargeted row.
    with write_transaction(runs.db):
        runs.point_column_at_run(second_op_id, output_column_id, second_run_id)
    edit_op_id = project.apply_edits(
        [
            {
                "row_id": row_ids[0],
                "column_id": output_column_id,
                "value": "manual-value",
            }
        ],
        label="manual mixed-reader overlay",
    )

    fixture = _MixedReaderFixture(
        project=project,
        sheet_id=sheet_id,
        source_column_id=source_column_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        first_run_id=first_run_id,
        first_op_id=first_op_id,
        second_run_id=second_run_id,
        second_op_id=second_op_id,
        edit_op_id=edit_op_id,
    )
    yield fixture


def test_exact_heads_resolve_value_null_error_and_manual_overlay(
    mixed_reader: _MixedReaderFixture,
) -> None:
    fixture = mixed_reader
    values, refs = fixture.project.get_values_with_refs(
        fixture.sheet_id,
        fixture.output_column_id,
        row_ids=fixture.row_ids,
    )
    assert values == {
        fixture.row_ids[0]: "manual-value",
        fixture.row_ids[1]: None,
        fixture.row_ids[2]: None,
        fixture.row_ids[3]: "first-untargeted",
    }
    assert refs[fixture.row_ids[0]] == {
        "kind": "manual_edit",
        "op_id": fixture.edit_op_id,
        "row_id": fixture.row_ids[0],
        "column_id": fixture.output_column_id,
        "run_id": None,
        "outcome": "completed",
    }
    for row_id in fixture.row_ids[1:3]:
        assert refs[row_id] == {
            "kind": "run_result",
            "op_id": fixture.second_op_id,
            "row_id": row_id,
            "column_id": fixture.output_column_id,
            "run_id": fixture.second_run_id,
            "outcome": "completed" if row_id == fixture.row_ids[1] else "failed",
        }
    assert refs[fixture.row_ids[3]] == {
        "kind": "run_result",
        "op_id": fixture.first_op_id,
        "row_id": fixture.row_ids[3],
        "column_id": fixture.output_column_id,
        "run_id": fixture.first_run_id,
        "outcome": "completed",
    }

    from frisket.engine.store.runs import RunResultStore

    coordinate = (fixture.second_run_id, fixture.row_ids[0], fixture.output_column_id)
    saved = RunResultStore(fixture.project).decoded_result_rows([coordinate])
    assert saved[coordinate]["value"] == "second-value"

    generations = ResultGenerationStore(fixture.project)
    assert generations.has_mixed_origins(fixture.output_column_id)
    assert set(generations.origin_run_ids(fixture.output_column_id, limit=2)) == {
        fixture.first_run_id,
        fixture.second_run_id,
    }
    assert generations.origin_run_ids(
        fixture.output_column_id,
        row_ids=[fixture.row_ids[3]],
        limit=2,
    ) == [fixture.first_run_id]


def test_grid_filter_sort_and_export_follow_exact_heads(
    mixed_reader: _MixedReaderFixture,
) -> None:
    fixture = mixed_reader
    project = fixture.project
    output_name = "generated"

    manual = resolve_sheet_filter_rows(
        project,
        fixture.sheet_id,
        filter_=json.dumps({output_name: {"eq": "manual-value"}}),
    )
    assert manual.row_ids == [fixture.row_ids[0]]
    hidden_source = resolve_sheet_filter_rows(
        project,
        fixture.sheet_id,
        filter_=json.dumps({output_name: {"eq": "source-error"}}),
    )
    assert hidden_source.row_ids == []
    hidden_source_null = resolve_sheet_filter_rows(
        project,
        fixture.sheet_id,
        filter_=json.dumps({output_name: {"eq": "source-null"}}),
    )
    assert hidden_source_null.row_ids == []
    failed = resolve_sheet_filter_rows(
        project,
        fixture.sheet_id,
        filter_=json.dumps({output_name: {"failed": "any"}}),
    )
    assert failed.row_ids == [fixture.row_ids[2]]
    sorted_rows = resolve_sheet_filter_rows(
        project,
        fixture.sheet_id,
        sort=json.dumps([{"column": output_name, "dir": "asc"}]),
    )
    assert sorted_rows.row_ids == [
        fixture.row_ids[3],
        fixture.row_ids[0],
        fixture.row_ids[1],
        fixture.row_ids[2],
    ]

    grid = _sheet_data_payload(
        project,
        fixture.sheet_id,
        project.columns(fixture.sheet_id),
        fixture.row_ids,
        total=len(fixture.row_ids),
    )
    grid_rows = {int(row["id"]): row for row in grid["rows"]}
    output_key = str(fixture.output_column_id)
    null_row = grid_rows[fixture.row_ids[1]]
    assert null_row["cells"][output_key] is None
    assert null_row["meta"][output_key]["current_value_ref"]["run_id"] == (
        fixture.second_run_id
    )
    assert null_row["meta"][output_key].get("state") != "incomplete"
    error_row = grid_rows[fixture.row_ids[2]]
    assert error_row["cells"][output_key] is None
    assert error_row["meta"][output_key]["state"] == "error"
    assert error_row["meta"][output_key]["error"] == ("deliberate mixed-reader error")
    assert error_row["meta"][output_key]["outcome"] == "model_error"
    assert error_row["meta"][output_key]["current_value_ref"] == {
        "kind": "run_result",
        "op_id": fixture.second_op_id,
        "row_id": fixture.row_ids[2],
        "column_id": fixture.output_column_id,
        "run_id": fixture.second_run_id,
        "outcome": "failed",
    }
    assert (
        grid_rows[fixture.row_ids[3]]["meta"][output_key]["current_value_ref"]["run_id"]
        == fixture.first_run_id
    )
    assert grid_rows[fixture.row_ids[3]]["meta"][output_key]["confidence"] == (
        pytest.approx(0.42)
    )
    assert grid_rows[fixture.row_ids[3]]["meta"][output_key]["justification"] == (
        "retained first-generation evidence"
    )
    assert (
        next(
            column
            for column in grid["columns"]
            if int(column["id"]) == fixture.output_column_id
        )["replay_pending_count"]
        == 0
    )

    csv_text = export_sheet_csv_text(project, fixture.sheet_id, row_ids=fixture.row_ids)
    assert csv_text.splitlines() == [
        "source,generated",
        "one,manual-value",
        "two,",
        "three,",
        "four,first-untargeted",
    ]

    with pytest.raises(MixedOriginReplayUnsupported) as excinfo:
        project.pending_replay_count(fixture.sheet_id, fixture.output_column_id)
    assert excinfo.value.code == "mixed_origin_replay_unsupported"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE column_id=?",
            (fixture.output_column_id,),
        ).fetchone()[0]
        == 0
    )


def test_managed_sheet_grid_wire_separates_cell_authority_from_latest_family(
    mixed_reader: _MixedReaderFixture,
) -> None:
    fixture = mixed_reader
    grid = _sheet_data_payload(
        fixture.project,
        fixture.sheet_id,
        fixture.project.columns(fixture.sheet_id),
        fixture.row_ids,
        total=len(fixture.row_ids),
    )
    column = next(
        item for item in grid["columns"] if int(item["id"]) == fixture.output_column_id
    )
    rows = {int(row["id"]): row for row in grid["rows"]}
    output_key = str(fixture.output_column_id)

    # Generated columns publish the retired scalar as null. The newest applied output
    # family is separate display metadata, while mixed-ness is derived from
    # the exact head set.
    assert {
        "column": {
            "current_run_id": column.get("current_run_id"),
            "latest_run_id": column.get("latest_run_id"),
            "generation_managed": column.get("generation_managed"),
            "mixed_origins": column.get("mixed_origins"),
        },
        "active_error": {
            "value": rows[fixture.row_ids[2]]["cells"][output_key],
            "state": rows[fixture.row_ids[2]]["meta"][output_key]["state"],
            "run_id": rows[fixture.row_ids[2]]["meta"][output_key]["current_value_ref"][
                "run_id"
            ],
        },
        "untargeted_old_head": {
            "value": rows[fixture.row_ids[3]]["cells"][output_key],
            "run_id": rows[fixture.row_ids[3]]["meta"][output_key]["current_value_ref"][
                "run_id"
            ],
        },
    } == {
        "column": {
            "current_run_id": None,
            "latest_run_id": fixture.second_run_id,
            "generation_managed": True,
            "mixed_origins": True,
        },
        "active_error": {
            "value": None,
            "state": "error",
            "run_id": fixture.second_run_id,
        },
        "untargeted_old_head": {
            "value": "first-untargeted",
            "run_id": fixture.first_run_id,
        },
    }
    SheetData.model_validate(grid)

    from frisket.engine.store.op_log import step_operation

    fixture.project.db.execute("BEGIN IMMEDIATE")
    step_operation(fixture.project, "revert", change_id=fixture.first_op_id)
    fixture.project.db.commit()
    single_origin_grid = _sheet_data_payload(
        fixture.project,
        fixture.sheet_id,
        fixture.project.columns(fixture.sheet_id),
        fixture.row_ids,
        total=len(fixture.row_ids),
    )
    single_origin_column = next(
        item
        for item in single_origin_grid["columns"]
        if int(item["id"]) == fixture.output_column_id
    )
    assert single_origin_column["mixed_origins"] is False


def test_managed_column_history_exposes_latest_without_calling_it_current(
    mixed_reader: _MixedReaderFixture,
) -> None:
    fixture = mixed_reader
    payload = column_run_provenance_payload(
        fixture.project,
        fixture.output_column_id,
        offset=0,
        limit=20,
    )

    # History may offer the latest family for labels, grouping, and action
    # replay, but it must not turn that convenience into value authority.
    assert {
        "column": {
            "current_run_id": payload["column"].get("current_run_id"),
            "latest_run_id": payload["column"].get("latest_run_id"),
            "mixed_origins": payload["column"].get("mixed_origins"),
        },
        "current_run": payload.get("current_run"),
        "current_run_loaded": payload.get("current_run_loaded"),
        "latest_run_id": (payload.get("latest_run") or {}).get("run_id"),
        "latest_run_loaded": payload.get("latest_run_loaded"),
        "current_flags": [run["current"] for run in payload["runs"]],
    } == {
        "column": {
            "current_run_id": None,
            "latest_run_id": fixture.second_run_id,
            "mixed_origins": True,
        },
        "current_run": None,
        "current_run_loaded": False,
        "latest_run_id": fixture.second_run_id,
        "latest_run_loaded": True,
        "current_flags": [False, False],
    }
    ColumnRunsPage.model_validate(payload)


@pytest.mark.parametrize("scalar_mirror", ["null", "stale"])
def test_single_origin_replay_uses_heads_when_scalar_mirror_is_unusable(
    mixed_reader: _MixedReaderFixture,
    scalar_mirror: str,
) -> None:
    fixture = mixed_reader
    project = fixture.project

    # Selectively revert the old generation, leaving the newer subset and
    # manual edit intact. Deleting a disposable head cannot change authority.
    # The compatibility scalar is either cleared or still points at the old run.
    from frisket.engine.store.op_log import step_operation

    project.db.execute("BEGIN IMMEDIATE")
    step_operation(project, "revert", change_id=fixture.first_op_id)
    project.db.execute(
        "UPDATE columns SET current_run_id=? WHERE id=?",
        (
            None if scalar_mirror == "null" else fixture.first_run_id,
            fixture.output_column_id,
        ),
    )
    project.db.commit()
    assert ResultGenerationStore(project).origin_run_ids(
        fixture.output_column_id, limit=2
    ) == [fixture.second_run_id]

    pending = project.pending_replay_values(
        fixture.sheet_id,
        fixture.output_column_id,
        row_ids=[fixture.row_ids[0]],
    )
    assert pending[fixture.row_ids[0]]["fresh_value"] == "second-value"
    assert pending[fixture.row_ids[0]]["run_id"] == fixture.second_run_id
    assert project.pending_replay_count(fixture.sheet_id, fixture.output_column_id) == 1

    grid = _sheet_data_payload(
        project,
        fixture.sheet_id,
        project.columns(fixture.sheet_id),
        fixture.row_ids,
        total=len(fixture.row_ids),
    )
    output_key = str(fixture.output_column_id)
    grid_rows = {int(row["id"]): row for row in grid["rows"]}
    assert (
        grid_rows[fixture.row_ids[0]]["meta"][output_key]["pending_value"]["run_id"]
        == fixture.second_run_id
    )
    output_column = next(
        column
        for column in grid["columns"]
        if int(column["id"]) == fixture.output_column_id
    )
    assert output_column["replay_pending_count"] == 1

    result = executor_actions.run_action_spec(
        project,
        {
            "action_id": "replay.accept",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": fixture.sheet_id,
                "column_id": fixture.output_column_id,
                "row_id": fixture.row_ids[0],
                "run_id": fixture.second_run_id,
                "generated_value_hash": pending[fixture.row_ids[0]][
                    "generated_value_hash"
                ],
            },
            "idempotency_key": f"single-origin-{scalar_mirror}@1",
        },
        project_id="single-origin-replay",
    )
    assert result.status == "completed", result.errors
    assert result.op_ids
    accepted_op = project.db.execute(
        "SELECT spec FROM ops WHERE id=?", (result.op_ids[-1],)
    ).fetchone()
    assert accepted_op is not None
    assert json.loads(accepted_op["spec"])["source_run_id"] == fixture.second_run_id
    assert project.pending_replay_count(fixture.sheet_id, fixture.output_column_id) == 0

    if scalar_mirror == "null":
        project.apply_edits(
            [
                {
                    "row_id": fixture.row_ids[0],
                    "column_id": fixture.output_column_id,
                    "value": "manual-after-zero-origin",
                }
            ]
        )
        project.db.execute("BEGIN IMMEDIATE")
        step_operation(project, "revert", change_id=fixture.second_op_id)
        project.db.commit()
        assert (
            project.pending_replay_values(fixture.sheet_id, fixture.output_column_id)
            == {}
        )


@pytest.mark.parametrize(
    ("kind", "target_kind"),
    [
        ("replay.accept", "replay_pending_cell"),
        ("replay.accept_column", "column"),
        ("replay.dismiss", "replay_pending_cell"),
    ],
)
def test_replay_actions_refuse_mixed_origin_columns_before_mutation(
    mixed_reader: _MixedReaderFixture,
    kind: str,
    target_kind: str,
) -> None:
    fixture = mixed_reader
    params: dict[str, Any] = {
        "sheet_id": fixture.sheet_id,
        "column_id": fixture.output_column_id,
    }
    if target_kind == "replay_pending_cell":
        params["row_id"] = fixture.row_ids[0]
    if kind in {"replay.accept", "replay.dismiss"}:
        params.update(
            {
                "generated_value_hash": f"sha256:{'a' * 64}",
                "run_id": fixture.second_run_id,
            }
        )
    op_count = int(fixture.project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0])
    result = executor_actions.run_action_spec(
        fixture.project,
        {
            "action_id": kind,
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": f"mixed-origin-{kind}@1",
        },
        project_id="mixed-origin-reader",
    )
    assert result.status == "failed"
    assert [error.code for error in result.errors] == [
        "mixed_origin_column_unsupported"
    ]
    assert fixture.project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == (
        op_count
    )
    assert (
        fixture.project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE column_id=?",
            (fixture.output_column_id,),
        ).fetchone()[0]
        == 0
    )


def test_entity_mentions_coverage_typed_gates_mixed_origins(
    mixed_reader: _MixedReaderFixture,
) -> None:
    fixture = mixed_reader
    with write_transaction(fixture.project.db):
        set_descriptor(fixture.project, fixture.output_column_id, "type", "json")
    with write_transaction(fixture.project.db):
        set_descriptor(
            fixture.project,
            fixture.output_column_id,
            "semantic_type",
            "entity_mentions",
        )
    with pytest.raises(EntityMentionsPreviewError) as excinfo:
        resolve_entity_mentions_preview(
            fixture.project,
            sheet_id=fixture.sheet_id,
            column_id=fixture.output_column_id,
        )
    assert excinfo.value.code == "mixed_origin_column_unsupported"

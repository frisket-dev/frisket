from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from contextlib import closing

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.column_transform_action import (
    build_column_transform_preview_plan,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from helpers import replace_test_source_cell
from frisket.engine.store.output_claims import OutputColumnClaimStore

PROJECT_ID = "typed-resolve-actions"


@pytest.fixture
def text_project(tmp_path: Path):
    project = Project.create(tmp_path / "resolve.frisket", name="resolve")
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "source", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"source": value} for value in ["ACME", "acme inc", "misc", " ", None]],
        {"source": column_id},
    )
    yield project, sheet_id, column_id, row_ids
    project.close()


def _request(
    action_id: str,
    sheet_id: int,
    params: dict[str, Any],
    *,
    key: str,
    output_name: str = "cleaned",
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "source", **params},
        "output_names": {"cleaned": output_name},
        "idempotency_key": key,
    }


def _output_values(
    project: Project, sheet_id: int, row_ids: list[int], name: str
) -> tuple[str, list[Any]]:
    column = project.db.execute(
        "SELECT id, type FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    assert column is not None
    values = project.get_values(sheet_id, int(column["id"]))
    return str(column["type"]), [values.get(row_id) for row_id in row_ids]


def _operation(kind: str, *, expected_op_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "columns",
            "ops",
            "receipts",
            "runs",
            "results",
            "run_output_generations",
            "consumed_source_bundles",
        )
    }


def test_empty_column_transform_creates_one_restorable_column(tmp_path):
    with closing(Project.create(tmp_path / "empty-transform.frisket")) as project:
        sheet = project.add_sheet("Empty")
        project.add_column(sheet, "source")
        result = run_action_spec(
            project,
            _request("resolve.substitute", sheet, {"mapping": {"a": "b"}}, key="empty"),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        column = result.outputs[0].column_id
        assert project.get_column(column)["active"] == 1
        assert project.undo() == result.op_ids[0]
        assert project.get_column(column)["active"] == 0
        assert project.redo() == result.op_ids[0]
        assert project.get_column(column)["active"] == 1
        assert project.get_values(sheet, column) == {}


@pytest.mark.parametrize(
    ("action_id", "params", "expected"),
    [
        (
            "resolve.substitute",
            {"mapping": {"ACME": "Acme", "misc": None}},
            ["Acme", "acme inc", None, None, None],
        ),
        (
            "resolve.replace",
            {"rules": [{"match": "contains", "pattern": "acme", "target": "Acme"}]},
            ["Acme", "Acme", "misc", None, None],
        ),
        (
            "resolve.combine",
            {"groups": [{"canonical": "Acme", "members": ["ACME", "acme inc"]}]},
            ["Acme", "Acme", "misc", None, None],
        ),
    ],
)
def test_text_resolve_transforms_publish_one_atomic_column(
    text_project, action_id: str, params: dict[str, Any], expected: list[Any]
) -> None:
    project, sheet_id, source_id, row_ids = text_project
    output_name = action_id.rsplit(".", 1)[-1]
    result = run_action_spec(
        project,
        _request(action_id, sheet_id, params, key=action_id, output_name=output_name),
        project_id=PROJECT_ID,
    )

    assert result.status == "completed", result.errors
    assert _output_values(project, sheet_id, row_ids, output_name) == ("text", expected)
    assert [
        project.get_values(sheet_id, source_id).get(row_id) for row_id in row_ids
    ] == ["ACME", "acme inc", "misc", " ", None]
    assert len(result.op_ids) == 1
    assert result.receipt_id is not None


@pytest.mark.parametrize(
    ("source_type", "values", "params", "expected_type", "expected"),
    [
        ("number", [1, None], {"method": "value", "fill_value": "0"}, "number", [1, 0]),
        ("integer", [1, None, 3], {"method": "mean"}, "integer", [1, 2, 3]),
        ("integer", [1, None, 2], {"method": "mean"}, "number", [1, 1.5, 2]),
        (
            "number",
            [1, 3, "n/a", None],
            {"method": "mean"},
            "number",
            [1, 3, None, 2],
        ),
        (
            "number",
            ["n/a", None],
            {"method": "down"},
            "number",
            [None, None],
        ),
        (
            "number",
            ["n/a", None],
            {"method": "mode"},
            "number",
            [None, None],
        ),
        ("text", [None, "a", None], {"method": "down"}, "text", [None, "a", "a"]),
    ],
)
def test_fill_missing_preserves_zero_and_widens_only_when_needed(
    tmp_path: Path,
    source_type: str,
    values: list[Any],
    params: dict[str, Any],
    expected_type: str,
    expected: list[Any],
) -> None:
    project = Project.create(tmp_path / f"{source_type}.frisket")
    try:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "source", type=source_type)
        row_ids = project.add_rows(
            sheet_id,
            [{"source": value} for value in values],
            {"source": column_id},
        )
        result = run_action_spec(
            project,
            _request("resolve.fill_missing", sheet_id, params, key="fill"),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        assert _output_values(project, sheet_id, row_ids, "cleaned") == (
            expected_type,
            expected,
        )
        op = project.db.execute(
            "SELECT label FROM ops WHERE id=?", (result.op_ids[0],)
        ).fetchone()
        assert op is not None and op["label"] == "fill source → cleaned"
    finally:
        project.close()


def test_fill_missing_does_not_repair_an_invalid_typed_source_value(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "exceptional-integer.frisket")
    try:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "source", type="integer")
        [row_id] = project.add_rows(sheet_id, [{"source": 1.5}], {"source": column_id})

        result = run_action_spec(
            project,
            _request(
                "resolve.fill_missing",
                sheet_id,
                {"method": "value", "fill_value": "4"},
                key="fill",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        assert _output_values(project, sheet_id, [row_id], "cleaned") == (
            "integer",
            [None],
        )
    finally:
        project.close()


def test_output_collision_and_active_claim_are_atomic(text_project) -> None:
    project, sheet_id, _column_id, _row_ids = text_project
    terminal = ACTION_REGISTRY.get("resolve.substitute").definition.run
    original = terminal.handler
    called = False

    def recording_handler(params, rows):
        nonlocal called
        called = True
        return original(params, rows)

    object.__setattr__(terminal, "handler", recording_handler)
    project.add_column(sheet_id, "existing", type="text")
    collision_before = _counts(project)
    try:
        collision = run_action_spec(
            project,
            _request(
                "resolve.substitute",
                sheet_id,
                {"mapping": {"ACME": "Acme"}},
                key="collision",
                output_name="existing",
            ),
            project_id=PROJECT_ID,
        )
        assert collision.status == "failed"
        assert collision.errors[0].code == "output_column_exists"
        assert called is False
        assert _counts(project) == collision_before

        _claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["claimed"],
            action_kind="test.blocker",
        )
        assert conflict is None
        claim_before = _counts(project)
        claimed = run_action_spec(
            project,
            _request(
                "resolve.substitute",
                sheet_id,
                {"mapping": {"ACME": "Acme"}},
                key="claimed",
                output_name="claimed",
            ),
            project_id=PROJECT_ID,
        )
        assert claimed.status == "failed"
        assert claimed.errors[0].code == "output_column_busy"
        assert called is False
        assert _counts(project) == claim_before
    finally:
        object.__setattr__(terminal, "handler", original)


def test_handler_and_receipt_failure_roll_back_every_write(
    text_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, _column_id, _row_ids = text_project
    terminal = ACTION_REGISTRY.get("resolve.substitute").definition.run
    original = terminal.handler

    def exploding_handler(_params, _rows):
        raise RuntimeError("boom")

    before = _counts(project)
    object.__setattr__(terminal, "handler", exploding_handler)
    try:
        handler_result = run_action_spec(
            project,
            _request(
                "resolve.substitute",
                sheet_id,
                {"mapping": {"ACME": "Acme"}},
                key="handler-failure",
            ),
            project_id=PROJECT_ID,
        )
    finally:
        object.__setattr__(terminal, "handler", original)
    assert handler_result.status == "failed"
    assert handler_result.errors[0].code == "project_write_failed"
    assert _counts(project) == before

    from frisket.engine.executor import column_transform_action

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("receipt failed")

    monkeypatch.setattr(
        column_transform_action.ReceiptStore, "update_body_status", fail_insert
    )
    receipt_result = run_action_spec(
        project,
        _request(
            "resolve.substitute",
            sheet_id,
            {"mapping": {"ACME": "Acme"}},
            key="receipt-failure",
        ),
        project_id=PROJECT_ID,
    )
    assert receipt_result.status == "failed"
    assert receipt_result.errors[0].code == "project_write_failed"
    assert _counts(project) == before


def test_idempotency_conflict_and_undo_redo_replay(text_project) -> None:
    project, sheet_id, _column_id, _row_ids = text_project
    request = _request(
        "resolve.substitute",
        sheet_id,
        {"mapping": {"ACME": "Acme"}},
        key="undo-replay",
    )
    first = run_action_spec(project, request, project_id=PROJECT_ID)
    assert first.status == "completed", first.errors

    conflict_request = {**request, "output_names": {"cleaned": "different"}}
    conflict = run_action_spec(project, conflict_request, project_id=PROJECT_ID)
    assert conflict.status == "failed"
    assert conflict.errors[0].code == "idempotency_conflict"

    op_id = first.op_ids[0]
    undo = run_action_spec(
        project,
        _operation("operation.undo", expected_op_id=op_id, key="undo"),
        project_id=PROJECT_ID,
    )
    assert undo.status == "completed", undo.errors
    stale = run_action_spec(project, request, project_id=PROJECT_ID)
    assert stale.status == "failed"
    assert stale.errors[0].code == "stale_replay"

    redo = run_action_spec(
        project,
        _operation("operation.redo", expected_op_id=op_id, key="redo"),
        project_id=PROJECT_ID,
    )
    assert redo.status == "completed", redo.errors
    assert run_action_spec(project, request, project_id=PROJECT_ID) == first


def test_undone_output_name_gets_fresh_identity_for_new_transform(text_project) -> None:
    project, sheet_id, _column_id, _row_ids = text_project
    first_request = _request(
        "resolve.substitute",
        sheet_id,
        {"mapping": {"ACME": "first"}},
        key="hidden-first",
    )
    first = run_action_spec(project, first_request, project_id=PROJECT_ID)
    assert first.status == "completed", first.errors
    output_column_id = first.outputs[0].column_id
    undo = run_action_spec(
        project,
        _operation("operation.undo", expected_op_id=first.op_ids[0], key="hidden-undo"),
        project_id=PROJECT_ID,
    )
    assert undo.status == "completed", undo.errors

    second = run_action_spec(
        project,
        _request(
            "resolve.substitute",
            sheet_id,
            {"mapping": {"ACME": "second"}},
            key="hidden-second",
        ),
        project_id=PROJECT_ID,
    )
    assert second.status == "completed", second.errors
    assert second.outputs[0].column_id != output_column_id
    assert project.get_column(output_column_id)["active"] == 0
    assert project.get_column(second.outputs[0].column_id)["active"] == 1
    assert _output_values(project, sheet_id, _row_ids, "cleaned")[1][0] == "second"
    statuses = {
        int(row["id"]): str(row["status"])
        for row in project.db.execute("SELECT id, status FROM ops")
    }
    assert statuses[first.op_ids[0]] == "undone"
    assert (
        project.db.execute(
            "SELECT redo_eligible FROM ops WHERE id=?", (first.op_ids[0],)
        ).fetchone()[0]
        == 0
    )
    assert statuses[second.op_ids[0]] == "applied"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='cleaned' AND hidden=0",
            (sheet_id,),
        ).fetchone()[0]
        == 1
    )


def test_same_idempotency_key_race_publishes_once(
    text_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, _column_id, _row_ids = text_project
    seeded_op_count = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    request = _request(
        "resolve.substitute",
        sheet_id,
        {"mapping": {"ACME": "Acme"}},
        key="same-key-race",
    )
    from frisket.engine.executor import column_transform_action

    original_lookup = column_transform_action._receipt_for_idempotency
    first_lookups = threading.Barrier(2)
    lookup_lock = threading.Lock()
    lookup_count = 0

    def synchronized_lookup(project_arg, key):
        nonlocal lookup_count
        found = original_lookup(project_arg, key)
        with lookup_lock:
            lookup_count += 1
            number = lookup_count
        if number <= 2:
            assert found is None
            first_lookups.wait(timeout=5)
        return found

    monkeypatch.setattr(
        column_transform_action, "_receipt_for_idempotency", synchronized_lookup
    )
    results: list[Any] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            results.append(run_action_spec(project, request, project_id=PROJECT_ID))
        except BaseException as error:
            errors.append(error)

    # realtime: thread barrier intentionally exercises concurrent receipt claims
    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0].status == "completed", results[0].errors
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        == seeded_op_count + 1
    )


def test_preview_computes_full_scope_then_bounds_display_and_never_writes(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "preview.frisket")
    try:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "source", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"source": None} for _ in range(20)] + [{"source": "tail"}],
            {"source": column_id},
        )
        before = _counts(project)
        request = _request(
            "resolve.fill_missing", sheet_id, {"method": "up"}, key="preview"
        )
        plan = build_column_transform_preview_plan(
            project, typed_action_for_request(request)
        )
        assert not hasattr(plan, "code")
        preview = plan.run()

        assert preview.row_ids == row_ids[:20]
        assert preview.sampled == 20
        assert preview.total == 21
        assert all(
            preview.values[row_id]["cleaned"]["value"] == "tail"
            for row_id in row_ids[:20]
        )
        assert _counts(project) == before
    finally:
        project.close()


def test_source_edit_waits_for_atomic_transform_snapshot(text_project) -> None:
    project, sheet_id, column_id, row_ids = text_project
    terminal = ACTION_REGISTRY.get("resolve.substitute").definition.run
    original = terminal.handler
    entered = threading.Event()
    writer_attempted = threading.Event()
    errors: list[BaseException] = []
    results: list[Any] = []

    def waiting_handler(params, rows):
        entered.set()
        assert writer_attempted.wait(timeout=5)
        return original(params, rows)

    def transform() -> None:
        try:
            results.append(
                run_action_spec(
                    project,
                    _request(
                        "resolve.substitute",
                        sheet_id,
                        {"mapping": {"ACME": "before-edit"}},
                        key="atomic",
                    ),
                    project_id=PROJECT_ID,
                )
            )
        except BaseException as error:
            errors.append(error)

    def edit() -> None:
        peer = sqlite3.connect(project.db_path, timeout=10)
        try:
            peer.set_trace_callback(
                lambda statement: (
                    writer_attempted.set() if statement == "BEGIN IMMEDIATE" else None
                )
            )
            peer.execute("BEGIN IMMEDIATE")
            replace_test_source_cell(
                project,
                db=peer,
                row_id=row_ids[0],
                column_id=column_id,
                value="after-edit",
            )
            peer.commit()
        except BaseException as error:
            errors.append(error)
        finally:
            peer.close()

    object.__setattr__(terminal, "handler", waiting_handler)
    try:
        # realtime: real SQLite threads exercise the atomic snapshot boundary
        transform_thread = threading.Thread(target=transform)
        transform_thread.start()
        assert entered.wait(timeout=5)
        # realtime: real SQLite threads exercise the atomic snapshot boundary
        edit_thread = threading.Thread(target=edit)
        edit_thread.start()
        transform_thread.join(timeout=10)
        edit_thread.join(timeout=10)
    finally:
        object.__setattr__(terminal, "handler", original)

    assert not transform_thread.is_alive()
    assert not edit_thread.is_alive()
    assert errors == []
    assert results[0].status == "completed", results[0].errors
    assert _output_values(project, sheet_id, row_ids, "cleaned")[1][0] == "before-edit"
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == "after-edit"


@pytest.mark.parametrize("hidden", [False, True])
def test_fill_publishes_real_saved_results_and_preserves_unavailable_source(
    tmp_path, hidden
):
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "fill-outcomes.frisket")
    try:
        sheet = project.add_sheet("data")
        source = project.add_column(sheet, "source", type="number")
        rows = project.add_rows(
            sheet,
            [{"source": 2}, {"source": "n/a"}, {"source": None}, {"source": 4}],
            {"source": source},
        )
        if hidden:
            project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (source,))
            project.db.commit()
        request = _request(
            "resolve.fill_missing", sheet, {"method": "mean"}, key="fill-outcomes"
        )
        result = run_action_spec(project, request, project_id=PROJECT_ID)
        assert result.status == "completed", result.errors
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None and receipt.run_id is not None
        output = result.outputs[0].column_id
        saved = project.db.execute(
            "SELECT row_id,outcome,error_code FROM results WHERE run_id=? ORDER BY row_id",
            (receipt.run_id,),
        ).fetchall()
        assert len(saved) == 4
        assert saved[1]["outcome"] == "not_completed"
        assert saved[1]["error_code"] == "input_invalid"
        values, refs = project.get_values_with_refs(
            sheet, output, include_validity=True
        )
        assert [values.get(row) for row in rows] == [2, None, 3, 4]
        assert refs[rows[1]]["outcome"] == "not_completed"
        assert all(ref["kind"] == "run_result" for ref in refs.values())
        assert (
            project.get_values(sheet, source, preserve_invalid=True)[rows[1]] == "n/a"
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM cells WHERE column_id=?", (output,)
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM consumed_source_bundles WHERE owner_op_id=? AND run_id=?",
                (result.op_ids[0], receipt.run_id),
            ).fetchone()[0]
            == 1
        )
        assert run_action_spec(project, request, project_id=PROJECT_ID) == result

        # A second transform cannot turn the first transform's unavailable output
        # into an ordinary null eligible for filling.
        follow = _request(
            "resolve.fill_missing",
            sheet,
            {"source": "cleaned", "method": "value", "fill_value": "99"},
            key="follow",
            output_name="again",
        )
        again = run_action_spec(project, follow, project_id=PROJECT_ID)
        assert again.status == "completed", again.errors
        values, refs = project.get_values_with_refs(
            sheet, again.outputs[0].column_id, include_validity=True
        )
        assert [values.get(row) for row in rows] == [2, None, 3, 4]
        assert refs[rows[1]]["outcome"] == "not_completed"
        from frisket.engine.store.consumed_sources import verify_bundle

        bundle = project.db.execute(
            "SELECT id FROM consumed_source_bundles WHERE run_id=?", (receipt.run_id,)
        ).fetchone()[0]
        assert verify_bundle(project, bundle) == "fresh"
        replace_test_source_cell(project, row_id=rows[0], column_id=source, value=20)
        assert verify_bundle(project, bundle) == "stale"
        assert project.get_values(sheet, output)[rows[0]] == 2
    finally:
        project.close()


def test_active_hidden_output_is_not_replaced(text_project):
    project, sheet, _, _ = text_project
    column = project.add_column(sheet, "cleaned", type="text")
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column,))
    project.db.commit()
    before = _counts(project)
    result = run_action_spec(
        project,
        _request(
            "resolve.substitute",
            sheet,
            {"mapping": {"ACME": "Acme"}},
            key="hidden-collision",
        ),
        project_id=PROJECT_ID,
    )
    assert result.errors[0].code == "output_column_exists"
    assert _counts(project) == before


def test_fill_preview_exposes_unavailable_and_preserves_blank_semantics(tmp_path):
    project = Project.create(tmp_path / "preview-invalid.frisket")
    try:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "source", type="number")
        rows = project.add_rows(
            sheet, [{"source": "n/a"}, {"source": None}], {"source": column}
        )
        request = _request(
            "resolve.fill_missing",
            sheet,
            {"method": "value", "fill_value": "7"},
            key="preview",
        )
        bound = typed_action_for_request(request)
        plan = build_column_transform_preview_plan(project, bound)
        before = _counts(project)
        preview = plan.run()
        assert preview.values[rows[0]]["cleaned"]["error_code"] == "input_invalid"
        assert preview.values[rows[1]]["cleaned"]["value"] == 7
        assert _counts(project) == before
    finally:
        project.close()


def test_fill_does_not_wait_for_or_fill_failed_and_pending_sources(tmp_path):
    from tests.engine.test_result_generation_store import (
        _start_claimed_run,
        _declare,
        _write,
    )
    from frisket.engine.store.result_generations import ResultGenerationStore

    project = Project.create(tmp_path / "pending-fill.frisket")
    try:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "source", type="text", ai_generated=True)
        rows = project.add_rows(sheet, [{}, {}, {}], {})
        producer = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=column,
            row_ids=rows,
            label="source",
        )
        _declare(ResultGenerationStore(project), producer, column, write_mode="create")
        _write(
            project,
            producer,
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": None,
                    "error": "provider failed",
                    "error_code": "provider_error",
                    "publication_effect": "publish_error",
                },
                {
                    "row_id": rows[1],
                    "column_id": column,
                    "value": None,
                    "publication_effect": "publish_null",
                },
            ],
        )
        result = run_action_spec(
            project,
            _request(
                "resolve.fill_missing",
                sheet,
                {"method": "value", "fill_value": "filled"},
                key="pending-fill",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        values = project.get_values(sheet, result.outputs[0].column_id)
        assert [values.get(row) for row in rows] == [None, "filled", None]
        errors = project.db.execute(
            "SELECT row_id,error_code FROM results WHERE column_id=? ORDER BY row_id",
            (result.outputs[0].column_id,),
        ).fetchall()
        assert [item["error_code"] for item in errors] == [
            "input_failed",
            None,
            "input_pending",
        ]
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (producer.run_id,)
            ).fetchone()[0]
            == "running"
        )
    finally:
        project.close()


def test_column_transform_large_scope_respects_sqlite_bind_limit(tmp_path):
    with closing(Project.create(tmp_path / "large-transform.frisket")) as project:
        sheet = project.add_sheet("Rows")
        source = project.add_column(sheet, "source")
        rows = project.add_rows(sheet, [{"source": "old"}] * 33_000, {"source": source})
        project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32766)
        per_row_family_reads = 0

        def track(sql):
            nonlocal per_row_family_reads
            if sql.startswith("SELECT column_id FROM results WHERE run_id="):
                per_row_family_reads += 1

        project.db.set_trace_callback(track)
        try:
            result = run_action_spec(
                project,
                _request(
                    "resolve.substitute",
                    sheet,
                    {"mapping": {"old": "new"}},
                    key="large",
                ),
                project_id=PROJECT_ID,
            )
        finally:
            project.db.set_trace_callback(None)
        assert per_row_family_reads == 0
        assert result.status == "completed", result.errors
        output = result.outputs[0].column_id
        assert project.get_values(sheet, output, row_ids=[rows[0], rows[-1]]) == {
            rows[0]: "new",
            rows[-1]: "new",
        }
        assert project.db.execute(
            "SELECT completed_rows,failed_rows FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[:] == (33_000, 0)


def test_whole_column_transform_refuses_row_retry_and_backfill(tmp_path):
    from frisket.engine.runner.review import review_runs_page
    from frisket.server.run_status import public_run_progress

    with closing(Project.create(tmp_path / "transform-retry.frisket")) as project:
        sheet = project.add_sheet("Rows")
        source = project.add_column(sheet, "source", type="number")
        rows = project.add_rows(
            sheet, [{"source": 2}, {"source": "n/a"}], {"source": source}
        )
        result = run_action_spec(
            project,
            _request(
                "resolve.fill_missing",
                sheet,
                {"method": "mean"},
                key="fill",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        run = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run["completed_rows"] == 1
        assert run["failed_rows"] == 0
        public = public_run_progress(run, "completed")
        assert (
            public["attempted"],
            public["succeeded"],
            public["failed"],
            public["skipped"],
        ) == (1, 1, 0, 1)
        assert (
            review_runs_page(project, run_id=run["id"])["runs"][0]["retry_supported"]
            is False
        )
        before = _counts(project)
        for action, params in (
            ("run.retry", {"source_run_id": run["id"]}),
            ("run.backfill", {"column": "cleaned"}),
        ):
            refused = run_action_spec(
                project,
                {
                    "action_id": action,
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet,
                        "row_ids": [rows[1]],
                    },
                    "params": params,
                    "idempotency_key": action,
                },
                project_id=PROJECT_ID,
            )
            assert refused.status == "failed"
            assert refused.errors[0].code == "run_required"
            assert "whole column" in refused.errors[0].message
        assert _counts(project) == before

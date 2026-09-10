from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    UndoRerun,
    case_env,
)
from frisket.engine.store import Project
from frisket.engine.store.output_claims import (
    ClaimLeaseRenewalFailed,
    OutputColumnClaimStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import (
    StaleAttemptWriter,
    abandon_stale_dispatching_attempts,
)

_OUTPUT_COLUMNS = ("__result_entities", "python_result", "word_count")

# Reset by _patch_sandbox_spy at the start of every harness test for this
# case; asserts on it are only meaningful behind that patch.
_SANDBOX_CALLS: list[str] = []


def _patch_sandbox_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the executor's sandbox transport through a counting delegate.

    map.python's per-row work goes through frisket.sandbox.shim.run_python_op;
    the spy proves the runner really crosses that seam (once per row, never on
    replay) while delegating to the real sandbox so behavior coverage is
    unchanged.
    """
    from frisket.engine.sandbox import shim

    _SANDBOX_CALLS.clear()
    real_run_python_op = shim.run_python_op

    async def spy(*args: Any, **kwargs: Any) -> Any:
        _SANDBOX_CALLS.append("run_python_op")
        return await real_run_python_op(*args, **kwargs)

    monkeypatch.setattr(shim, "run_python_op", spy)


def _map_python_action(sheet_id: int, *, idempotency_key: str) -> dict[str, Any]:
    code = "\n".join(
        [
            "words = row['transcript'].split()",
            "person = words[0]",
            "result = {",
            "    'excerpt': ' '.join(words[:4]),",
            "    'word_count': len(words),",
            "    'entities': [{'name': person, 'title': 'speaker'}],",
            "    'debug': {'first_word': person, 'input_title': row['title']},",
            "}",
        ]
    )
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"raw_result": "python_result"},
        "params": {
            "input_columns": ["title", "transcript"],
            "code": code,
            "return_schema": {
                "type": "object",
                "required": ["excerpt", "word_count", "entities", "debug"],
                "properties": {
                    "excerpt": {"type": "string"},
                    "word_count": {"type": "integer"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "title"],
                            "properties": {
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                            },
                        },
                    },
                    "debug": {"type": "object"},
                },
            },
            "output_routes": [
                {
                    "name": "raw_result",
                    "path": "$",
                    "target": {
                        "kind": "column",
                        "type": "json",
                    },
                },
                {
                    "name": "word_count",
                    "path": "$.word_count",
                    "target": {
                        "kind": "column",
                        "type": "integer",
                    },
                },
                {
                    "name": "entities",
                    "path": "$.entities",
                    "target": {
                        "kind": "named_result",
                        "schema": "entity_list",
                        "may_feed": ["derive.table_from_list"],
                    },
                },
                {
                    "name": "debug",
                    "path": "$.debug",
                    "target": {
                        "kind": "receipt_evidence",
                        "retention": "compactable",
                    },
                },
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Transcripts")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "transcript": project.add_column(sheet_id, "transcript", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "transcript": "Alice founded Newsroom Labs in Brooklyn",
            },
            {
                "title": "Episode 2",
                "transcript": "Bob joined Civic Data in Queens",
            },
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:stable"
    )


def _caller_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:missing-capability"
    )
    action["capabilities"] = ["project:write"]
    return action


def _visible_collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:visible-collision"
    )


def _rename_routes(action: dict[str, Any], suffix: str) -> dict[str, Any]:
    names = {}
    for route in action["params"]["output_routes"]:
        name = action["output_names"].get(route["name"], route["name"])
        route["name"] = f"{route['name']}_{suffix}"
        target = route["target"]
        if target["kind"] == "column":
            names[route["name"]] = f"{name}_{suffix}"
    action["output_names"] = names
    return action


def _missing_route_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:missing-route"
    )
    action["params"]["output_routes"][0]["path"] = "$.missing"
    return _rename_routes(action, "missing")


def _invalid_schema_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:invalid-schema"
    )
    action["params"]["code"] = "result = {'word_count': 'six'}"
    return _rename_routes(action, "bad")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    assert result.run_id is not None
    assert {output.kind for output in result.outputs} == {"column", "named_result"}
    assert {output.name for output in result.outputs} == {
        "python_result",
        "word_count",
        "entities",
    }
    # The patch seam routed every per-row execution through the sandbox spy.
    assert len(_SANDBOX_CALLS) == 2

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    # visible column targets
    assert columns["python_result"]["type"] == "json"
    assert columns["python_result"]["ai_generated"] == 1
    assert columns["python_result"]["current_run_id"] == result.run_id
    assert columns["python_result"]["hidden"] == 0
    assert columns["word_count"]["type"] == "integer"
    assert columns["word_count"]["current_run_id"] == result.run_id

    # named_result target -> hidden plumbing column
    named_result_column = columns.get("__result_entities")
    assert named_result_column is not None
    assert named_result_column["hidden"] == 1
    assert named_result_column["type"] == "json"
    assert named_result_column["current_run_id"] == result.run_id

    # receipt_evidence target -> hidden plumbing column (NOT a user-facing output)
    evidence_column = columns.get("__evidence_debug")
    assert evidence_column is not None
    assert evidence_column["hidden"] == 1

    raw_values = project.get_values(sheet_id, int(columns["python_result"]["id"]))
    assert list(raw_values.values())[0]["excerpt"] == "Alice founded Newsroom Labs"
    word_counts = project.get_values(sheet_id, int(columns["word_count"]["id"]))
    assert list(word_counts.values()) == [6, 6]
    entity_values = project.get_values(sheet_id, int(named_result_column["id"]))
    assert list(entity_values.values())[0] == [{"name": "Alice", "title": "speaker"}]
    evidence_values = project.get_values(sheet_id, int(evidence_column["id"]))
    assert list(evidence_values.values())[0]["first_word"] == "Alice"

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "map.python"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0

    op = project.db.execute(
        "SELECT * FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op is not None
    assert op["kind"] == "map"

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] == result.run_id
    assert receipt_row["action_kind"] == "map.python"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.run_id == result.run_id
    assert receipt.action_kind == "map.python"
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "map_python_code",
        "typed_action_request",
        "map_python_receipt_evidence",
    }
    debug_refs = [
        item.ref
        for item in receipt.evidence
        if item.ref["kind"] == "map_python_receipt_evidence"
    ]
    # lightweight: a pointer at the hidden column, no per-row value blobs.
    assert debug_refs[0]["route"] == "debug"
    assert debug_refs[0]["column_id"] == int(evidence_column["id"])
    assert "values" not in debug_refs[0]
    # column targets advertise the map_result_column ref kind.
    column_refs = [
        output.ref
        for output in receipt.outputs
        if output.ref["kind"] == "map_result_column"
    ]
    assert {ref["name"] for ref in column_refs} == {"python_result", "word_count"}
    named_refs = [
        output.ref for output in receipt.outputs if output.ref["kind"] == "named_result"
    ]
    assert named_refs[0]["schema"] == "entity_list"
    assert named_refs[0]["may_feed"] == ["derive.table_from_list"]


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _make_action(seeded)
    action["params"]["code"] = "result = None"
    return action


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='python_result'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


def _output_column_rows(project: Project, sheet_id: int) -> list[Any]:
    placeholders = ", ".join("?" for _ in _OUTPUT_COLUMNS)
    return project.db.execute(
        "SELECT id, name, hidden, current_run_id FROM columns "
        f"WHERE sheet_id=? AND name IN ({placeholders}) ORDER BY id",
        (sheet_id, *_OUTPUT_COLUMNS),
    ).fetchall()


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    rows = _output_column_rows(project, seeded["sheet_id"])
    assert {row["name"] for row in rows} == set(_OUTPUT_COLUMNS)
    assert {int(row["hidden"]) for row in rows} == {1}
    assert {row["current_run_id"] for row in rows} == {None}
    seeded["output_ids"] = {row["name"]: int(row["id"]) for row in rows}


def _rerun_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_python_action(
        seeded["sheet_id"], idempotency_key="map_python@sha256:undo-rerun"
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first
    rows = _output_column_rows(project, seeded["sheet_id"])
    revived = {row["name"]: row for row in rows}
    # the SAME column ids are revived (not duplicated)...
    assert len(rows) == 3
    assert {name: int(row["id"]) for name, row in revived.items()} == (
        seeded["output_ids"]
    )
    # ...visible column targets come back visible; the named_result plumbing
    # stays hidden across the undo/rerun cycle.
    assert revived["python_result"]["hidden"] == 0
    assert revived["word_count"]["hidden"] == 0
    assert revived["__result_entities"]["hidden"] == 1
    assert {row["current_run_id"] for row in revived.values()} == {None}


CASES = [
    ExecutorCase(
        kind="map.python",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "unsafe:local_code"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "execute_trusted_local_python",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "output_column_exists",
                    "map_rows_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_sandbox_spy,
        gates=(
            Gate(
                "caller_authored_capabilities",
                _caller_capability_action,
                "invalid_action_request",
            ),
            Gate(
                "output_column_exists",
                _visible_collision_action,
                "output_column_exists",
                after_primary_run=True,
            ),
            # A failed map run persists its failed run/results/op/receipt
            # trail, so the no-writes assert does not apply.
            Gate(
                "map_run_failed_missing_route",
                _missing_route_action,
                "map_rows_failed",
                no_writes=False,
            ),
            Gate(
                "map_run_failed_schema_mismatch",
                _invalid_schema_action,
                "map_rows_failed",
                no_writes=False,
            ),
        ),
        expect_counts={
            "columns": 4,
            "runs": 1,
            # one result row per source row per output route (2 rows x 4)
            "results": 8,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            make_conflict=_conflicting_params_action,
            go_stale=_go_stale,
        ),
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_rerun_action,
            check_rerun=_check_rerun,
        ),
    )
]


def test_map_python_replay_does_not_reenter_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying the stored receipt must reuse the persisted results — the
    sandbox transport is never crossed again."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert len(_SANDBOX_CALLS) == 2

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_SANDBOX_CALLS) == 2


def test_map_python_claim_lease_is_liveness_without_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production map.python batches renew the claim before an immediate sweep.

    The attempt is made older than the stale bound after every committed
    result batch. The pre-fix model-call inference abandons it on the first
    sweep because map.python emits no model-call facts; claim-lease liveness
    keeps every sweep at zero instead.
    """
    original_write_results = RunResultStore.write_results
    sweep_results: list[int] = []

    def write_then_sweep(
        store: RunResultStore,
        run_id: int,
        batch: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        original_write_results(store, run_id, batch, **kwargs)
        writer_attempt_id = kwargs.get("writer_attempt_id")
        assert isinstance(writer_attempt_id, str) and writer_attempt_id
        store.db.execute(
            "UPDATE execution_attempts "
            "SET created_at=datetime('now', '-7 hours') WHERE id=?",
            (writer_attempt_id,),
        )
        store.db.commit()
        sweep_results.append(
            abandon_stale_dispatching_attempts(
                store.project,
                run_id,
                max_age=timedelta(hours=6),
            )
        )

    monkeypatch.setattr(RunResultStore, "write_results", write_then_sweep)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run_primary()
        assert result.status == "completed", result.errors
        assert sweep_results
        assert set(sweep_results) == {0}
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE run_id=?",
                (result.run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            env.project.db.execute(
                "SELECT state FROM execution_attempts "
                "WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (result.run_id,),
            ).fetchone()[0]
            == "effected"
        )


def test_direct_maprunner_stale_writer_refusal_is_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram

    monkeypatch.setattr(_TypedMapRowsProgram, "max_concurrency", 1)

    def lose_effect_authority(*_args: Any, **_kwargs: Any) -> None:
        raise StaleAttemptWriter("attempt A resumed after replacement B")

    monkeypatch.setattr(
        RunResultStore,
        "write_results",
        lose_effect_authority,
    )
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run_primary()

        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["stale_attempt_writer"]
        assert len(_SANDBOX_CALLS) == 1
        run_id = int(env.project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
        run = env.project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "running"
        assert isinstance(run["current_attempt_id"], str)
        assert (
            env.project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (run["current_attempt_id"],),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchone()[0]
            == len(_OUTPUT_COLUMNS) + 1
        )
        receipt = env.project.db.execute(
            "SELECT status, body FROM receipts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert receipt is not None
        assert receipt["status"] == "running"
        assert json.loads(receipt["body"])["errors"] == []


def test_direct_maprunner_renewal_failure_leaves_run_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram

    monkeypatch.setattr(_TypedMapRowsProgram, "max_concurrency", 1)

    def fail_renewal(*_args: Any, **_kwargs: Any) -> int:
        raise sqlite3.OperationalError("injected claim renewal outage")

    monkeypatch.setattr(OutputColumnClaimStore, "renew", fail_renewal)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        with pytest.raises(
            ClaimLeaseRenewalFailed,
            match="claim renewal outage",
        ):
            env.run_primary()

        assert len(_SANDBOX_CALLS) == 1
        run_id = int(env.project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
        run = env.project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "running"
        assert isinstance(run["current_attempt_id"], str)
        assert (
            env.project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (run["current_attempt_id"],),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchone()[0]
            == len(_OUTPUT_COLUMNS) + 1
        )
        receipt = env.project.db.execute(
            "SELECT status, body FROM receipts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert receipt is not None
        assert receipt["status"] == "running"
        assert json.loads(receipt["body"])["errors"] == []

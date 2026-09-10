from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.extract import (
    MAX_REGEX_TIMEOUT_SECONDS,
    REGEX_EXTRACT,
    RegexExtractParams,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore


def _map_regex_extract_action(
    sheet_id: int,
    *,
    idempotency_key: str = "map_regex_extract@sha256:stable",
    output_name: str = "phone",
    pattern: str = r"\d{3}-\d{3}-\d{4}",
    all_matches: bool = False,
    group: int | str | None = None,
) -> dict[str, Any]:
    params = RegexExtractParams(
        input_columns=["note"],
        pattern=pattern,
        all_matches=all_matches,
        group=group,
    )
    logical_outputs = [
        field.key for field in REGEX_EXTRACT.run.resolve_output_fields(params)
    ]
    output_names = {
        logical: (
            output_name
            if logical == "extracted"
            else f"{output_name}{logical.removeprefix('extracted')}"
        )
        for logical in logical_outputs
    }
    request_params: dict[str, Any] = {
        "input_columns": ["note"],
        "pattern": pattern,
        "all_matches": all_matches,
    }
    if group is not None:
        request_params["group"] = group
    return {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": request_params,
        "output_names": output_names,
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Feature Tour")
    columns = {
        "name": project.add_column(sheet_id, "name", type="text"),
        "note": project.add_column(sheet_id, "note", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {"name": "Ada", "note": "Phone 212-555-0123, ids A12 B34"},
            {"name": "Grace", "note": "No phone here, ids B34 C56"},
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_regex_extract_action(seeded["sheet_id"])


def _missing_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_regex_extract_action(
        seeded["sheet_id"],
        idempotency_key="map_regex_extract@sha256:missing-input",
        output_name="missing",
    )
    action["params"]["input_columns"] = ["missing_column"]
    return action


def _colliding_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_regex_extract_action(
        seeded["sheet_id"],
        idempotency_key="map_regex_extract@sha256:collision",
        output_name="phone",
    )


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_regex_extract_action(seeded["sheet_id"], output_name="other_phone")


def _rename_output_column(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET name='renamed_phone' WHERE sheet_id=? AND name='phone'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    assert [output.name for output in result.outputs] == ["phone"]
    assert result.outputs[0].ref["type"] == "text"

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "map.regex_extract"
    assert run["model"] is None
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0
    assert run["cost_actual"] == 0.0

    op = project.db.execute("SELECT * FROM ops WHERE id=?", (run["op_id"],)).fetchone()
    assert op is not None
    assert op["kind"] == "map"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_kind"] == "map.regex_extract"
    assert op_spec["action_version"] == "1"
    assert "recipe" not in op_spec
    assert op_spec["input_columns"] == ["note"]
    assert op_spec["params"]["pattern"] == r"\d{3}-\d{3}-\d{4}"
    assert op_spec["output_names"] == {"extracted": "phone"}

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    assert columns["phone"]["type"] == "text"
    assert columns["phone"]["ai_generated"] == 1
    assert columns["phone"]["current_run_id"] == result.run_id
    assert list(project.get_values(sheet_id, int(columns["phone"]["id"])).values()) == [
        "212-555-0123",
        None,
    ]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "map.regex_extract"
    assert receipt.run_id == result.run_id
    assert receipt.op_ids == [run["op_id"]]
    assert receipt.provider_use == [
        {
            "provider": "local",
            "service": "frisket.map.regex_extract",
            "external_api": False,
            "cost_actual": 0.0,
        }
    ]
    input_refs = {item.name: item.ref for item in receipt.inputs}
    assert input_refs["column.note"]["kind"] == "source_column"
    assert input_refs["column.note"]["name"] == "note"
    output_refs = {item.name: item.ref for item in receipt.outputs}
    assert output_refs["phone"]["kind"] == "map_result_column"
    assert output_refs["phone"]["type"] == "text"
    evidence_refs = {item.ref["kind"]: item.ref for item in receipt.evidence}
    assert set(evidence_refs) == {"typed_action_request", "map_rows_run_counts"}
    assert evidence_refs["typed_action_request"]["params"] == {
        "input_columns": ["note"],
        "pattern": r"\d{3}-\d{3}-\d{4}",
        "all_matches": False,
    }


CASES = [
    ExecutorCase(
        kind="map.regex_extract",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "output_column_exists",
                    "output_column_busy",
                    "map_rows_failed",
                    "project_write_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="none",
            cost_requires_confirmation=False,
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_input_ref",
                _missing_input_action,
                "invalid_input_ref",
            ),
            Gate(
                "output_column_exists",
                _colliding_output_action,
                "output_column_exists",
                after_primary_run=True,
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_params_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            # regex_extract is completed-only (accepted_run_statuses default),
            # so its replay is expected-scope strict:
            # a replay whose output column was renamed away fails stale_replay,
            # same as any other expected-scope op.
            Gate(
                "stale_replay_renamed_output",
                _make_action,
                "stale_replay",
                after_primary_run=True,
                prepare=_rename_output_column,
            ),
        ),
        expect_counts={
            "columns": 1,
            "runs": 1,
            "results": 2,
            "model_calls": 0,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        request_style="typed",
    )
]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pattern": "([unclosed"}, "invalid regex pattern"),
        ({"pattern": r"([A-Z]\d\d)", "group": 2}, "invalid regex group"),
        ({"timeout_seconds": 0}, "greater than 0"),
        (
            {"timeout_seconds": MAX_REGEX_TIMEOUT_SECONDS + 0.1},
            f"less than or equal to {MAX_REGEX_TIMEOUT_SECONDS:g}",
        ),
    ],
)
def test_params_reject_invalid_regex_configuration(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        RegexExtractParams.model_validate(
            {
                "input_columns": ["note"],
                "pattern": r"\d{3}-\d{3}-\d{4}",
                **overrides,
            }
        )


def test_generated_form_digit_group_is_normalized_to_integer() -> None:
    params = RegexExtractParams(
        input_columns=["note"],
        pattern=r"([A-Z]\d\d)",
        group="1",
    )

    assert params.group == 1


def test_typed_request_rejects_legacy_capability_authority() -> None:
    action = _map_regex_extract_action(1)
    action["capabilities"] = ["project:write"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ActionRequest.model_validate(action)


def test_all_matches_writes_json_list_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        sheet_id = env.seeded["sheet_id"]
        result = env.run(
            _map_regex_extract_action(
                sheet_id,
                idempotency_key="map_regex_extract@sha256:all-matches",
                output_name="ids",
                pattern=r"[A-Z]\d\d",
                all_matches=True,
            )
        )
        assert result.status == "completed", result.errors

        column = env.project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? AND name='ids'",
            (sheet_id,),
        ).fetchone()
        assert column is not None
        assert column["type"] == "json"
        assert list(env.project.get_values(sheet_id, int(column["id"])).values()) == [
            ["A12", "B34"],
            ["B34", "C56"],
        ]

        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        output_refs = {item.name: item.ref for item in receipt.outputs}
        assert output_refs["ids"]["type"] == "json"
        assert output_refs["ids"]["kind"] == "map_result_column"


def test_hidden_column_without_generation_history_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        sheet_id = env.seeded["sheet_id"]
        hidden_column_id = env.project.add_column(sheet_id, "restored", type="text")
        env.project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (hidden_column_id,)
        )
        env.project.db.commit()

        before = tuple(env.project.get_column(hidden_column_id))
        result = env.run(
            _map_regex_extract_action(
                sheet_id,
                idempotency_key="map_regex_extract@sha256:hidden-reuse",
                output_name="restored",
            )
        )
        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["output_column_exists"]
        restored = env.project.db.execute(
            "SELECT * FROM columns WHERE id=?", (hidden_column_id,)
        ).fetchone()
        assert restored is not None
        assert tuple(restored) == before
        assert restored["hidden"] == 1
        assert not ResultGenerationStore(env.project).is_generation_managed(
            hidden_column_id
        )


def test_no_explicit_group_expands_capture_groups_via_typed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An omitted group expands multi-capture output through the typed runner."""
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        sheet_id = env.seeded["sheet_id"]
        action = _map_regex_extract_action(
            sheet_id,
            idempotency_key="map_regex_extract@sha256:multi-group",
            output_name="phone",
            pattern=r"(\d{3})-(\d{3}-\d{4})",
        )
        # No "group" key at all — this is the exact shape the frontend sends.
        assert "group" not in action["params"]

        request = ActionRequest.model_validate(action)
        assert request.scope == SheetRows(sheet_id=sheet_id)
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get(request.action_id), request
        )
        assert bound.params.group is None

        result = env.run(action)
        assert result.status == "completed", result.errors
        assert [output.name for output in result.outputs] == ["phone_1", "phone_2"]
        assert [output.ref["type"] for output in result.outputs] == ["text", "text"]

        columns = {
            row["name"]: row
            for row in env.project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        }
        assert set(columns) >= {"phone_1", "phone_2"}
        assert columns["phone_1"]["type"] == "text"
        assert columns["phone_2"]["type"] == "text"
        assert columns["phone_1"]["current_run_id"] == result.run_id
        assert columns["phone_2"]["current_run_id"] == result.run_id
        assert list(
            env.project.get_values(sheet_id, int(columns["phone_1"]["id"])).values()
        ) == ["212", None]
        assert list(
            env.project.get_values(sheet_id, int(columns["phone_2"]["id"])).values()
        ) == ["555-0123", None]

        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        output_refs = {item.name: item.ref for item in receipt.outputs}
        assert set(output_refs) == {"phone_1", "phone_2"}
        assert {ref["kind"] for ref in output_refs.values()} == {"map_result_column"}

        # Explicit group stays a single column even with 2+ capture groups.
        explicit_action = _map_regex_extract_action(
            sheet_id,
            idempotency_key="map_regex_extract@sha256:explicit-group",
            output_name="area_code",
            pattern=r"(\d{3})-(\d{3}-\d{4})",
            group=1,
        )
        explicit = env.run(explicit_action)
        assert explicit.status == "completed", explicit.errors
        assert [output.name for output in explicit.outputs] == ["area_code"]
        area_code = env.project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='area_code'",
            (sheet_id,),
        ).fetchone()
        assert area_code is not None
        assert list(
            env.project.get_values(sheet_id, int(area_code["id"])).values()
        ) == ["212", None]

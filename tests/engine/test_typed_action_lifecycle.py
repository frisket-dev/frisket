from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Outcome,
    Row,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.contracts.action import Receipt, ReceiptIO
from frisket.engine.executor.map_rows_action import (
    typed_request_hash,
    run_typed_map_rows_action as _run_typed_map_rows_action,
)
from frisket.engine.runner import MapRunner, NetworkDisabled, validation
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import Recipe


class UpperParams(ActionParams):
    source: ColumnRef[str]


class UpperOutput(BaseModel):
    upper: str


def upper(params: UpperParams, row: Row) -> RowResult[UpperOutput]:
    return RowResult(output=UpperOutput(upper=params.source.read(row).upper()))


class PartialOutput(BaseModel):
    stable: Outcome[str]
    fallible: Outcome[str]
    error: str


def partial(params: UpperParams, row: Row) -> RowResult[PartialOutput]:
    value = params.source.read(row)
    return RowResult(
        output=PartialOutput(
            stable=Outcome.ok(value.upper(), confidence=0.95),
            fallible=(
                Outcome.failed("domain_lookup_failed", "No domain match")
                if value == "two"
                else Outcome.ok(value[::-1], confidence=0.25)
            ),
            error=f"literal {value}",
        )
    )


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "map",
            actions=[
                action(
                    name="upper_test",
                    title="Upper test",
                    description="Uppercase a source value.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(upper),
                ),
                action(
                    name="partial_test",
                    title="Partial test",
                    description="Materialize successful siblings on partial failure.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(partial),
                ),
            ],
        )
    ]
)


@pytest.fixture
def project(tmp_path: Path):
    value = Project.create(tmp_path / "typed-lifecycle.frisket")
    sheet_id = value.add_sheet("data")
    source_id = value.add_column(sheet_id, "source")
    row_ids = value.add_rows(
        sheet_id,
        [{"source": "one"}, {"source": "two"}],
        {"source": source_id},
    )
    yield value, sheet_id, source_id, row_ids
    value.close()


def _request(
    sheet_id: int,
    row_ids: list[int],
    *,
    key: str = "upper@1",
    output: str = "upper",
) -> ActionRequest:
    return ActionRequest(
        action_id="map.upper_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source"},
        output_names={"upper": output},
        idempotency_key=key,
    )


def _runner(project: Project, router: Any | None) -> MapRunner:
    return MapRunner(
        project,
        router or ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def _hash_request(action_id: str, request: ActionRequest) -> str:
    return typed_request_hash(
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(action_id), request)
    )


def run_typed_map_rows_action(
    project: Project,
    project_id: str,
    action: Any,
    request: ActionRequest,
    router: Any | None,
    factory: Any,
):
    return _run_typed_map_rows_action(
        project,
        project_id,
        BoundTypedActionRequest.bind(action, request),
        router,
        factory,
    )


def _forbid_registry_lookup(*_args: Any, **_kwargs: Any) -> Recipe:
    raise AssertionError("typed actions must not use the legacy recipe registry")


def test_typed_action_reserves_runs_and_finalizes_generic_receipt_refs(
    project: tuple[Project, int, int, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sheet_id, source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    observed_reservation: list[str] = []

    def factory(project: Project, router: Any | None) -> MapRunner:
        receipt = ReceiptStore(project).find_by_idempotency_key(request.idempotency_key)
        observed_reservation.append(receipt.status if receipt is not None else "")
        return _runner(project, router)

    monkeypatch.setattr(validation, "recipe_for_spec", _forbid_registry_lookup)
    result = run_typed_map_rows_action(
        store,
        "project-1",
        REGISTRY.get(request.action_id),
        request,
        ModelRouter(cache=None, cache_mode="off"),
        factory,
    )

    assert observed_reservation == ["running"]
    assert result.status == "completed", result
    assert result.action.kind == "map.upper_test"
    assert result.run_id is not None
    assert len(result.op_ids) == 1
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "upper")
    ]
    output_id = result.outputs[0].column_id
    assert output_id is not None
    assert result.outputs[0].row_ids == row_ids
    assert store.get_values(sheet_id, output_id, row_ids=row_ids) == {
        row_ids[0]: "ONE",
        row_ids[1]: "TWO",
    }

    receipt = ReceiptStore(store).parsed_by_id(str(result.receipt_id))
    assert receipt is not None
    assert receipt.inputs[0].ref == {
        "kind": "source_column",
        "name": "source",
        "sheet_id": sheet_id,
        "column_id": source_id,
        "type": "text",
        "row_ids": row_ids,
    }
    assert receipt.outputs[0].ref["kind"] == "map_result_column"
    assert receipt.outputs[0].ref["column_id"] == output_id
    assert receipt.outputs[0].ref["row_ids"] == row_ids
    assert receipt.outputs[0].ref["value_hash"].startswith("sha256:")
    counts = next(
        evidence.ref
        for evidence in receipt.evidence
        if evidence.ref["kind"] == "map_rows_run_counts"
    )
    assert counts == {
        "kind": "map_rows_run_counts",
        "total_rows": 2,
        "completed_rows": 2,
        "failed_rows": 0,
        "failed_row_ids": [],
        "result_count": 2,
        "model_call_count": 0,
        "cost_actual": 0.0,
        "op_id": result.op_ids[0],
        "run_id": result.run_id,
    }
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


def test_typed_action_persists_independent_outcomes_and_partial_failure(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = ActionRequest(
        action_id="map.partial_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source"},
        idempotency_key="partial@1",
    )

    result = run_typed_map_rows_action(
        store, "project-1", REGISTRY.get(request.action_id), request, None, _runner
    )

    assert result.status == "partial", result.errors
    run = store.db.execute(
        "SELECT status, completed_rows, failed_rows FROM runs WHERE id=?",
        (result.run_id,),
    ).fetchone()
    assert dict(run) == {
        "status": "completed",
        "completed_rows": 2,
        "failed_rows": 1,
    }

    columns = {column["name"]: column["id"] for column in store.columns(sheet_id)}
    rows = {
        (row["row_id"], row["column_id"]): row
        for row in store.db.execute(
            "SELECT row_id, column_id, value, confidence, justification, error, "
            "error_code, outcome, publication_effect FROM results WHERE run_id=?",
            (result.run_id,),
        )
    }
    stable_second = rows[(row_ids[1], columns["stable"])]
    assert stable_second["value"] == '"TWO"'
    assert stable_second["confidence"] == 0.95
    assert stable_second["error"] is None

    fallible_first = rows[(row_ids[0], columns["fallible"])]
    assert fallible_first["value"] == '"eno"'
    assert fallible_first["confidence"] == 0.25

    fallible_second = rows[(row_ids[1], columns["fallible"])]
    assert fallible_second["value"] is None
    assert fallible_second["error"] == "No domain match"
    assert fallible_second["error_code"] == "domain_lookup_failed"
    assert fallible_second["outcome"] == "model_error"
    assert fallible_second["publication_effect"] == "publish_error"

    literal_error = rows[(row_ids[1], columns["error"])]
    assert literal_error["value"] == '"literal two"'
    assert literal_error["error"] is None


def test_typed_action_refuses_adopting_unmanaged_generated_output(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    store.add_column(sheet_id, "orphan", type="text", ai_generated=True)
    request = _request(sheet_id, row_ids, output="orphan").model_copy(
        update={"replace_existing": True}
    )

    result = run_typed_map_rows_action(
        store,
        "project-1",
        REGISTRY.get(request.action_id),
        request,
        None,
        _runner,
    )

    assert result.status == "failed"
    assert result.errors[0].code == "output_column_exists"
    assert "generation-managed" in result.errors[0].message
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_typed_action_rejects_source_type_before_reserving(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    store.add_column(sheet_id, "count", type="integer")
    request = ActionRequest(
        action_id="map.upper_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "count"},
        output_names={"upper": "upper_count"},
        idempotency_key="wrong-type@1",
    )

    result = run_typed_map_rows_action(
        store, "project-1", REGISTRY.get(request.action_id), request, None, _runner
    )

    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
    assert result.errors[0].details == {
        "columns": [
            {
                "name": "count",
                "actual_type": "integer",
                "accepted_column_types": ["text"],
            }
        ]
    }
    assert ReceiptStore(store).count() == 0
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_typed_action_replays_exact_hash_and_refuses_conflict(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    action = REGISTRY.get(request.action_id)
    first = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    counts = {
        "receipts": ReceiptStore(store).count(),
        "runs": int(store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "ops": int(store.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]),
    }

    replay = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    conflict = run_typed_map_rows_action(
        store,
        "project-1",
        action,
        _request(sheet_id, row_ids, output="different"),
        None,
        _runner,
    )

    assert replay == first
    assert counts == {
        "receipts": ReceiptStore(store).count(),
        "runs": int(store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "ops": int(store.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]),
    }
    assert conflict.status == "failed"
    assert conflict.errors[0].code == "idempotency_conflict"


@pytest.mark.parametrize(
    ("action_id", "params", "logical_output", "final_output", "expected_params"),
    [
        (
            "map.template",
            {"template": {"text": "{{source}}"}},
            "rendered",
            "rendered_source",
            {"template": {"text": "{{source}}"}},
        ),
        (
            "map.clean_dates",
            {"source": "source", "format": "  %Y  "},
            "cleaned",
            "filed_year",
            {"source": "source", "format": "%Y"},
        ),
        (
            "map.regex_extract",
            {"input_columns": ["source"], "pattern": r"(\w+)", "group": " 1 "},
            "extracted",
            "first_word",
            {
                "input_columns": ["source"],
                "pattern": r"(\w+)",
                "group": 1,
            },
        ),
    ],
)
def test_typed_receipt_preserves_normalized_request_provenance_on_replay(
    project: tuple[Project, int, int, list[int]],
    action_id: str,
    params: dict[str, Any],
    logical_output: str,
    final_output: str,
    expected_params: dict[str, Any],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = ActionRequest(
        action_id=action_id,
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(reversed(row_ids))),
        params=params,
        output_names={logical_output: final_output},
        idempotency_key=f"{action_id}-provenance@1",
    )
    action = ACTION_REGISTRY.get(action_id)

    first = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )

    assert first.status == "completed", first.errors
    assert [output.name for output in first.outputs] == [final_output]
    assert first.receipt_id is not None
    receipt_store = ReceiptStore(store)
    direct_body = receipt_store.body_by_id(first.receipt_id)
    receipt = receipt_store.parsed_by_id(first.receipt_id)
    assert receipt is not None
    request_evidence = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "typed_action_request"
    )
    assert request_evidence == {
        "kind": "typed_action_request",
        "action_id": action_id,
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": expected_params,
        "output_names": {logical_output: final_output},
        "replace_existing": False,
        "params_hash": receipt.params_hash,
        "op_id": first.op_ids[0],
        "run_id": first.run_id,
    }

    replay = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )

    assert replay == first
    assert receipt_store.body_by_id(first.receipt_id) == direct_body


def test_typed_request_hash_normalizes_omitted_and_explicit_defaults() -> None:
    base = {
        "action_id": "map.regex_extract",
        "scope": SheetRows(sheet_id=7, row_ids=(3, 5)),
        "output_names": {},
        "idempotency_key": "regex-defaults@1",
    }
    omitted = ActionRequest(
        **base,
        params={"input_columns": ["source"], "pattern": r"\w+"},
    )
    explicit = ActionRequest(
        **base,
        params={
            "input_columns": ["source"],
            "pattern": r"\w+",
            "all_matches": False,
            "group": None,
            "timeout_seconds": 0.25,
        },
    )

    assert _hash_request("map.regex_extract", omitted) == _hash_request(
        "map.regex_extract", explicit
    )


def test_typed_request_hash_uses_validator_normalized_params() -> None:
    padded = ActionRequest(
        action_id="map.clean_dates",
        scope=SheetRows(sheet_id=7),
        params={"source": "source", "format": "  %Y-%m-%d  "},
        idempotency_key="clean-date-format@1",
    )
    normalized = padded.model_copy(
        update={"params": {"source": "source", "format": "%Y-%m-%d"}}
    )

    assert _hash_request("map.clean_dates", padded) == _hash_request(
        "map.clean_dates", normalized
    )


def test_typed_request_hash_normalizes_omitted_identity_output_names() -> None:
    omitted = ActionRequest(
        action_id="map.regex_extract",
        scope=SheetRows(sheet_id=7),
        params={"input_columns": ["source"], "pattern": r"\w+"},
        output_names={},
        idempotency_key="regex-output-name@1",
    )
    explicit = omitted.model_copy(update={"output_names": {"extracted": "extracted"}})

    assert _hash_request("map.regex_extract", omitted) == _hash_request(
        "map.regex_extract", explicit
    )


def test_typed_request_hash_treats_row_scope_as_an_orderless_set() -> None:
    descending = ActionRequest(
        action_id="map.regex_extract",
        scope=SheetRows(sheet_id=7, row_ids=(5, 3)),
        params={"input_columns": ["source"], "pattern": r"\w+"},
        idempotency_key="regex-row-order@1",
    )
    ascending = descending.model_copy(
        update={"scope": SheetRows(sheet_id=7, row_ids=(3, 5))}
    )

    assert _hash_request("map.regex_extract", descending) == _hash_request(
        "map.regex_extract", ascending
    )


def test_semantically_equivalent_typed_requests_replay_without_project_planning(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    action = ACTION_REGISTRY.get("map.regex_extract")
    first_request = ActionRequest(
        action_id=action.action_id,
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(reversed(row_ids))),
        params={"input_columns": ["source"], "pattern": r"\w+"},
        output_names={},
        idempotency_key="semantic-regex-replay@1",
    )
    equivalent_request = ActionRequest(
        action_id=action.action_id,
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(sorted(row_ids))),
        params={
            "input_columns": ["source"],
            "pattern": r"\w+",
            "all_matches": False,
            "group": None,
            "timeout_seconds": 0.25,
        },
        output_names={"extracted": "extracted"},
        idempotency_key=first_request.idempotency_key,
    )

    first = run_typed_map_rows_action(
        store, "project-1", action, first_request, None, _runner
    )

    def forbidden_factory(_project: Any, _router: Any | None) -> MapRunner:
        raise AssertionError("equivalent replay must not re-plan or execute")

    replay = run_typed_map_rows_action(
        store,
        "project-1",
        action,
        equivalent_request,
        None,
        forbidden_factory,
    )

    assert first.status == "completed", first.errors
    assert replay == first


def test_typed_action_refuses_in_progress_reservation(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    action = REGISTRY.get(request.action_id)
    first = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    store.db.execute(
        "UPDATE receipts SET status='running' WHERE id=?", (first.receipt_id,)
    )
    store.db.commit()

    result = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )

    assert result.status == "failed"
    assert result.errors[0].code == "idempotency_in_progress"
    assert result.errors[0].details["receipt_id"] == first.receipt_id


def test_typed_action_clears_stale_running_reservation_before_retry(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    action = REGISTRY.get(request.action_id)
    params_hash = typed_request_hash(BoundTypedActionRequest.bind(action, request))
    stale = Receipt(
        receipt_id="receipt_stale_typed_map_rows",
        project_id="project-1",
        action_id="act_stale_typed_map_rows",
        action_kind=action.action_id,
        idempotency_key=request.idempotency_key,
        params_hash=params_hash,
        status="running",
        inputs=[
            ReceiptIO(
                name="idempotency",
                ref={
                    "kind": "typed_map_rows_idempotency_reservation",
                    "params_hash": params_hash,
                },
            )
        ],
    )
    ReceiptStore(store).insert_running(stale)
    store.db.execute(
        "UPDATE receipts SET created_at='2000-01-01 00:00:00' WHERE id=?",
        (stale.receipt_id,),
    )
    store.db.commit()

    def forbidden_factory(_project: Any, _router: Any | None) -> MapRunner:
        raise AssertionError("stale reservation recovery must not run rows")

    cleared = run_typed_map_rows_action(
        store, "project-1", action, request, None, forbidden_factory
    )

    assert cleared.status == "failed"
    assert cleared.errors[0].code == "idempotency_stale_running"
    assert cleared.errors[0].field == "idempotency_key"
    assert cleared.errors[0].details["receipt_id"] == stale.receipt_id
    assert cleared.errors[0].details["retryable"] is True
    assert cleared.errors[0].details["stale_after_seconds"] == 3600
    assert ReceiptStore(store).find_by_id(stale.receipt_id) is None

    retried = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    assert retried.status == "completed", retried.errors


def test_typed_action_replay_rejects_renamed_output(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    action = REGISTRY.get(request.action_id)
    first = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    output_id = first.outputs[0].column_id
    assert output_id is not None
    store.db.execute("UPDATE columns SET name='renamed_upper' WHERE id=?", (output_id,))
    store.db.commit()

    def forbidden_factory(_project: Any, _router: Any | None) -> MapRunner:
        raise AssertionError("stale replay validation must not run rows")

    replay = run_typed_map_rows_action(
        store, "project-1", action, request, None, forbidden_factory
    )

    assert replay.status == "failed"
    error = replay.errors[0]
    assert error.code == "stale_replay"
    assert error.details == {
        "receipt_id": first.receipt_id,
        "output": "upper",
        "column_id": output_id,
        "expected_name": "upper",
        "current_name": "renamed_upper",
    }


def test_typed_action_collision_preserves_recovery_identity(
    project: tuple[Project, int, int, list[int]],
) -> None:
    store, sheet_id, _source_id, row_ids = project
    seeded_op_count = store.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    request = _request(sheet_id, row_ids)
    blocker = Receipt(
        receipt_id="receipt_typed_map_rows_blocker",
        project_id="project-1",
        action_id="act_typed_map_rows_blocker",
        action_kind="map.blocker",
        idempotency_key="blocker@1",
        params_hash="sha256:blocker",
        status="running",
    )
    ReceiptStore(store).insert_running(blocker)
    claims, conflict = OutputColumnClaimStore(store).acquire(
        sheet_id=sheet_id,
        output_names=["upper"],
        action_kind=blocker.action_kind,
        receipt_id=blocker.receipt_id,
        claim_token="claim:typed-map-rows-blocker",
        lease_seconds=60,
    )
    assert conflict is None

    result = run_typed_map_rows_action(
        store,
        "project-1",
        REGISTRY.get(request.action_id),
        request,
        None,
        _runner,
    )

    assert result.status == "failed"
    error = result.errors[0]
    assert error.code == "output_column_busy"
    assert error.field == "output_names"
    assert error.details == {
        "sheet_id": sheet_id,
        "column_id": None,
        "output_name": "upper",
        "run_id": None,
        "receipt_id": blocker.receipt_id,
        "job_id": None,
        "claim_id": claims[0]["id"],
        "action_kind": blocker.action_kind,
        "mode": "replace",
        "lease_expires_at": claims[0]["lease_expires_at"],
        "requires_recovery": True,
    }
    assert ReceiptStore(store).count() == 1
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == seeded_op_count


def test_typed_action_maps_network_refusal_and_cleans_reservation(
    project: tuple[Project, int, int, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)

    def refuse_network(*_args: Any, **_kwargs: Any) -> None:
        raise NetworkDisabled("external:typed-test")

    monkeypatch.setattr(MapRunner, "_prepare", refuse_network)
    result = run_typed_map_rows_action(
        store,
        "project-1",
        REGISTRY.get(request.action_id),
        request,
        None,
        _runner,
    )

    assert result.status == "failed"
    assert result.errors[0].code == "network_disabled"
    assert result.errors[0].details == {"capability": "external:typed-test"}
    assert ReceiptStore(store).count() == 0
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    claim = store.db.execute("SELECT status FROM output_column_claims").fetchone()
    assert claim is not None
    assert claim["status"] == "failed"


def test_typed_action_terminalizes_failed_run_and_retries_cleanly(
    project: tuple[Project, int, int, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = _request(sheet_id, row_ids)
    action = REGISTRY.get(request.action_id)
    original_run = MapRunner.run

    async def fail_run(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic typed map failure")

    monkeypatch.setattr(MapRunner, "run", fail_run)
    failed = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )

    assert failed.status == "failed"
    assert failed.errors[0].code == "map_rows_failed"
    assert ReceiptStore(store).count() == 0
    run = store.db.execute("SELECT status, finished_at FROM runs").fetchone()
    assert run is not None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    hidden = store.db.execute(
        "SELECT hidden FROM columns WHERE sheet_id=? AND name='upper'", (sheet_id,)
    ).fetchone()
    assert hidden is not None
    assert hidden["hidden"] == 1

    monkeypatch.setattr(MapRunner, "run", original_run)
    retried = run_typed_map_rows_action(
        store, "project-1", action, request, None, _runner
    )
    assert retried.status == "completed", retried.errors

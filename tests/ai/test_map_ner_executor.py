from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    case_env,
)
from frisket.ai.llm import ModelRouter
from typed_model_fixtures import model_request
from frisket.engine.store import Project
from frisket.ops.ner_evidence import _write_entity_spans

# Reset by _patch_sidecar at the start of every harness test for this case;
# asserts on it are only meaningful behind that patch.
_SIDECAR_CALLS: list[tuple[str, dict[str, Any]]] = []


def test_entity_span_result_lookup_chunks_large_scope(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "spans.frisket")
    try:
        sheet_id = project.add_sheet("rows")
        output_column = project.add_column(sheet_id, "entities", type="json")
        row_ids = project.add_rows(
            sheet_id,
            [{} for _ in range(901)],
            {},
        )
        project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 902)

        _write_entity_spans(
            project,
            {},
            sheet_id=sheet_id,
            output_column_id=output_column,
            run_id=999_999,
            op_id=1,
            receipt_id="receipt",
            input_column_ids={},
            row_ids=row_ids,
        )
    finally:
        project.close()


class _SidecarResponse:
    status_code = 200
    text = "ok"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.entities = entities

    def json(self) -> dict[str, Any]:
        return {"results": [self.entities]}


class _SidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs: Any) -> _SidecarResponse:
        _SIDECAR_CALLS.append((url, kwargs))
        text = str((kwargs.get("json") or {}).get("texts", [""])[0])
        if "Ada" in text:
            return _SidecarResponse(
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    }
                ]
            )
        return _SidecarResponse(
            [
                {
                    "text": "Analytical Engine",
                    "label": "organization",
                    "start": 18,
                    "end": 35,
                    "score": 0.91,
                }
            ]
        )


class _FailingSidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs: Any) -> Any:
        _SIDECAR_CALLS.append((url, kwargs))

        class _Resp:
            status_code = 503
            text = "sidecar unavailable"

            def json(self) -> dict[str, Any]:
                return {}

        return _Resp()


def _router_with_http(http: Any) -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = http  # noqa: SLF001
    return router


def _patch_sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    _SIDECAR_CALLS.clear()
    return {"router": _router_with_http(_SidecarHttp())}


def _map_ner_action(
    sheet_id: int,
    *,
    idempotency_key: str = "map_ner@sha256:stable",
    labels: list[str] | None = None,
    output_name: str = "entities",
    row_ids: list[int] | None = None,
) -> dict[str, Any]:
    return model_request(
        {
            "action_kind": "map.ner",
            "sheet_id": sheet_id,
            "input_columns": ["body"],
            "labels": labels if labels is not None else ["person", "organization"],
            "threshold": 0.5,
            "engine": "gliner",
            "output_name": output_name,
            "row_ids": row_ids,
            "idempotency_key": idempotency_key,
        }
    ).model_dump(mode="json", exclude_none=True)


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Transcripts")
    columns = {
        "body": project.add_column(sheet_id, "body", type="text"),
        "speaker": project.add_column(sheet_id, "speaker", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "body": "Ada Lovelace wrote notes for the Analytical Engine.",
                "speaker": "host",
            },
            {
                "body": "Babbage described the Analytical Engine in London.",
                "speaker": "guest",
            },
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_ner_action(seeded["sheet_id"])


def _missing_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_ner_action(
        seeded["sheet_id"], idempotency_key="map_ner@sha256:missing-cap"
    )
    action["capabilities"] = []
    return action


def _empty_labels_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_ner_action(
        seeded["sheet_id"],
        idempotency_key="map_ner@sha256:empty-labels",
        labels=[],
    )


def _colliding_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_ner_action(
        seeded["sheet_id"],
        idempotency_key="map_ner@sha256:collision",
        output_name="speaker",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    assert len(_SIDECAR_CALLS) == 2
    assert _SIDECAR_CALLS[0][0] == "http://models/ner"
    assert _SIDECAR_CALLS[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert _SIDECAR_CALLS[0][1]["json"]["labels"] == ["person", "organization"]
    assert _SIDECAR_CALLS[0][1]["json"]["threshold"] == 0.5

    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "entities"),
        ("named_result", "entities"),
    ]
    column_output = next(o for o in result.outputs if o.kind == "column")
    named_output = next(o for o in result.outputs if o.kind == "named_result")
    assert column_output.ref["kind"] == "map_result_column"
    assert named_output.ref["kind"] == "named_result"
    assert named_output.ref["source_action_kind"] == "map.ner"
    assert named_output.ref["schema"] == "entities_list"
    assert "EntityMention" in str(named_output.ref["item_schema"])
    assert named_output.ref["may_feed"] == ["derive.table_from_list"]

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "map.ner"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0
    assert run["cost_actual"] == 0.0

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    assert columns["entities"]["type"] == "json"
    assert columns["entities"]["ai_generated"] == 1
    values = project.get_values(sheet_id, int(columns["entities"]["id"]))
    assert values
    assert values[min(values)][0]["text"] == "Ada Lovelace"

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "map.ner"
    assert receipt.provider_use[0]["provider"] == "local"
    assert receipt.provider_use[0]["cost_actual"] == 0.0
    assert {item.ref["kind"] for item in receipt.inputs} == {"source_column"}
    column_ref = next(
        item.ref for item in receipt.outputs if item.ref["kind"] == "map_result_column"
    )
    named_ref = next(
        item.ref for item in receipt.outputs if item.ref["kind"] == "named_result"
    )
    assert column_ref["name"] == "entities"
    assert named_ref["source_action_kind"] == "map.ner"
    assert named_ref["output_key"] == "entities"
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    assert evidence["typed_action_request"]["params"]["labels"] == [
        "person",
        "organization",
    ]
    assert evidence["typed_action_request"]["params"]["engine"] == "gliner"
    assert evidence["map_rows_run_counts"]["completed_rows"] == 2


def _conflicting_labels_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_ner_action(seeded["sheet_id"], labels=["person"])


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    # ner tolerates "partial"/"failed" runs, so replay is receipt-scoped
    #: hiding the output column is still caught (an
    # identity check, not a row/value one), but the wire code is now the op's
    # own map_error_code instead of "stale_replay" -- see stale_code below.
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='entities'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


CASES = [
    ExecutorCase(
        kind="map.ner",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "call_model_router",
                    "write_model_calls",
                    "read_input_rows",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_receipt",
                    "write_trace",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "invalid_params",
                    "model_cost_requires_confirmation",
                    "output_column_exists",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_sidecar,
        gates=(
            Gate(
                "client_capability_field",
                _missing_capability_action,
                "invalid_action_request",
            ),
            Gate("invalid_params", _empty_labels_action, "invalid_action_request"),
            Gate(
                "output_column_exists",
                _colliding_output_action,
                "output_column_exists",
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
        # Replay rebuilds outputs from the receipt and only reconstructs the
        # named_result ref, dropping the column output.
        replay_output_names=False,
        replay_output_kinds=False,
        reservation=Reservation(
            make_conflict=_conflicting_labels_action,
            stale_code="stale_replay",
            go_stale=_go_stale,
        ),
    )
]


def test_map_ner_replay_does_not_reenter_the_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying the stored receipt must reuse the persisted results — the
    sidecar transport is never crossed again."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        _check_state(env.project, env.seeded, first)
        assert len(_SIDECAR_CALLS) == 2

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_SIDECAR_CALLS) == 2


def test_map_ner_rejects_explicit_empty_scope_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        with pytest.raises(ValueError, match="row ids must not be empty"):
            _map_ner_action(
                env.seeded["sheet_id"],
                idempotency_key="map_ner@sha256:exact-empty",
                row_ids=[],
            )
        assert _SIDECAR_CALLS == []


def test_map_ner_sidecar_failure_fails_run_and_clears_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        env.run_kwargs["router"] = _router_with_http(_FailingSidecarHttp())
        failing = env.run(
            _map_ner_action(
                env.seeded["sheet_id"],
                idempotency_key="map_ner@sha256:sidecar-failed",
            )
        )
        assert failing.status == "failed"
        assert failing.errors[0].code == "map_rows_failed"
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE status='running'"
            ).fetchone()[0]
            == 0
        )

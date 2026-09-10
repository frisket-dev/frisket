from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.contracts.action import (
    Receipt,
    ReceiptIO,
)
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import (
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
    typed_program_from_runner_spec,
    typed_queued_map_spec,
)
from frisket.sdk.capture import (
    MixedOriginInputProvenanceUnsupported,
    capture_maprunner_facts,
)
from frisket.sdk.replay import (
    output_column_result_value_hash,
    output_column_value_hash,
    output_columns_replay_error,
)
from helpers import RunWriterAuthorityFixture, run_writer_authority_fixture


_TEXT_OUTPUT = [{"name": "generated", "type": "text", "nullable": True}]


def _research_bound(sheet_id, row_ids=None):
    request = ActionRequest.model_validate(
        {
            "action_id": "research.answer",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                **({"row_ids": row_ids} if row_ids is not None else {}),
            },
            "params": {
                "source": ["generated"],
                "question": {"text": "What does this row establish?"},
                "model": "anthropic/claude-haiku-4-5",
            },
            "output_names": {"answer": "answer", "sources": "answer_sources"},
            "idempotency_key": "research-generation-proof",
        }
    )
    return BoundTypedActionRequest.bind(ACTION_REGISTRY.get("research.answer"), request)


def _typed_model_rows_error(
    project: Project,
    *,
    sheet_id: int,
    source: str,
    row_ids: list[int] | None = None,
) -> Exception:
    request = ActionRequest.model_validate(
        {
            "action_id": "map.ask",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                **({"row_ids": row_ids} if row_ids is not None else {}),
            },
            "params": {
                "source": [source],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What happened?",
            },
            "output_names": {"answer": "answer"},
            "idempotency_key": "typed-provenance@1",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request)
    with pytest.raises(ValueError) as caught:
        build_typed_map_rows_plan(project, bound)
    return caught.value


@dataclass(frozen=True)
class _PublishedRun:
    op_id: int
    run_id: int


@pytest.fixture
def sheet(tmp_path: Path):
    project = Project.create(tmp_path / "sdk-generations.frisket", name="SDK")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source")
    project.add_rows(
        sheet_id,
        [{"source": "alpha"}, {"source": "beta"}, {"source": "gamma"}],
        {"source": source_column_id},
    )
    row_ids = project.visible_row_ids(sheet_id)
    try:
        yield project, sheet_id, source_column_id, row_ids
    finally:
        project.close()


def _start_run(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    action_kind: str = "map.regex_extract",
    run_params: dict[str, Any] | None = None,
) -> tuple[int, int, RunWriterAuthorityFixture]:
    op_id = project.append_op(
        action_kind,
        {"column_id": column_id, "row_ids": row_ids},
        label=f"publish {action_kind}",
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        action_kind,
        params=run_params,
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={column_id}
    )
    return op_id, run_id, authority


def _release(project: Project, authority: RunWriterAuthorityFixture) -> None:
    assert authority.claim_token is not None
    assert (
        OutputColumnClaimStore(project).release(claim_token=authority.claim_token) >= 1
    )


def _publish_managed(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    output_role: str,
    values: dict[int, Any],
    write_mode: str,
    effects: dict[int, str] | None = None,
    run_params: dict[str, Any] | None = None,
) -> _PublishedRun:
    row_ids = list(values)
    op_id, run_id, authority = _start_run(
        project,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=row_ids,
        run_params=run_params,
    )
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        column_id,
        output_role=output_role,
        compatibility_key="sha256:text-output-v1",
        write_mode=write_mode,
        claim_token=authority.claim_token,
    )
    batch: list[dict[str, Any]] = []
    for row_id, value in values.items():
        effect = (effects or {}).get(row_id, "publish_value")
        item: dict[str, Any] = {
            "row_id": row_id,
            "column_id": column_id,
            "value": value,
            "publication_effect": effect,
        }
        if effect == "publish_error":
            item.update(error="fixture error", error_code="fixture_error")
        batch.append(item)
    RunResultStore(project).write_results(run_id, batch, **authority.kwargs())
    generations.seal(
        run_id,
        [column_id],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, column_id, run_id)
    _release(project, authority)
    return _PublishedRun(op_id=op_id, run_id=run_id)


def _publish_legacy(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    values: dict[int, Any],
    action_kind: str = "map.summarize",
) -> _PublishedRun:
    row_ids = list(values)
    op_id, run_id, authority = _start_run(
        project,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=row_ids,
        action_kind=action_kind,
    )
    RunResultStore(project).write_results(
        run_id,
        [
            {"row_id": row_id, "column_id": column_id, "value": value}
            for row_id, value in values.items()
        ],
        **authority.kwargs(),
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, column_id, run_id)
    _release(project, authority)
    return _PublishedRun(op_id=op_id, run_id=run_id)


def _value_hash(row_ids: list[int], values: dict[int, Any]) -> str:
    encoded = json.dumps(
        [{"row_id": row_id, "value": values.get(row_id)} for row_id in row_ids],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _receipt(
    *,
    run_id: int,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    value_hash: str,
) -> Receipt:
    output_kind = "test_generation_output_column"
    return Receipt(
        receipt_id=f"receipt-{run_id}",
        project_id="project-1",
        action_id=f"action-{run_id}",
        action_kind="map.regex_extract",
        run_id=run_id,
        status="completed",
        outputs=[
            ReceiptIO(
                name="generated",
                kind=output_kind,
                ref={
                    "kind": output_kind,
                    "name": "generated",
                    "type": "text",
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "run_id": run_id,
                    "row_ids": row_ids,
                    "value_hash": value_hash,
                },
            )
        ],
    )


def test_capture_discovers_managed_output_by_sealed_binding_and_hashes_heads(
    sheet,
) -> None:
    project, sheet_id, source_column_id, row_ids = sheet
    output_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    published = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        output_role="generated",
        values={row_ids[0]: "head value", row_ids[1]: None},
        effects={row_ids[1]: "publish_error"},
        write_mode="create",
    )
    # The scalar mirror is deliberately stale/empty. Managed capture must not
    # use it for either discovery or the value hash.
    project.db.execute(
        "UPDATE columns SET current_run_id=NULL WHERE id=?", (output_column_id,)
    )
    project.db.commit()

    facts = capture_maprunner_facts(
        project,
        runner_spec={
            "action_kind": "map.summarize",
            "sheet_id": sheet_id,
            "input_columns": ["source"],
            "pattern": "(.*)",
            "output_name": "generated",
        },
        run_id=published.run_id,
        params_hash="sha256:params",
        input_column_ids={"source": source_column_id},
        input_column_types={"source": "text"},
        output_fields=_TEXT_OUTPUT,
    )

    assert facts.missing_outputs == []
    assert [(fact.name, fact.column_id) for fact in facts.output_facts] == [
        ("generated", output_column_id)
    ]
    assert facts.output_facts[0].value_hash == _value_hash(
        row_ids[:2], {row_ids[0]: "head value", row_ids[1]: None}
    )


def test_capture_refuses_recipe_binding_role_drift_instead_of_filtering(sheet) -> None:
    project, sheet_id, source_column_id, row_ids = sheet
    generated_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    optional_column_id = project.add_column(
        sheet_id, "generated_optional", type="text", ai_generated=True
    )
    op_id = project.append_op(
        "map.regex_extract", {}, label="two-role generation fixture"
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.regex_extract",
        row_ids=[row_ids[0]],
        total_rows=1,
    )
    authority = run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids={generated_column_id, optional_column_id},
    )
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)
    for role, column_id in (
        ("generated", generated_column_id),
        ("generated_optional", optional_column_id),
    ):
        generations.declare(
            run_id,
            column_id,
            output_role=role,
            compatibility_key="sha256:text-output-v1",
            write_mode="create",
            claim_token=authority.claim_token,
        )
    RunResultStore(project).write_results(
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": role,
                "publication_effect": "publish_value",
            }
            for role, column_id in (
                ("generated", generated_column_id),
                ("generated_optional", optional_column_id),
            )
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run_id,
        [generated_column_id, optional_column_id],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    RunResultStore(project).finish_run(run_id)

    with pytest.raises(GenerationStateError, match="unexpected=.*generated_optional"):
        capture_maprunner_facts(
            project,
            runner_spec={
                "action_kind": "map.summarize",
                "sheet_id": sheet_id,
                "input_columns": ["source"],
                "pattern": "(.*)",
                "output_name": "generated",
            },
            run_id=run_id,
            params_hash="sha256:params",
            input_column_ids={"source": source_column_id},
            input_column_types={"source": "text"},
            output_fields=_TEXT_OUTPUT,
        )
    _release(project, authority)


def test_managed_hash_uses_heads_and_preserves_edit_surface_policy(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    output_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        output_role="generated",
        values={row_ids[0]: "first", row_ids[1]: "second"},
        write_mode="create",
    )
    expected = _value_hash(row_ids[:2], {row_ids[0]: "first", row_ids[1]: "second"})
    assert (
        output_column_result_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=output_column_id,
            row_ids=row_ids[:2],
        )
        == expected
    )

    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": output_column_id, "value": "edited"}],
        label="edit generated output",
    )
    assert output_column_value_hash(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        row_ids=row_ids[:2],
    ) == _value_hash(row_ids[:2], {row_ids[0]: "edited", row_ids[1]: "second"})
    assert (
        output_column_result_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=output_column_id,
            row_ids=row_ids[:2],
        )
        == expected
    )


def test_replay_checks_receipt_rows_against_exact_active_heads(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    output_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    first = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        output_role="generated",
        values={row_id: f"first:{index}" for index, row_id in enumerate(row_ids)},
        write_mode="create",
    )
    receipt_rows = [row_ids[0]]
    receipt = _receipt(
        run_id=first.run_id,
        sheet_id=sheet_id,
        column_id=output_column_id,
        row_ids=receipt_rows,
        value_hash=output_column_result_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=output_column_id,
            row_ids=receipt_rows,
        ),
    )

    # A replacement elsewhere changes the scalar mirror but not this receipt's
    # exact active row, so the replay remains valid.
    second = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        output_role="generated",
        values={row_ids[1]: "second:elsewhere"},
        write_mode="replace_scope",
    )
    assert project.get_column(output_column_id)["current_run_id"] == second.run_id
    assert (
        output_columns_replay_error(
            project,
            receipt,
            output_kind="test_generation_output_column",
            action_kind="map.regex_extract",
            scope="receipt",
        )
        is None
    )

    # Replacing the receipt's own row invalidates it even if the old result row
    # remains in the immutable journal.
    third = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=output_column_id,
        output_role="generated",
        values={row_ids[0]: "third:receipt-row"},
        write_mode="replace_scope",
    )
    error = output_columns_replay_error(
        project,
        receipt,
        output_kind="test_generation_output_column",
        action_kind="map.regex_extract",
        scope="receipt",
    )
    assert error is not None
    assert error.code == "stale_replay"
    assert error.details["changed_row_ids"] == receipt_rows
    assert error.details["current_head_run_ids"] == [third.run_id]


def test_rich_input_provenance_is_scoped_or_typed_refused(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    first = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_id: f"first:{index}" for index, row_id in enumerate(row_ids)},
        write_mode="create",
    )
    queued_request = ActionRequest.model_validate(
        {
            "action_id": "map.ask",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["generated"],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What happened?",
            },
            "output_names": {"answer": "answer"},
            "idempotency_key": "queued-provenance@1",
        }
    )
    queued_bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.ask"), queued_request
    )
    _queued_action, queued_spec, _program = typed_queued_map_spec(queued_bound)
    runner_spec = queued_spec.runner_spec_fn(queued_bound.params)
    resolved = queued_spec.resolve_fn(
        project,
        queued_bound.params,
        runner_spec,
    )
    assert isinstance(resolved, dict)
    queued_payload = {
        "v1_input_column_ids": resolved["input_column_ids"],
        "v1_input_column_types": resolved["input_column_types"],
        "v1_output_names": resolved["output_names"],
        "v1_output_target_preconditions": resolved["output_target_preconditions"],
        "spec": runner_spec,
    }
    second = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_ids[1]: "second:mixed"},
        write_mode="replace_scope",
    )
    assert project.get_column(input_column_id)["current_run_id"] == second.run_id
    assert queued_spec.pre_run_guard is not None
    stale = queued_spec.pre_run_guard(project, queued_payload, queued_bound.params)
    assert stale is not None
    assert stale.code == "stale_input"
    assert stale.field == "scope"
    assert stale.message == "queued action inputs are no longer valid"
    assert stale.details["reason"] == "mixed_origin_input_provenance_unsupported"

    refused = _typed_model_rows_error(project, sheet_id=sheet_id, source="generated")
    assert getattr(refused, "code", None) == "invalid_input_ref"
    assert getattr(refused, "details") == {
        "reason": "mixed_origin_input_provenance_unsupported",
        "column": "generated",
        "column_id": input_column_id,
        "origin_run_ids": [second.run_id, first.run_id],
        "missing_head_row_ids": [],
        "divergent_edit_row_ids": [],
    }

    from frisket.authoring import copilot
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    workspace = SimpleNamespace(edition="solo", get=lambda _project_id: project)
    live = ActionParamValidationService(workspace).validate_params(
        "project-1",
        {
            "action_id": "map.ask",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["generated"],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What happened?",
            },
        },
    )
    assert live["diagnostics"] == {
        "source": {
            "ok": False,
            "message": "map.ask cannot represent mixed-origin input column provenance",
        }
    }
    assert (
        copilot.validate_proposals(
            project,
            {
                "proposals": [
                    {
                        "kind": "map",
                        "title": "Ask about generated text",
                        "spec": {
                            "action_kind": "map.ask",
                            "sheet_id": sheet_id,
                            "source": ["generated"],
                            "model": "anthropic/claude-haiku-4-5",
                            "question": "What happened?",
                            "output_names": {"answer": "answer"},
                        },
                    }
                ]
            },
        )
        == []
    )

    summary_column_id = project.add_column(
        sheet_id, "summary", type="text", ai_generated=True
    )
    scoped_output = _publish_legacy(
        project,
        sheet_id=sheet_id,
        column_id=summary_column_id,
        values={row_ids[0]: "summary"},
    )
    facts = capture_maprunner_facts(
        project,
        runner_spec={
            "action_kind": "map.summarize",
            "sheet_id": sheet_id,
            "input_columns": ["generated"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "paragraph",
            "output_name": "summary",
        },
        run_id=scoped_output.run_id,
        params_hash="sha256:scoped",
        input_column_ids={"generated": input_column_id},
        input_column_types={"generated": "text"},
        output_fields=[{"name": "summary", "type": "text", "nullable": True}],
        rich_input_columns=True,
    )
    assert facts.input_columns_rich[0]["source_run_id"] == first.run_id

    mixed_output = _publish_legacy(
        project,
        sheet_id=sheet_id,
        column_id=summary_column_id,
        values={row_ids[0]: "one", row_ids[1]: "two"},
    )
    with pytest.raises(MixedOriginInputProvenanceUnsupported) as caught:
        capture_maprunner_facts(
            project,
            runner_spec={
                "action_kind": "map.summarize",
                "sheet_id": sheet_id,
                "input_columns": ["generated"],
                "model": "anthropic/claude-haiku-4-5",
                "preset": "paragraph",
                "output_name": "summary",
            },
            run_id=mixed_output.run_id,
            params_hash="sha256:mixed",
            input_column_ids={"generated": input_column_id},
            input_column_types={"generated": "text"},
            output_fields=[{"name": "summary", "type": "text", "nullable": True}],
            rich_input_columns=True,
        )
    assert caught.value.code == "mixed_origin_input_provenance_unsupported"
    assert caught.value.origin_run_ids == [second.run_id, first.run_id]


def test_rich_input_provenance_refuses_partial_head_coverage(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    published = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_ids[0]: "only one generated row"},
        write_mode="create",
    )
    refused = _typed_model_rows_error(project, sheet_id=sheet_id, source="generated")

    assert getattr(refused, "code", None) == "invalid_input_ref"
    details = getattr(refused, "details")
    assert details["reason"] == "mixed_origin_input_provenance_unsupported"
    assert details["origin_run_ids"] == [published.run_id]
    assert details["missing_head_row_ids"] == row_ids[1:]
    assert details["divergent_edit_row_ids"] == []


def test_rich_input_provenance_all_headless_scope_stays_baseline(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_ids[0]: "generated elsewhere"},
        write_mode="create",
    )
    summary_column_id = project.add_column(
        sheet_id, "summary", type="text", ai_generated=True
    )
    output = _publish_legacy(
        project,
        sheet_id=sheet_id,
        column_id=summary_column_id,
        values={row_ids[1]: "baseline summary"},
    )

    facts = capture_maprunner_facts(
        project,
        runner_spec={
            "action_kind": "map.summarize",
            "sheet_id": sheet_id,
            "input_columns": ["generated"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "paragraph",
            "output_name": "summary",
        },
        run_id=output.run_id,
        params_hash="sha256:headless",
        input_column_ids={"generated": input_column_id},
        input_column_types={"generated": "text"},
        output_fields=[{"name": "summary", "type": "text", "nullable": True}],
        rich_input_columns=True,
    )

    assert facts.input_columns_rich[0]["source_run_id"] is None
    assert facts.input_columns_rich[0]["source_receipt_id"] is None


def test_rich_input_provenance_refuses_divergent_manual_overlay(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    published = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_id: f"generated:{index}" for index, row_id in enumerate(row_ids)},
        write_mode="create",
    )
    project.apply_edits(
        [{"row_id": row_ids[1], "column_id": input_column_id, "value": "manual"}],
        label="manual input override",
    )
    refused = _typed_model_rows_error(project, sheet_id=sheet_id, source="generated")

    assert getattr(refused, "code", None) == "invalid_input_ref"
    details = getattr(refused, "details")
    assert details["origin_run_ids"] == [published.run_id]
    assert details["missing_head_row_ids"] == []
    assert details["divergent_edit_row_ids"] == [row_ids[1]]


def _seed_unrepresentable_managed_input(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    scenario: str,
) -> dict[str, list[int]]:
    if scenario == "mixed":
        first = _publish_managed(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            output_role="generated",
            values={row_id: f"first:{index}" for index, row_id in enumerate(row_ids)},
            write_mode="create",
        )
        second = _publish_managed(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            output_role="generated",
            values={row_ids[1]: "second"},
            write_mode="replace_scope",
        )
        return {
            "origin_run_ids": [second.run_id, first.run_id],
            "missing_head_row_ids": [],
            "divergent_edit_row_ids": [],
        }
    published = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=column_id,
        output_role="generated",
        values=(
            {row_ids[0]: "partial"}
            if scenario == "partial"
            else {row_id: f"generated:{index}" for index, row_id in enumerate(row_ids)}
        ),
        write_mode="create",
    )
    if scenario in {"manual_edit", "manual_equal"}:
        project.apply_edits(
            [
                {
                    "row_id": row_ids[1],
                    "column_id": column_id,
                    "value": (
                        "generated:1" if scenario == "manual_equal" else "manual"
                    ),
                }
            ],
            label="overlay paid input",
        )
    return {
        "origin_run_ids": [published.run_id],
        "missing_head_row_ids": row_ids[1:] if scenario == "partial" else [],
        "divergent_edit_row_ids": (
            [row_ids[1]] if scenario in {"manual_edit", "manual_equal"} else []
        ),
    }


@pytest.mark.parametrize(
    "scenario", ["mixed", "partial", "manual_edit", "manual_equal"]
)
def test_judge_typed_refuses_unrepresentable_managed_judged_input_before_effect(
    sheet, scenario: str
) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    judged_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    expected = _seed_unrepresentable_managed_input(
        project,
        sheet_id=sheet_id,
        column_id=judged_column_id,
        row_ids=row_ids,
        scenario=scenario,
    )
    request = ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["generated"],
                "judged_column": "generated",
                "model": "openai/gpt-5-mini",
                "guidelines": "The value must be supported.",
                "include_original_prompt": True,
            },
            "idempotency_key": f"judge-{scenario}",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.judge"), request)
    before = (
        project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
    )

    with pytest.raises(TypedMapRowsPlanError) as raised:
        build_typed_map_rows_plan(project, bound)

    assert raised.value.code == "invalid_input_ref"
    assert dict(raised.value.details) == {
        "reason": "mixed_origin_input_provenance_unsupported",
        "column": "generated",
        "column_id": judged_column_id,
        **expected,
    }
    assert (
        project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
    ) == before


def test_judge_typed_refuses_legacy_ai_scalar_without_generation_heads(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    judged_column_id = project.add_column(
        sheet_id, "legacy_generated", type="text", ai_generated=True
    )
    published = _publish_legacy(
        project,
        sheet_id=sheet_id,
        column_id=judged_column_id,
        values={row_ids[0]: "legacy", row_ids[1]: None},
    )
    assert project.get_column(judged_column_id)["current_run_id"] == published.run_id
    request = ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["source"],
                "judged_column": "legacy_generated",
                "model": "openai/gpt-5-mini",
                "guidelines": "The value must be supported.",
            },
            "idempotency_key": "judge-legacy-scalar",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.judge"), request)

    with pytest.raises(TypedMapRowsPlanError) as raised:
        build_typed_map_rows_plan(project, bound)

    assert raised.value.code == "invalid_input_ref"
    assert "no generated provenance" in str(raised.value)
    assert dict(raised.value.details) == {"columns": ["legacy_generated"]}


def test_judge_typed_refuses_generated_column_without_selected_scope_provenance(
    sheet,
) -> None:
    project, sheet_id, _source_column_id, _row_ids = sheet
    project.add_column(sheet_id, "generated", type="text", ai_generated=True)
    request = ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["source"],
                "judged_column": "generated",
                "model": "openai/gpt-5-mini",
                "guidelines": "The value must be supported.",
            },
            "idempotency_key": "judge-missing-provenance",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.judge"), request)

    with pytest.raises(TypedMapRowsPlanError, match="no generated provenance"):
        build_typed_map_rows_plan(project, bound)


@pytest.mark.parametrize("scenario", ["mixed", "partial", "manual_edit"])
def test_research_answer_typed_refuses_unrepresentable_managed_input_before_effect(
    sheet, scenario: str
) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    expected = _seed_unrepresentable_managed_input(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        row_ids=row_ids,
        scenario=scenario,
    )
    before = (
        project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
    )

    with pytest.raises(TypedMapRowsPlanError) as caught:
        build_typed_map_rows_plan(project, _research_bound(sheet_id))
    refused = caught.value

    assert refused.code == "invalid_input_ref"
    assert "mixed-origin input column provenance" in str(refused)
    assert refused.details == {
        "reason": "mixed_origin_input_provenance_unsupported",
        "column": "generated",
        "column_id": input_column_id,
        **expected,
    }
    assert (
        project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
    ) == before


def test_judge_backfill_program_reresolves_provenance_for_retry_scope(sheet) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    judged_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    first = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=judged_column_id,
        output_role="generated",
        values={row_ids[0]: "first"},
        write_mode="create",
    )
    second = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=judged_column_id,
        output_role="generated",
        values={row_ids[1]: "second"},
        write_mode="replace_scope",
    )
    request = ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_ids[0]],
            },
            "params": {
                "source": ["source"],
                "judged_column": "generated",
                "model": "openai/gpt-5-mini",
                "guidelines": "The value must be supported.",
            },
            "idempotency_key": "judge-backfill-program",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.judge"), request)
    original = build_typed_map_rows_plan(project, bound)
    assert original.evaluation_context is not None
    assert original.evaluation_context.source_run_id == first.run_id

    backfill_spec = original.spec_dict()
    backfill_spec["row_ids"] = [row_ids[1]]
    program = typed_program_from_runner_spec(project, backfill_spec)

    assert program is not None
    assert backfill_spec["evaluation_context"]["source_run_id"] == second.run_id


def test_paid_scalar_bypasses_use_exact_scoped_head_instead_of_column_scalar(
    sheet,
) -> None:
    project, sheet_id, _source_column_id, row_ids = sheet
    input_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    first = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_id: f"first:{index}" for index, row_id in enumerate(row_ids)},
        write_mode="create",
        run_params={"instruction": "use the first scoped instruction"},
    )
    second = _publish_managed(
        project,
        sheet_id=sheet_id,
        column_id=input_column_id,
        output_role="generated",
        values={row_ids[1]: "second"},
        write_mode="replace_scope",
        run_params={"instruction": "do not use the newer outside-scope instruction"},
    )
    assert project.get_column(input_column_id)["current_run_id"] == second.run_id

    request = ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_ids[0]],
            },
            "params": {
                "source": ["generated"],
                "judged_column": "generated",
                "model": "openai/gpt-5-mini",
                "guidelines": "The value must be supported.",
                "include_original_prompt": True,
            },
            "idempotency_key": "judge-scoped-head",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.judge"), request)
    plan = build_typed_map_rows_plan(project, bound)
    assert plan.evaluation_context is not None
    assert plan.evaluation_context.subject_column_id == input_column_id
    assert plan.evaluation_context.source_run_id == first.run_id
    assert "first scoped instruction" in (plan.evaluation_context.original_prompt or "")
    assert "newer outside-scope" not in (plan.evaluation_context.original_prompt or "")

    research_plan = build_typed_map_rows_plan(
        project, _research_bound(sheet_id, [row_ids[0]])
    )
    assert research_plan.spec["row_ids"] == [row_ids[0]]
    assert research_plan.source_columns == ("generated",)

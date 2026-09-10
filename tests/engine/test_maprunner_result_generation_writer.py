from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from action_test_helpers import run_typed_map_request, typed_map_request
import frisket.engine.runner.map_runner as map_runner_module
import frisket.engine.runner.validation as validation_module
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.actions.system import BoundTypedActionRequest
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import ActionError, ActionResult
from frisket.engine.executor.map_rows_action import (
    _TypedMapRowsProgram,
    TypedMapRowsPlan,
    build_typed_map_rows_plan,
)
from frisket.engine.executor.queued_actions import queued_v1_terminal_failure_result
from frisket.engine.runner import MapRunner
from frisket.engine.runner.preparation import _validate_managed_recipe_identity
from frisket.engine.runner.publication import PreparedRowPublication
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import (
    GenerationDeclarationConflict,
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import OpContext, Recipe
from frisket.server.app import create_app
from runner_test_helpers import run_with_output_claim


def test_compatibility_key_allows_cross_producer_readable_shape() -> None:
    regex_output = {
        "name": "digits",
        "column_type": "text",
        "semantic_type": "identifier",
        "format": {"case": "preserve"},
        "schema": {"type": ["string", "null"]},
        "description": "regex-owned role and copy",
    }
    ocr_output = {
        **regex_output,
        "name": "recognized_identifier",
        "description": "different producer and output role",
    }

    assert _compatibility_key(field=regex_output) == _compatibility_key(
        field=ocr_output
    )
    assert _compatibility_key(field=regex_output) != _compatibility_key(
        field={**ocr_output, "semantic_type": "free_text"}
    )


def test_managed_recovery_refuses_claim_without_frozen_descriptors(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "old-managed-claim.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        project.add_column(
            sheet_id,
            "cleaned",
            type="text",
            ai_generated=True,
        )
        op_id = project.append_op("map", {"action_kind": "map.regex_extract"})
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.regex_extract",
        )
        claims = OutputColumnClaimStore(project)
        created, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["cleaned"],
            action_kind="map.regex_extract",
            claim_token="old-claim-without-output-fields",
            details={},
        )
        assert len(created) == 1 and conflict is None
        assert (
            claims.bind_to_run(
                claim_token="old-claim-without-output-fields",
                run_id=run_id,
                expected_output_names=["cleaned"],
            )
            == 1
        )

        assert claims.active_output_fields(
            claim_token="old-claim-without-output-fields",
            run_id=run_id,
        ) == [{"name": "cleaned", "column_type": "text"}]
        with pytest.raises(ValueError, match="no frozen output descriptors"):
            claims.active_output_fields(
                claim_token="old-claim-without-output-fields",
                run_id=run_id,
                require_frozen_descriptors=True,
            )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_managed_recovery_pins_recipe_name_and_version() -> None:
    recipe = _CancellationBatchRecipe()

    _validate_managed_recipe_identity(
        run_id=7,
        run_row={
            "action_kind": "map.regex_extract",
            "action_version": recipe.version,
        },
        recipe=recipe,
        action_kind="map.regex_extract",
    )
    with pytest.raises(
        GenerationStateError,
        match="recipe identity changed before recovery",
    ):
        _validate_managed_recipe_identity(
            run_id=7,
            run_row={
                "action_kind": "map.regex_extract",
                "action_version": "older-version",
            },
            recipe=recipe,
            action_kind="map.regex_extract",
        )


def test_scalar_only_existing_output_refuses_managed_adoption_without_blanking(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "legacy-scalar-adoption.frisket")
    try:
        sheet_id = project.add_sheet("Legacy")
        note_column = project.add_column(sheet_id, "note", type="text")
        output_id = project.add_column(
            sheet_id,
            "digits",
            type="text",
            ai_generated=True,
        )
        row_ids = project.add_rows(
            sheet_id,
            [{"note": "one 101"}, {"note": "two 202"}],
            {"note": note_column},
        )
        legacy_op_id = project.append_op(
            "map",
            {"action_kind": "map.regex_extract", "output_name": "digits"},
            label="legacy regex output",
        )
        store = RunResultStore(project)
        legacy_run_id = store.start_run(
            legacy_op_id,
            sheet_id,
            "map.regex_extract",
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        project.db.executemany(
            "INSERT INTO results (run_id,row_id,column_id,value,outcome) "
            "VALUES (?,?,?,?, 'ok')",
            [
                (legacy_run_id, row_ids[0], output_id, json.dumps("101")),
                (legacy_run_id, row_ids[1], output_id, json.dumps("202")),
            ],
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (legacy_run_id, output_id),
        )
        store.finish_run(legacy_run_id, "completed")
        before = project.get_values_with_refs(sheet_id, output_id, row_ids=row_ids)

        refused = run_typed_map_request(
            project,
            _regex_action(
                sheet_id,
                key="p3b-refuse-scalar-adoption@sha256:v1",
                row_ids=[row_ids[0]],
                overwrite=True,
            ),
            project_id="p3b-writer",
        )

        assert refused.status == "failed"
        assert refused.errors and refused.errors[0].code == "output_column_exists"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM run_output_generations WHERE column_id=?",
                (output_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.get_values_with_refs(
                sheet_id,
                output_id,
                row_ids=row_ids,
            )
            == before
        )
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["current_run_id"]
            == legacy_run_id
        )
    finally:
        project.close()


@dataclass
class _CancellationBatchRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"

    name: str = "regex_extract"
    llm: bool = False
    atomic_output_columns: bool = False
    cancellation_observed: bool = False

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["seed"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [
            {
                "name": "cleaned",
                "column_type": "text",
                "schema": {"type": "string"},
            }
        ]

    async def execute_batch(
        self,
        values_by_row: dict[int, dict[str, Any]],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[int, dict[str, Any]]:
        del ctx
        rows = list(values_by_row)
        if spec["phase"] == "missing":
            return {row_id: {} for row_id in rows}
        if spec["phase"] == "replacement":
            self.cancellation_observed = True
            return {rows[0]: {"cleaned": "replacement"}}
        return {
            row_id: {"cleaned": f"initial:{values_by_row[row_id]['seed']}"}
            for row_id in rows
        }


@dataclass
class _LegacyOverwriteRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"

    name: str = "template"
    llm: bool = False

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["note"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [{"name": "digits", "column_type": "text"}]

    async def execute(
        self,
        values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[str, Any]:
        del values, spec, ctx
        return {"digits": "legacy overwrite"}


def test_cancelled_existing_batch_leaves_unattempted_heads_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _CancellationBatchRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: (
            recipe
            if name in {recipe.name, "map.regex_extract"}
            else original_get_recipe(name)
        ),
    )
    project = Project.create(tmp_path / "cancelled-existing-generation.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        seed_column = project.add_column(sheet_id, "seed", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"seed": "a"}, {"seed": "b"}, {"seed": "c"}],
            {"seed": seed_column},
        )

        def run(phase: str, *, overwrite: bool) -> Any:
            spec = {
                "action_kind": "map.regex_extract",
                "sheet_id": sheet_id,
                "input_columns": ["seed"],
                "phase": phase,
                "overwrite": overwrite,
            }
            return asyncio.run(
                run_with_output_claim(
                    MapRunner(
                        project,
                        ModelRouter(keys={}),
                        should_cancel=(
                            (lambda _run_id: recipe.cancellation_observed)
                            if phase == "replacement"
                            else None
                        ),
                        authority=UnroutedOnlyAuthority(project),
                    ),
                    spec,
                )
            )

        first = run("initial", overwrite=False)
        assert first.done and not first.cancelled
        output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='cleaned'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        generation_store = ResultGenerationStore(project)
        first_heads = generation_store.read_cell_heads(output_id, row_ids)
        assert set(first_heads) == set(row_ids)
        assert {head.run_id for head in first_heads.values()} == {first.run_id}

        second = run("replacement", overwrite=True)
        assert second.cancelled
        second_rows = project.db.execute(
            "SELECT row_id,publication_effect FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id",
            (second.run_id, output_id),
        ).fetchall()
        assert [
            (int(row["row_id"]), row["publication_effect"]) for row in second_rows
        ] == [(row_ids[0], "publish_value")]
        heads = generation_store.read_cell_heads(output_id, row_ids)
        assert heads[row_ids[0]].run_id == second.run_id
        assert heads[row_ids[0]].value == "replacement"
        assert heads[row_ids[1]] == first_heads[row_ids[1]]
        assert heads[row_ids[2]] == first_heads[row_ids[2]]
    finally:
        project.close()


def test_single_output_omission_publishes_an_explicit_error_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _CancellationBatchRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: (
            recipe
            if name in {recipe.name, "map.regex_extract"}
            else original_get_recipe(name)
        ),
    )
    project = Project.create(tmp_path / "missing-single-output.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        seed_column = project.add_column(sheet_id, "seed", type="text")
        (row_id,) = project.add_rows(
            sheet_id,
            [{"seed": "a"}],
            {"seed": seed_column},
        )
        progress = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    authority=UnroutedOnlyAuthority(project),
                ),
                {
                    "action_kind": "map.regex_extract",
                    "sheet_id": sheet_id,
                    "input_columns": ["seed"],
                    "phase": "missing",
                },
            )
        )
        output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='cleaned'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        result = project.db.execute(
            "SELECT publication_effect,error_code,outcome FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (progress.run_id, row_id, output_id),
        ).fetchone()
        assert dict(result) == {
            "publication_effect": "publish_error",
            "error_code": "invalid_output",
            "outcome": "invalid_output",
        }
        head = ResultGenerationStore(project).read_cell_heads(output_id, [row_id])[
            row_id
        ]
        assert head.run_id == progress.run_id
        assert head.publication_effect == "publish_error"
        assert head.error_code == "invalid_output"
        # Fresh-column zero-success visibility keeps the CURRENT product
        # policy (doc 14 owner decision): a brand-new column whose every row
        # errored is hidden. The error head above is untouched by the hide —
        # visibility is presentation state, not publication state — and the
        # successful retry below reveals the recovered column.
        assert bool(
            project.db.execute(
                "SELECT hidden FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["hidden"]
        )

        retry = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    authority=UnroutedOnlyAuthority(project),
                ),
                {
                    "action_kind": "map.regex_extract",
                    "sheet_id": sheet_id,
                    "input_columns": ["seed"],
                    "phase": "initial",
                    "overwrite": True,
                },
            )
        )
        assert (
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='cleaned'",
                (sheet_id,),
            ).fetchone()["id"]
            == output_id
        )
        retry_head = ResultGenerationStore(project).read_cell_heads(
            output_id,
            [row_id],
        )[row_id]
        assert retry_head.run_id == retry.run_id
        assert retry_head.publication_effect == "publish_value"
        assert retry_head.value == "initial:a"
    finally:
        project.close()


def test_fresh_pre_dispatch_cancellation_still_seals_managed_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _CancellationBatchRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: (
            recipe
            if name in {recipe.name, "map.regex_extract"}
            else original_get_recipe(name)
        ),
    )
    project = Project.create(tmp_path / "fresh-pre-dispatch-cancel.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        seed_column = project.add_column(sheet_id, "seed", type="text")
        project.add_rows(sheet_id, [{"seed": "a"}], {"seed": seed_column})
        progress = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    should_cancel=lambda _run_id: True,
                    authority=UnroutedOnlyAuthority(project),
                ),
                {
                    "action_kind": "map.regex_extract",
                    "sheet_id": sheet_id,
                    "input_columns": ["seed"],
                    "phase": "initial",
                },
            )
        )
        assert progress.cancelled
        output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='cleaned'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        binding = ResultGenerationStore(project).get_binding(
            progress.run_id,
            output_id,
        )
        assert binding is not None
        assert binding.state == "sealed"
        assert binding.terminal_disposition == "cancelled"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (progress.run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_queued_pre_run_failure_seals_zero_effect_binding_then_allows_replace(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(tmp_path / "queued-pre-run-workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects",
            json={"name": "Queued pre-run generation"},
        ).json()["id"]
        imported = client.post(
            f"/api/projects/{project_id}/import/csv",
            files={"file": ("rows.csv", "note\none 101\ntwo 202\n", "text/csv")},
        )
        assert imported.status_code == 200, imported.text
        sheet_id = int(imported.json()["sheet_id"])
        project = client.app.state.workspace.get(project_id)
        queued_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_queued_python_action(
                sheet_id,
                key="p3b-queued-pre-run-failure@sha256:v1",
            ),
        )
        assert queued_response.status_code == 200, queued_response.text
        queued = ActionResult.model_validate(queued_response.json())
        assert queued.status == "queued"
        assert queued.run_id is not None and queued.job_id is not None
        job = client.app.state.workspace.queue.get(queued.job_id)
        assert job is not None
        output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='digits'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        binding = ResultGenerationStore(project).get_binding(
            int(queued.run_id),
            output_id,
        )
        assert binding is not None and binding.state == "active"

        failed = queued_v1_terminal_failure_result(
            project,
            job.payload,
            project_id=project_id,
            run_id=int(queued.run_id),
            error=ActionError(
                code="map_run_failed",
                message="forced pre-run refusal",
                action_kind="map.python",
            ),
            never_dispatched=True,
        )
        assert failed.status == "failed"
        sealed = ResultGenerationStore(project).get_binding(
            int(queued.run_id),
            output_id,
        )
        assert sealed is not None and sealed.state == "sealed"
        assert sealed.terminal_disposition == "failed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (int(queued.run_id),),
            ).fetchone()[0]
            == 0
        )
        replacement_plan = _typed_regex_plan(
            project,
            sheet_id,
            key="p3b-after-queued-pre-run-failure@sha256:v1",
            replace_existing=True,
        )

        replacement = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    authority=UnroutedOnlyAuthority(project),
                    allow_action_lifecycle_only_recipes=True,
                ),
                replacement_plan.spec_dict(),
                program=replacement_plan.program,
            )
        )
        assert replacement.done and replacement.failed == 0
        bindings = project.db.execute(
            "SELECT state,write_mode FROM run_output_generations "
            "WHERE column_id=? ORDER BY run_id",
            (output_id,),
        ).fetchall()
        assert [tuple(row) for row in bindings] == [
            ("sealed", "create"),
            ("sealed", "replace_scope"),
        ]


def _regex_action(
    sheet_id: int,
    *,
    key: str,
    row_ids: list[int] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    request = typed_map_request(
        "map.regex_extract",
        sheet_id,
        params={"input_columns": ["note"], "pattern": r"\d+"},
        output_names={"extracted": "digits"},
        idempotency_key=key,
        row_ids=row_ids,
    )
    if overwrite:
        request["replace_existing"] = True
    return request


def _queued_python_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "code": "result = {'digits': row['note']}",
            "return_schema": {
                "type": "object",
                "properties": {"digits": {"type": "string"}},
                "required": ["digits"],
            },
            "output_routes": [
                {
                    "name": "digits",
                    "path": "$.digits",
                    "target": {"kind": "column", "type": "text"},
                }
            ],
        },
        "idempotency_key": key,
    }


def _typed_regex_plan(
    project: Project,
    sheet_id: int,
    *,
    key: str,
    row_ids: list[int] | None = None,
    replace_existing: bool = False,
) -> TypedMapRowsPlan:
    request = ActionRequest.model_validate(
        _regex_action(
            sheet_id,
            key=key,
            row_ids=row_ids,
            overwrite=replace_existing,
        )
    )
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    return build_typed_map_rows_plan(project, bound)


def _seed_managed_regex_output(
    project: Project,
) -> tuple[int, list[int], int, int]:
    sheet_id = project.add_sheet("Rows")
    note_column = project.add_column(sheet_id, "note", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"note": "one 101"}, {"note": "two 202"}],
        {"note": note_column},
    )
    result = run_typed_map_request(
        project,
        _regex_action(
            sheet_id,
            key="p3b-managed-regex-seed@sha256:v1",
        ),
        project_id="p3b-writer",
    )
    assert result.status == "completed", result.errors
    output_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='digits'",
            (sheet_id,),
        ).fetchone()["id"]
    )
    binding = ResultGenerationStore(project).bindings_for_run(
        int(
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["current_run_id"]
        )
    )
    assert len(binding) == 1
    return sheet_id, row_ids, output_id, binding[0].run_id


def test_undo_then_same_name_managed_output_revives_as_staged_successor(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "managed-revival-after-undo.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        first_run = RunResultStore(project).get_run(first_run_id)
        assert first_run is not None
        assert project.undo() == int(first_run["op_id"])
        undone_column = project.db.execute(
            "SELECT hidden,current_run_id FROM columns WHERE id=?",
            (output_id,),
        ).fetchone()
        assert tuple(undone_column) == (1, None)
        assert ResultGenerationStore(project).read_cell_heads(output_id, row_ids) == {}

        replacement = run_typed_map_request(
            project,
            _regex_action(
                sheet_id,
                key="p3b-managed-revival-after-undo@sha256:v1",
                overwrite=True,
            ),
            project_id="p3b-writer",
        )

        assert replacement.status == "completed", replacement.errors
        assert replacement.run_id is not None
        revived = project.db.execute(
            "SELECT id,hidden,current_run_id FROM columns "
            "WHERE sheet_id=? AND name='digits'",
            (sheet_id,),
        ).fetchone()
        assert tuple(revived) == (output_id, 0, None)
        binding = ResultGenerationStore(project).get_binding(
            int(replacement.run_id), output_id
        )
        assert binding is not None
        assert binding.write_mode == "replace_scope"
        assert binding.expected_base_run_id is None
        assert binding.state == "sealed"
        assert {
            head.run_id
            for head in ResultGenerationStore(project)
            .read_cell_heads(output_id, row_ids)
            .values()
        } == {replacement.run_id}

        replacement_run = RunResultStore(project).get_run(int(replacement.run_id))
        assert replacement_run is not None
        assert project.undo() == int(replacement_run["op_id"])
        hidden_again = project.db.execute(
            "SELECT hidden,current_run_id FROM columns WHERE id=?",
            (output_id,),
        ).fetchone()
        assert tuple(hidden_again) == (1, None)
        assert ResultGenerationStore(project).read_cell_heads(output_id, row_ids) == {}

        assert project.redo() == int(replacement_run["op_id"])
        redone = project.db.execute(
            "SELECT hidden,current_run_id FROM columns WHERE id=?",
            (output_id,),
        ).fetchone()
        assert tuple(redone) == (0, None)
        assert {
            head.run_id
            for head in ResultGenerationStore(project)
            .read_cell_heads(output_id, row_ids)
            .values()
        } == {replacement.run_id}
    finally:
        project.close()


def test_incompatible_hidden_managed_revival_refuses_before_mutation(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "incompatible-hidden-managed-revival.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        first_run = RunResultStore(project).get_run(first_run_id)
        assert first_run is not None
        assert project.undo() == int(first_run["op_id"])

        def snapshot() -> tuple[object, ...]:
            return (
                tuple(
                    project.db.execute(
                        "SELECT id,type,format,semantic_type,hidden,current_run_id "
                        "FROM columns WHERE id=?",
                        (output_id,),
                    ).fetchone()
                ),
                int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]),
                int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
                int(
                    project.db.execute(
                        "SELECT COUNT(*) FROM output_column_claims "
                        "WHERE released_at IS NULL"
                    ).fetchone()[0]
                ),
                int(
                    project.db.execute(
                        "SELECT COUNT(*) FROM run_output_generations"
                    ).fetchone()[0]
                ),
            )

        before = snapshot()
        refused = run_typed_map_request(
            project,
            typed_map_request(
                "map.clean_dates",
                sheet_id,
                params={"source": "note"},
                output_names={"cleaned": "digits"},
                idempotency_key="p3b-incompatible-hidden-revival@sha256:v1",
            ),
            project_id="p3b-writer",
        )
        assert refused.status == "failed"
        assert refused.errors and refused.errors[0].code == "output_column_exists"
        assert snapshot() == before
        assert ResultGenerationStore(project).read_cell_heads(output_id, row_ids) == {}

        compatible = run_typed_map_request(
            project,
            _regex_action(
                sheet_id,
                key="p3b-compatible-after-hidden-refusal@sha256:v1",
            ),
            project_id="p3b-writer",
        )
        assert compatible.status == "completed", compatible.errors
        assert compatible.run_id is not None
        binding = ResultGenerationStore(project).get_binding(
            int(compatible.run_id), output_id
        )
        assert binding is not None
        assert binding.write_mode == "replace_scope"
        assert binding.expected_base_run_id is None
        assert binding.state == "sealed"
        assert {
            head.run_id
            for head in ResultGenerationStore(project)
            .read_cell_heads(output_id, row_ids)
            .values()
        } == {compatible.run_id}
    finally:
        project.close()


def test_unbound_managed_declaration_failure_never_moves_revival_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "unbound-managed-revival-refusal.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        first_run = RunResultStore(project).get_run(first_run_id)
        assert first_run is not None
        assert project.undo() == int(first_run["op_id"])

        original_declare = map_runner_module.declare_prepared_outputs

        def refuse_declaration(*_args: Any, **_kwargs: Any) -> None:
            raise GenerationDeclarationConflict("injected declaration refusal")

        monkeypatch.setattr(
            map_runner_module,
            "declare_prepared_outputs",
            refuse_declaration,
        )
        plan = _typed_regex_plan(
            project,
            sheet_id,
            key="p3b-unbound-declaration@sha256:v1",
            replace_existing=True,
        )
        with pytest.raises(
            GenerationDeclarationConflict, match="injected declaration refusal"
        ):
            asyncio.run(
                run_with_output_claim(
                    MapRunner(
                        project,
                        ModelRouter(keys={}),
                        authority=UnroutedOnlyAuthority(project),
                        allow_action_lifecycle_only_recipes=True,
                    ),
                    plan.spec_dict(),
                    program=plan.program,
                )
            )
        refused_run = project.db.execute(
            "SELECT id,status FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert refused_run is not None and refused_run["status"] == "failed"
        assert (
            ResultGenerationStore(project).get_binding(
                int(refused_run["id"]), output_id
            )
            is None
        )
        column = project.db.execute(
            "SELECT hidden,current_run_id FROM columns WHERE id=?", (output_id,)
        ).fetchone()
        assert tuple(column) == (1, None)

        monkeypatch.setattr(
            map_runner_module,
            "declare_prepared_outputs",
            original_declare,
        )
        compatible = run_typed_map_request(
            project,
            _regex_action(
                sheet_id,
                key="p3b-compatible-after-declaration-refusal@sha256:v1",
            ),
            project_id="p3b-writer",
        )
        assert compatible.status == "completed", compatible.errors
        assert compatible.run_id is not None
        assert {
            head.run_id
            for head in ResultGenerationStore(project)
            .read_cell_heads(output_id, row_ids)
            .values()
        } == {compatible.run_id}
    finally:
        project.close()


def test_typed_producer_refuses_an_existing_managed_output_before_mutation(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "legacy-on-managed.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        generation_store = ResultGenerationStore(project)
        before_heads = generation_store.read_cell_heads(output_id, row_ids)
        before_runs = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        replacement = run_typed_map_request(
            project,
            typed_map_request(
                "map.template",
                sheet_id,
                params={"template": {"text": "T={{note}}"}},
                output_names={"rendered": "digits"},
                idempotency_key="p3b-typed-on-managed@sha256:v1",
            ),
            project_id="p3b-writer",
        )

        assert replacement.status == "failed"
        assert replacement.errors[0].code == "output_column_exists"
        assert generation_store.read_cell_heads(output_id, row_ids) == before_heads
        assert (
            project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before_runs
        )
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["current_run_id"]
            == first_run_id
        )
    finally:
        project.close()


def test_runner_fence_refuses_legacy_producer_on_managed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "legacy-on-managed-runner-fence.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        generation_store = ResultGenerationStore(project)
        before_heads = generation_store.read_cell_heads(output_id, row_ids)
        recipe = _LegacyOverwriteRecipe()
        original_get_recipe = validation_module.get_recipe
        monkeypatch.setattr(
            validation_module,
            "get_recipe",
            lambda name: (
                recipe
                if name in {recipe.name, "map.template"}
                else original_get_recipe(name)
            ),
        )

        with pytest.raises(GenerationDeclarationConflict):
            asyncio.run(
                run_with_output_claim(
                    MapRunner(
                        project,
                        ModelRouter(keys={}),
                        authority=UnroutedOnlyAuthority(project),
                    ),
                    {
                        "action_kind": "map.template",
                        "sheet_id": sheet_id,
                        "input_columns": ["note"],
                        "overwrite": True,
                    },
                )
            )

        assert generation_store.read_cell_heads(output_id, row_ids) == before_heads
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["current_run_id"]
            == first_run_id
        )
        refused_run = project.db.execute(
            "SELECT id,status FROM runs WHERE action_kind='map.template' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert refused_run is not None and refused_run["status"] == "failed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (int(refused_run["id"]),),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_managed_producer_refuses_intervening_legacy_scalar_run(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "managed-after-legacy.frisket")
    try:
        sheet_id, row_ids, output_id, first_run_id = _seed_managed_regex_output(project)
        generation_store = ResultGenerationStore(project)
        before_heads = generation_store.read_cell_heads(output_id, row_ids)
        legacy_op_id = project.append_op(
            "map",
            {"action_kind": "map.template", "output_name": "digits"},
            label="intervening legacy output",
        )
        run_store = RunResultStore(project)
        legacy_run_id = run_store.start_run(
            legacy_op_id,
            sheet_id,
            "map.template",
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        project.db.executemany(
            "INSERT INTO results (run_id,row_id,column_id,value,outcome) "
            "VALUES (?,?,?,?, 'ok')",
            [
                (legacy_run_id, row_ids[0], output_id, json.dumps("legacy 1")),
                (legacy_run_id, row_ids[1], output_id, json.dumps("legacy 2")),
            ],
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (legacy_run_id, output_id),
        )
        run_store.finish_run(legacy_run_id, "completed")

        refused = run_typed_map_request(
            project,
            _regex_action(
                sheet_id,
                key="p3b-managed-after-legacy@sha256:v1",
                overwrite=True,
            ),
            project_id="p3b-writer",
        )

        assert refused.status == "failed"
        assert refused.errors and refused.errors[0].code == "output_column_exists"
        assert generation_store.read_cell_heads(output_id, row_ids) == before_heads
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (output_id,),
            ).fetchone()["current_run_id"]
            == legacy_run_id
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM run_output_generations WHERE column_id=?",
                (output_id,),
            ).fetchone()[0]
            == 1
        )
        assert first_run_id != legacy_run_id
    finally:
        project.close()


def test_regex_writer_streams_fresh_then_seals_exact_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "maprunner-generation-writer.frisket")
    try:
        sheet_id = project.add_sheet("Notes")
        note_column = project.add_column(sheet_id, "note", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"note": "one 101"},
                {"note": "two 202"},
                {"note": "three 303"},
                {"note": "four 404"},
            ],
            {"note": note_column},
        )
        generation_store = ResultGenerationStore(project)
        original_write_results = RunResultStore.write_results
        fresh_flushes: list[tuple[int, int]] = []

        def observe_fresh_flush(
            store: RunResultStore,
            run_id: int,
            batch: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            original_write_results(store, run_id, batch, **kwargs)
            run = store.get_run(run_id)
            assert run is not None
            assert run["status"] == "running"
            for result in batch:
                row_id = int(result["row_id"])
                column_id = int(result["column_id"])
                binding = generation_store.get_binding(run_id, column_id)
                assert binding is not None
                assert binding.write_mode == "create"
                assert binding.state == "active"
                head = generation_store.read_cell_heads(column_id, [row_id])[row_id]
                assert head.run_id == run_id
                assert head.value == result["value"]
                assert head.publication_effect == result["publication_effect"]
                fresh_flushes.append((row_id, column_id))

        monkeypatch.setattr(RunResultStore, "write_results", observe_fresh_flush)
        first = run_typed_map_request(
            project,
            _regex_action(sheet_id, key="p3b-writer-fresh@sha256:v1"),
            project_id="p3b-writer",
        )
        monkeypatch.setattr(RunResultStore, "write_results", original_write_results)

        assert first.status == "completed", first.errors
        output_row = project.db.execute(
            "SELECT id,current_run_id FROM columns WHERE sheet_id=? AND name='digits'",
            (sheet_id,),
        ).fetchone()
        assert output_row is not None
        output_id = int(output_row["id"])
        assert output_row["current_run_id"] == first.run_id
        assert {row_id for row_id, _column_id in fresh_flushes} == set(row_ids)
        first_binding = generation_store.get_binding(first.run_id, output_id)
        assert first_binding is not None
        assert first_binding.write_mode == "create"
        assert first_binding.state == "sealed"
        assert first_binding.terminal_disposition == "completed"
        first_heads = generation_store.read_cell_heads(output_id, row_ids)
        assert {row_id: head.value for row_id, head in first_heads.items()} == {
            row_ids[0]: "101",
            row_ids[1]: "202",
            row_ids[2]: "303",
            row_ids[3]: "404",
        }

        project.apply_edits(
            [
                {
                    "row_id": row_ids[0],
                    "column_id": note_column,
                    "value": "changed 909",
                },
                {
                    "row_id": row_ids[1],
                    "column_id": note_column,
                    "value": "no digits remain",
                },
                {
                    "row_id": row_ids[2],
                    "column_id": note_column,
                    "value": "unresolved producer output",
                },
            ],
            label="change exact regex scope",
        )
        original_execute = _TypedMapRowsProgram.execute

        async def omit_declared_output(
            recipe: _TypedMapRowsProgram,
            row_values: dict[str, Any],
            spec: dict[str, Any],
            ctx: Any,
        ) -> PreparedRowPublication:
            if any("unresolved" in str(value) for value in row_values.values()):
                return PreparedRowPublication(trace_data={}, cells={})
            return await original_execute(recipe, row_values, spec, ctx)

        monkeypatch.setattr(_TypedMapRowsProgram, "execute", omit_declared_output)
        staged_flushes: list[tuple[int, int]] = []

        def observe_staged_flush(
            store: RunResultStore,
            run_id: int,
            batch: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            original_write_results(store, run_id, batch, **kwargs)
            for result in batch:
                row_id = int(result["row_id"])
                column_id = int(result["column_id"])
                binding = generation_store.get_binding(run_id, column_id)
                assert binding is not None
                assert binding.write_mode == "replace_scope"
                assert binding.state == "staged"
                # Durable staged results do not become live heads piecemeal.
                assert (
                    generation_store.read_cell_heads(column_id, [row_id])[row_id]
                    == first_heads[row_id]
                )
                staged_flushes.append((row_id, column_id))

        monkeypatch.setattr(RunResultStore, "write_results", observe_staged_flush)
        replacement_plan = _typed_regex_plan(
            project,
            sheet_id,
            key="p3b-writer-replacement@sha256:v1",
            row_ids=row_ids[:3],
            replace_existing=True,
        )
        second_spec = replacement_plan.spec_dict()
        second = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    authority=UnroutedOnlyAuthority(project),
                    allow_action_lifecycle_only_recipes=True,
                ),
                second_spec,
                program=replacement_plan.program,
            )
        )

        assert second.done
        assert second.failed == 1
        assert {row_id for row_id, _column_id in staged_flushes} == set(row_ids[:3])
        second_results = project.db.execute(
            "SELECT row_id,publication_effect FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id",
            (second.run_id, output_id),
        ).fetchall()
        assert {int(row["row_id"]) for row in second_results} == set(row_ids[:3])
        assert [str(row["publication_effect"]) for row in second_results] == [
            "publish_value",
            "publish_null",
            "publish_error",
        ]

        second_binding = generation_store.get_binding(second.run_id, output_id)
        assert second_binding is not None
        assert second_binding.write_mode == "replace_scope"
        assert second_binding.state == "sealed"
        assert second_binding.terminal_disposition == "completed"
        assert second_binding.expected_base_run_id == first.run_id
        assert second_binding.compatibility_key == first_binding.compatibility_key

        heads = generation_store.read_cell_heads(output_id, row_ids)
        assert heads[row_ids[0]].run_id == second.run_id
        assert heads[row_ids[0]].publication_effect == "publish_value"
        assert heads[row_ids[0]].value == "909"
        assert heads[row_ids[1]].run_id == second.run_id
        assert heads[row_ids[1]].publication_effect == "publish_null"
        assert heads[row_ids[1]].value is None
        assert heads[row_ids[1]].error is None
        assert heads[row_ids[2]].run_id == second.run_id
        assert heads[row_ids[2]].publication_effect == "publish_error"
        assert heads[row_ids[2]].value is None
        assert heads[row_ids[2]].error_code == "model_error"
        assert "does not match declared outputs" in str(heads[row_ids[2]].error)
        assert heads[row_ids[3]] == first_heads[row_ids[3]]
        assert generation_store.origin_run_ids(output_id, row_ids) == [
            second.run_id,
            first.run_id,
        ]
    finally:
        project.close()

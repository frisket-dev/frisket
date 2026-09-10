from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import frisket.engine.runner.validation as validation_module
from frisket.ai.llm import ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    list_cell_evidence,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import OpContext, Recipe
from frisket.server.services.sheet_grid import _sheet_data_payload
from runner_test_helpers import run_with_output_claim
from tests.action_test_helpers import run_typed_map_request, typed_map_request


def _deterministic_extract_spec(
    sheet_id: int,
    *,
    key: str,
    pattern: str = r"\d+",
    row_ids: list[int] | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    if "a-z" in pattern:
        value_expression = "''.join(ch for ch in row['note'] if ch.isalpha()) or None"
    else:
        value_expression = "''.join(ch for ch in row['note'] if ch.isdigit()) or None"
    params: dict[str, Any] = {
        "input_columns": ["note"],
        "code": f"result = {{'digits': {value_expression}}}",
        "return_schema": {
            "type": "object",
            "properties": {"digits": {"type": ["string", "null"]}},
            "required": ["digits"],
        },
        "output_routes": [
            {
                "name": "digits",
                "path": "$.digits",
                "target": {"kind": "column", "type": "text"},
            }
        ],
    }
    spec = typed_map_request(
        "map.python",
        sheet_id,
        params=params,
        output_names={"digits": "digits"},
        idempotency_key=key,
        row_ids=row_ids,
    )
    if overwrite_existing:
        spec["replace_existing"] = True
    return spec


def _run_deterministic_extract(project: Project, spec: dict[str, Any]):
    return run_typed_map_request(
        project,
        spec,
        project_id="mixed-origin",
    )


def _backfill_action(
    sheet_id: int,
    *,
    row_ids: list[int],
    key: str = "mixed-origin-backfill@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {"column": "digits"},
        "output_names": {},
        "idempotency_key": key,
    }


def _run_op_id(project: Project, run_id: int) -> int:
    row = project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row is not None
    return int(row["op_id"])


def _assert_run_result_ref(
    ref: dict[str, Any],
    *,
    run_id: int,
    op_id: int,
    row_id: int,
    column_id: int,
) -> None:
    assert ref == {
        "kind": "run_result",
        "op_id": op_id,
        "row_id": row_id,
        "column_id": column_id,
        "run_id": run_id,
    }


def test_regex_fresh_column_stays_progressive_then_subset_reuses_exact_heads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P3b existing-output vertical must not terminalize fresh outputs.

    This is the lightweight real-producer half of the proof.  It deliberately
    observes the live resolver immediately after each ordinary result flush,
    while the owning run is still ``running``.  The action has one top-level
    canonical scope; there is no second ``params.row_ids``/``params.sheet_id``
    authority for the producer to reinterpret.
    """

    project = Project.create(tmp_path / "regex-fresh-progress.frisket")
    try:
        sheet_id = project.add_sheet("Notes")
        note_column = project.add_column(sheet_id, "note", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"note": "one 101"}, {"note": "two 202"}, {"note": "three 303"}],
            {"note": note_column},
        )

        observed_flushes: list[tuple[int, tuple[int, ...]]] = []
        original_write_results = RunResultStore.write_results

        def observe_fresh_flush(
            store: RunResultStore,
            run_id: int,
            batch: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            original_write_results(store, run_id, batch, **kwargs)
            run = project.db.execute(
                "SELECT action_kind, status, op_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or str(run["action_kind"]) != "map.python":
                return
            assert run["status"] == "running"
            for result in batch:
                row_id = int(result["row_id"])
                column_id = int(result["column_id"])
                values, refs = project.get_values_with_refs(
                    sheet_id, column_id, row_ids=[row_id]
                )
                assert values[row_id] == result["value"]
                _assert_run_result_ref(
                    refs[row_id],
                    run_id=run_id,
                    op_id=int(run["op_id"]),
                    row_id=row_id,
                    column_id=column_id,
                )
            observed_flushes.append(
                (run_id, tuple(sorted(int(item["row_id"]) for item in batch)))
            )

        monkeypatch.setattr(RunResultStore, "write_results", observe_fresh_flush)
        result = _run_deterministic_extract(
            project,
            _deterministic_extract_spec(
                sheet_id,
                key="p3b-regex-fresh@sha256:v1",
            ),
        )

        assert result.status == "completed", result.errors
        assert observed_flushes
        assert {rid for _run_id, batch in observed_flushes for rid in batch} == set(
            row_ids
        )
        output = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='digits'", (sheet_id,)
        ).fetchone()
        assert output is not None
        assert project.get_values(sheet_id, int(output["id"])) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
            row_ids[2]: "303",
        }

        output_id = int(output["id"])
        first_op_id = _run_op_id(project, result.run_id)
        before = _column_snapshot(
            project,
            sheet_id=sheet_id,
            column_id=output_id,
            row_ids=row_ids,
        )
        for row_id in row_ids:
            _assert_run_result_ref(
                before[1][row_id],
                run_id=result.run_id,
                op_id=first_op_id,
                row_id=row_id,
                column_id=output_id,
            )

        # Existing-output replacement keeps its characterized terminal
        # visibility, so the fresh-flush observer above deliberately applies
        # only to the first run.
        monkeypatch.setattr(RunResultStore, "write_results", original_write_results)
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
            ],
            label="change exact regex subset",
        )
        subset = _run_deterministic_extract(
            project,
            _deterministic_extract_spec(
                sheet_id,
                key="p3b-regex-subset@sha256:v1",
                row_ids=row_ids[:2],
                overwrite_existing=True,
            ),
        )

        assert subset.status == "completed", subset.errors
        assert (
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='digits'",
                (sheet_id,),
            ).fetchone()["id"]
            == output_id
        )
        subset_result_rows = {
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT row_id FROM results WHERE run_id=? AND column_id=?",
                (subset.run_id, output_id),
            ).fetchall()
        }
        assert subset_result_rows == set(row_ids[:2])

        after = _column_snapshot(
            project,
            sheet_id=sheet_id,
            column_id=output_id,
            row_ids=row_ids,
        )
        assert after[0] == {
            row_ids[0]: "909",
            row_ids[1]: None,
            row_ids[2]: "303",
        }
        subset_op_id = _run_op_id(project, subset.run_id)
        for row_id in row_ids[:2]:
            _assert_run_result_ref(
                after[1][row_id],
                run_id=subset.run_id,
                op_id=subset_op_id,
                row_id=row_id,
                column_id=output_id,
            )
        _assert_run_result_ref(
            after[1][row_ids[2]],
            run_id=result.run_id,
            op_id=first_op_id,
            row_id=row_ids[2],
            column_id=output_id,
        )
        assert _active_origin_run_ids(
            project,
            sheet_id=sheet_id,
            column_id=output_id,
            row_ids=row_ids,
        ) == {result.run_id, subset.run_id}

        assert project.undo() == subset_op_id
        assert (
            _column_snapshot(
                project,
                sheet_id=sheet_id,
                column_id=output_id,
                row_ids=row_ids,
            )
            == before
        )
    finally:
        project.close()


def test_regex_backfill_reconstructs_action_from_requested_cell_head(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "regex-backfill-exact-head.frisket")
    try:
        sheet_id = project.add_sheet("Notes")
        note_column = project.add_column(sheet_id, "note", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"note": "101"}, {"note": "202"}],
            {"note": note_column},
        )

        first = _run_deterministic_extract(
            project,
            _deterministic_extract_spec(
                sheet_id,
                key="exact-head-digits@sha256:v1",
            ),
        )
        assert first.status == "completed", first.errors

        project.apply_edits(
            [
                {
                    "row_id": row_ids[0],
                    "column_id": note_column,
                    "value": "letters",
                }
            ],
            label="change subset to letters",
        )
        second = _run_deterministic_extract(
            project,
            _deterministic_extract_spec(
                sheet_id,
                key="exact-head-letters@sha256:v1",
                pattern=r"[a-z]+",
                row_ids=[row_ids[0]],
                overwrite_existing=True,
            ),
        )
        assert second.status == "completed", second.errors

        output = next(
            column for column in project.columns(sheet_id) if column["name"] == "digits"
        )
        output_id = int(output["id"])
        assert project.get_values(sheet_id, output_id) == {
            row_ids[0]: "letters",
            row_ids[1]: "202",
        }

        backfill = run_action_spec(
            project,
            _backfill_action(sheet_id, row_ids=[row_ids[1]]),
            project_id="exact-head-backfill",
        )
        assert backfill.status == "completed", backfill.errors
        assert backfill.run_id not in {first.run_id, second.run_id}
        assert project.get_values(sheet_id, output_id) == {
            row_ids[0]: "letters",
            row_ids[1]: "202",
        }
        heads = ResultGenerationStore(project).read_cell_heads(output_id, row_ids)
        assert heads[row_ids[0]].run_id == second.run_id
        assert heads[row_ids[1]].run_id == backfill.run_id
        params = json.loads(
            project.db.execute(
                "SELECT params FROM runs WHERE id=?", (backfill.run_id,)
            ).fetchone()["params"]
        )
        assert params["action_kind"] == "map.python"
        assert "isdigit" in params["params"]["code"]

        before_values, before_refs = project.get_values_with_refs(
            sheet_id, output_id, row_ids=row_ids
        )
        run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        refused = run_action_spec(
            project,
            _backfill_action(
                sheet_id,
                row_ids=row_ids,
                key="mixed-origin-backfill-refused@sha256:v1",
            ),
            project_id="exact-head-backfill",
        )
        assert refused.status == "failed"
        assert [error.code for error in refused.errors] == [
            "mixed_origin_column_unsupported"
        ]
        assert (
            int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            == run_count
        )
        assert project.get_values_with_refs(sheet_id, output_id, row_ids=row_ids) == (
            before_values,
            before_refs,
        )

        new_row = project.add_rows(sheet_id, [{"note": "303"}], {"note": note_column})[
            0
        ]
        run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        missing_source = run_action_spec(
            project,
            _backfill_action(
                sheet_id,
                row_ids=[new_row],
                key="missing-head-backfill-refused@sha256:v1",
            ),
            project_id="exact-head-backfill",
        )
        assert missing_source.status == "failed"
        assert [error.code for error in missing_source.errors] == [
            "backfill_source_unavailable"
        ]
        assert (
            int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            == run_count
        )
        assert (
            ResultGenerationStore(project).read_cell_heads(output_id, [new_row]) == {}
        )
    finally:
        project.close()


@dataclass
class _MixedKindRecipe(Recipe):
    """Small deterministic fixture for the shared result publisher.

    It is not a product action and therefore does not introduce another
    authoring/scope contract.  The real regex action above owns that proof;
    this fixture only makes the already-shared MapRunner write boundary emit
    several physical value kinds plus an explicit null and a row error.
    """

    consumes_resolution = False
    cost_class = "free"

    name: str = "test.mixed_kind_publication"
    llm: bool = False

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["seed"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [
            {
                "name": "generated_text",
                "column_type": "text",
                "schema": {"type": "string"},
            },
            {
                "name": "generated_number",
                "column_type": "number",
                "schema": {"type": "number"},
            },
            {
                "name": "generated_boolean",
                "column_type": "boolean",
                "schema": {"type": "boolean"},
            },
            {
                "name": "generated_json",
                "column_type": "json",
                "schema": {"type": "object"},
            },
            {
                "name": "generated_nullable",
                "column_type": "text",
                "schema": {"type": ["string", "null"]},
            },
            {
                "name": "generated_image",
                "column_type": "image",
                "schema": {"type": "object"},
            },
        ]

    async def execute(
        self,
        row_values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[str, Any]:
        row_id = int(ctx.extras["row_id"])
        if row_id == spec.get("error_row_id"):
            raise RuntimeError("fixture row published a deliberate error")
        seed = int(row_values["seed"])
        phase = str(spec["phase"])
        return {
            "generated_text": f"{phase}:{seed}",
            "generated_number": seed + (100 if phase == "second" else 0),
            "generated_boolean": (seed % 2) == (0 if phase == "first" else 1),
            "generated_json": {"phase": phase, "seed": seed, "items": [seed]},
            "generated_nullable": (
                None if row_id == spec.get("null_row_id") else f"present:{phase}:{seed}"
            ),
            "generated_image": {
                "blob": str(spec["blob_hash"]),
                "mime": "image/png",
            },
        }


def _install_mixed_kind_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> _MixedKindRecipe:
    recipe = _MixedKindRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: recipe if name == recipe.name else original_get_recipe(name),
    )
    return recipe


def _run_fixture(
    project: Project,
    recipe: _MixedKindRecipe,
    *,
    sheet_id: int,
    phase: str,
    blob_hash: str,
    row_ids: list[int] | None = None,
    null_row_id: int | None = None,
    error_row_id: int | None = None,
) -> Any:
    spec: dict[str, Any] = {
        "action_kind": recipe.name,
        "sheet_id": sheet_id,
        "input_columns": ["seed"],
        "phase": phase,
        "blob_hash": blob_hash,
    }
    if row_ids is not None:
        spec["row_ids"] = row_ids
        spec["overwrite"] = True
    if null_row_id is not None:
        spec["null_row_id"] = null_row_id
    if error_row_id is not None:
        spec["error_row_id"] = error_row_id
    return asyncio.run(
        run_with_output_claim(
            MapRunner(
                project,
                ModelRouter(keys={}),
                authority=UnroutedOnlyAuthority(project),
            ),
            spec,
        )
    )


def _output_columns(project: Project, sheet_id: int) -> dict[str, int]:
    return {
        str(row["name"]): int(row["id"])
        for row in project.db.execute(
            "SELECT id, name FROM columns WHERE sheet_id=? AND ai_generated=1 "
            "ORDER BY position, id",
            (sheet_id,),
        ).fetchall()
    }


def _column_snapshot(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
) -> tuple[dict[int, Any], dict[int, dict[str, Any]]]:
    values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=row_ids)
    return dict(values), dict(refs)


def _active_origin_run_ids(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
) -> set[int]:
    """Derive mixed/single state from exact active refs at query time."""

    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=row_ids)
    return {
        int(ref["run_id"])
        for ref in refs.values()
        if ref.get("kind") == "run_result" and ref.get("run_id") is not None
    }


def test_subset_publication_keeps_exact_heads_and_undo_restores_evidence_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One vertical proof for value/null/error, mixed state, and rare undo.

    The second run targets three of four rows in an existing compatible output
    family.  Every actual attempt becomes that cell's newest head, including
    the explicit null and the error.  The fourth row remains attached to its
    first exact result; no result is copied into the second run.  Undo then
    restores the first refs, values, evidence, and blob after GC.
    """

    recipe = _install_mixed_kind_recipe(monkeypatch)
    project = Project.create(tmp_path / "mixed-origin-vertical.frisket")
    try:
        sheet_id = project.add_sheet("Cases")
        seed_column = project.add_column(sheet_id, "seed", type="integer")
        row_ids = project.add_rows(
            sheet_id,
            [{"seed": 1}, {"seed": 2}, {"seed": 3}, {"seed": 4}],
            {"seed": seed_column},
        )
        value_row, null_row, error_row, untargeted_row = row_ids
        target_rows = [value_row, null_row, error_row]

        first_blob_bytes = b"first generation image bytes"
        first_blob = project.add_blob(
            first_blob_bytes,
            filename="first.png",
            mime="image/png",
            metadata={"generation": "first"},
        )
        second_blob = project.add_blob(
            b"second generation image bytes",
            filename="second.png",
            mime="image/png",
            metadata={"generation": "second"},
        )
        evidence_blob_bytes = b"independent first-generation evidence bytes"
        evidence_blob = project.add_blob(
            evidence_blob_bytes,
            filename="evidence.png",
            mime="image/png",
            metadata={"role": "evidence"},
        )

        first = _run_fixture(
            project,
            recipe,
            sheet_id=sheet_id,
            phase="first",
            blob_hash=first_blob,
        )
        assert first.done and first.failed == 0
        first_op_id = _run_op_id(project, first.run_id)
        columns = _output_columns(project, sheet_id)
        assert set(columns) == {
            "generated_text",
            "generated_number",
            "generated_boolean",
            "generated_json",
            "generated_nullable",
            "generated_image",
        }
        before = {
            name: _column_snapshot(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
            for name, column_id in columns.items()
        }

        prior_error_ref = before["generated_text"][1][error_row]
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="image/png",
            blob_hash=evidence_blob,
            filename="evidence.png",
        )
        span = record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="text",
            quote="first generation evidence",
        )
        evidence_link = record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=prior_error_ref,
            spans=[{"span_id": int(span["id"])}],
            sheet_id=sheet_id,
            row_id=error_row,
            column_id=columns["generated_text"],
            run_id=first.run_id,
            op_id=first_op_id,
        )

        # Existing-output replacement remains staged: durable result rows do
        # not move a head until terminal publication seals the coherent batch.
        original_write_results = RunResultStore.write_results
        staged_writes: set[tuple[int, int]] = set()
        names_by_column = {column_id: name for name, column_id in columns.items()}

        def observe_staged_replacement(
            store: RunResultStore,
            run_id: int,
            batch: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            original_write_results(store, run_id, batch, **kwargs)
            for result in batch:
                row_id = int(result["row_id"])
                column_id = int(result["column_id"])
                name = names_by_column[column_id]
                staged = _column_snapshot(
                    project,
                    sheet_id=sheet_id,
                    column_id=column_id,
                    row_ids=[row_id],
                )
                assert staged[0][row_id] == before[name][0][row_id]
                assert staged[1][row_id] == before[name][1][row_id]
                staged_writes.add((row_id, column_id))

        monkeypatch.setattr(RunResultStore, "write_results", observe_staged_replacement)
        second = _run_fixture(
            project,
            recipe,
            sheet_id=sheet_id,
            phase="second",
            blob_hash=second_blob,
            row_ids=target_rows,
            null_row_id=null_row,
            error_row_id=error_row,
        )
        monkeypatch.setattr(RunResultStore, "write_results", original_write_results)
        assert second.done and second.failed == 1
        second_op_id = _run_op_id(project, second.run_id)
        assert staged_writes == {
            (row_id, column_id)
            for row_id in target_rows
            for column_id in columns.values()
        }

        second_results = {
            (int(row["row_id"]), int(row["column_id"]))
            for row in project.db.execute(
                "SELECT row_id, column_id FROM results WHERE run_id=?",
                (second.run_id,),
            ).fetchall()
        }
        assert second_results == {
            (row_id, column_id)
            for row_id in target_rows
            for column_id in columns.values()
        }
        assert all(row_id != untargeted_row for row_id, _column_id in second_results)

        after = {
            name: _column_snapshot(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
            for name, column_id in columns.items()
        }

        for name, column_id in columns.items():
            assert after[name][0][untargeted_row] == before[name][0][untargeted_row]
            _assert_run_result_ref(
                after[name][1][untargeted_row],
                run_id=first.run_id,
                op_id=first_op_id,
                row_id=untargeted_row,
                column_id=column_id,
            )
            assert _active_origin_run_ids(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            ) == {first.run_id, second.run_id}

        assert after["generated_text"][0][value_row] == "second:1"
        _assert_run_result_ref(
            after["generated_text"][1][value_row],
            run_id=second.run_id,
            op_id=second_op_id,
            row_id=value_row,
            column_id=columns["generated_text"],
        )
        assert after["generated_number"][0][value_row] == 101
        assert after["generated_boolean"][0][value_row] is True
        assert after["generated_json"][0][value_row] == {
            "phase": "second",
            "seed": 1,
            "items": [1],
        }

        assert after["generated_nullable"][0][null_row] is None
        _assert_run_result_ref(
            after["generated_nullable"][1][null_row],
            run_id=second.run_id,
            op_id=second_op_id,
            row_id=null_row,
            column_id=columns["generated_nullable"],
        )

        assert after["generated_text"][0][error_row] is None
        _assert_run_result_ref(
            after["generated_text"][1][error_row],
            run_id=second.run_id,
            op_id=second_op_id,
            row_id=error_row,
            column_id=columns["generated_text"],
        )

        grid = _sheet_data_payload(
            project,
            sheet_id,
            project.columns(sheet_id),
            row_ids,
            total=len(row_ids),
        )
        error_grid_row = next(row for row in grid["rows"] if row["id"] == error_row)
        text_key = str(columns["generated_text"])
        assert error_grid_row["cells"][text_key] is None
        assert error_grid_row["meta"][text_key]["state"] == "error"
        assert "deliberate error" in error_grid_row["meta"][text_key]["error"]
        _assert_run_result_ref(
            error_grid_row["meta"][text_key]["current_value_ref"],
            run_id=second.run_id,
            op_id=second_op_id,
            row_id=error_row,
            column_id=columns["generated_text"],
        )

        while_error_active = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=error_row,
            column_id=columns["generated_text"],
        )
        assert while_error_active["links"] == []
        assert (
            while_error_active["current_value_ref"]
            == after["generated_text"][1][error_row]
        )

        project.gc_blobs()
        assert project.read_blob(first_blob) == first_blob_bytes
        assert project.read_blob(evidence_blob) == evidence_blob_bytes
        assert project.undo() == second_op_id

        restored = {
            name: _column_snapshot(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
            for name, column_id in columns.items()
        }
        assert restored == before
        restored_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=error_row,
            column_id=columns["generated_text"],
        )
        assert restored_evidence["current_value_ref"] == prior_error_ref
        assert [link["stable_id"] for link in restored_evidence["links"]] == [
            evidence_link["stable_id"]
        ]
        assert project.read_blob(first_blob) == first_blob_bytes
        assert project.read_blob(evidence_blob) == evidence_blob_bytes
        assert MediaBlobStore(project).metadata(first_blob) == {"generation": "first"}
    finally:
        project.close()

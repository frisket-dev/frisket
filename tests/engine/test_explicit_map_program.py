from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import MapRunner, validation
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import OpContext, Recipe


@dataclass
class _ExplicitProgram(Recipe):
    consumes_resolution = False
    cost_class = "free"

    name: str = "test.explicit_program"
    llm: bool = False
    auto_verify: bool = True

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": spec["output_name"],
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]

    async def execute(
        self,
        row_values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[str, Any]:
        del ctx
        return {spec["output_name"]: str(row_values["source"]).upper()}


@pytest.fixture
def project(tmp_path: Path):
    value = Project.create(tmp_path / "explicit-program.frisket")
    yield value
    value.close()


def _seed(project: Project) -> tuple[dict[str, Any], list[int]]:
    sheet_id = project.add_sheet("data")
    source_id = project.add_column(sheet_id, "source")
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "one"}, {"source": "two"}],
        {"source": source_id},
    )
    return (
        {
            "action_kind": "test.explicit_program",
            "sheet_id": sheet_id,
            "input_columns": ["source"],
            "output_name": "upper",
            "row_ids": row_ids,
        },
        row_ids,
    )


def _runner(project: Project, **kwargs: Any) -> MapRunner:
    return MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
        **kwargs,
    )


def _forbid_registry_lookup(*_args: Any, **_kwargs: Any) -> Recipe:
    raise AssertionError(
        "an explicit program must not be resolved through the registry"
    )


def test_explicit_program_drives_estimate_validation_and_preview(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, row_ids = _seed(project)
    program = _ExplicitProgram()
    runner = _runner(project)
    monkeypatch.setattr(validation, "recipe_for_spec", _forbid_registry_lookup)

    assert runner.estimate(spec, program=program) == {
        "rows": 2,
        "cost": 0.0,
        "cost_source": "free_local",
    }
    assert runner.preview_precheck(spec, program=program) == 2
    preview = asyncio.run(runner.preview(spec, program=program))
    assert preview.row_ids == row_ids
    assert [preview.values[row_id]["upper"]["value"] for row_id in row_ids] == [
        "ONE",
        "TWO",
    ]


def test_explicit_program_drives_prepare_and_run_without_registry(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _seed(project)
    program = _ExplicitProgram()
    claim_token = f"output-claim:test:{uuid.uuid4()}"
    claim_store = OutputColumnClaimStore(project)
    claims, conflict = claim_store.acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=["upper"],
        action_kind=program.name,
        claim_token=claim_token,
        lease_seconds=60,
    )
    assert conflict is None
    assert len(claims) == 1

    bound = False

    def bind_prepared_run(progress: Any) -> None:
        nonlocal bound
        if not bound:
            assert (
                claim_store.bind_to_run(
                    claim_token=claim_token,
                    run_id=progress.run_id,
                    expected_output_names=["upper"],
                )
                == 1
            )
            bound = True

    runner = _runner(project, on_progress=bind_prepared_run)
    monkeypatch.setattr(validation, "recipe_for_spec", _forbid_registry_lookup)

    progress = asyncio.run(runner.run(spec, program=program, claim_token=claim_token))

    assert bound
    assert progress.done
    assert progress.completed == 2
    assert progress.failed == 0

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ops.base import OpContext, Recipe
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import MapRunner, validation
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.engine.store import Project
from runner_test_helpers import run_with_output_claim


class _ConcurrencyProbeRecipe(Recipe):
    RECIPE_NAME = "row_concurrency_probe"
    # Required declaration (frisket.ops.base.Recipe.consumes_resolution): a
    # test recipe that reaches the runner declares like any other.
    consumes_resolution = False
    cost_class = "free"  # required declaration (Recipe)

    def __init__(self) -> None:
        super().__init__(name=self.RECIPE_NAME, llm=False)
        self.active = 0
        self.peak = 0

    def reset(self) -> None:
        self.active = 0
        self.peak = 0

    def max_row_concurrency(self, spec: dict) -> int | None:
        return spec.get("row_concurrency_cap")

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.get("output_name", "out"),
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]

    async def execute(
        self, row_values: dict[str, Any], spec: dict, ctx: OpContext
    ) -> dict[str, Any]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return {spec.get("output_name", "out"): row_values["text"]}
        finally:
            self.active -= 1


@pytest.fixture
def concurrency_probe() -> _ConcurrencyProbeRecipe:
    return _ConcurrencyProbeRecipe()


def _project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "row-concurrency.frisket", name="probe")
    sheet_id = project.add_sheet("Rows")
    text_col = project.add_column(sheet_id, "text", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"text": f"row-{index}"} for index in range(8)],
        {"text": text_col},
    )
    return project, sheet_id, row_ids


def _spec(sheet_id: int, **extras: Any) -> dict[str, Any]:
    return {
        "action_kind": "test.row_concurrency_probe",
        "sheet_id": sheet_id,
        **extras,
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"engine": "parakeet-tdt"}, 1),
        ({}, None),
        ({"engine": "faster_whisper"}, None),
        ({"engine": "moss"}, None),
        ({"engine": "parakeet-tdt", "diarize": True}, None),
        ({"engine": "openai/whisper-1"}, None),
    ],
)
def test_transcribe_caps_only_resolved_local_parakeet(
    tmp_path, params, expected
) -> None:
    assert Recipe().max_row_concurrency({}) is None
    project = Project.create(tmp_path / "transcribe-concurrency.frisket")
    try:
        sheet = project.add_sheet("Audio")
        project.add_column(sheet, "audio", type="audio")
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("media.transcribe"),
            ActionRequest(
                action_id="media.transcribe",
                scope={"kind": "sheet_rows", "sheet_id": sheet},
                params={"source": "audio", **params},
                idempotency_key="concurrency",
            ),
        )
        plan = build_typed_map_rows_plan(project, bound)
        assert plan.program.max_row_concurrency(plan.spec_dict()) == expected
    finally:
        project.close()


def test_durable_run_applies_recipe_cap_and_preserves_uncapped_concurrency(
    tmp_path: Path,
    concurrency_probe: _ConcurrencyProbeRecipe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validation,
        "recipe_for_spec",
        lambda _spec: concurrency_probe,
    )
    project, sheet_id, _row_ids = _project(tmp_path)
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        concurrency=4,
        authority=UnroutedOnlyAuthority(project),
    )
    try:
        capped = asyncio.run(
            run_with_output_claim(
                runner,
                _spec(sheet_id, row_concurrency_cap=1, output_name="capped"),
                confirmed=True,
            )
        )
        assert capped.failed == 0
        assert concurrency_probe.peak == 1

        concurrency_probe.reset()
        uncapped = asyncio.run(
            run_with_output_claim(
                runner,
                _spec(sheet_id, output_name="uncapped"),
                confirmed=True,
            )
        )
        assert uncapped.failed == 0
        assert concurrency_probe.peak == 4
    finally:
        project.close()


def test_preview_applies_recipe_cap_and_preserves_uncapped_concurrency(
    tmp_path: Path,
    concurrency_probe: _ConcurrencyProbeRecipe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validation,
        "recipe_for_spec",
        lambda _spec: concurrency_probe,
    )
    project, sheet_id, row_ids = _project(tmp_path)
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        concurrency=4,
        authority=UnroutedOnlyAuthority(project),
    )
    try:
        capped = asyncio.run(
            runner.preview(_spec(sheet_id, row_ids=row_ids, row_concurrency_cap=1))
        )
        assert set(capped.values) == set(row_ids)
        assert concurrency_probe.peak == 1

        concurrency_probe.reset()
        uncapped = asyncio.run(runner.preview(_spec(sheet_id, row_ids=row_ids)))
        assert set(uncapped.values) == set(row_ids)
        assert concurrency_probe.peak == 4
    finally:
        project.close()

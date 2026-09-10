"""Map runner chaos and replay tests.

Determinism trick: cache lookups happen BEFORE chaos interception, so tests
pre-populate the cache for chosen rows and chaos-fault everything else —
partial failure, resume, and throttling become exact, network-free tests.
"""

import asyncio
import json
from dataclasses import dataclass

import pytest

from frisket.ai.llm import (
    ChaosConfig,
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.pricing import cost_of_with_source
from frisket.ops.base import OpContext, Recipe, RenderedCall
from frisket.ops.builtin import prompt_hash_of
from frisket.engine.runner import CostGate, MapRunner, validation
from frisket.engine.runner.row_execution import AdaptiveThrottle, execute_row, llm_row
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from runner_test_helpers import run_with_output_claim as _run_with_output_claim
from tests.typed_model_fixtures import model_plan
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

MODEL = "anthropic/claude-haiku-4-5"


def test_rendered_call_uses_shared_default_and_preserves_override() -> None:
    assert RenderedCall(messages=[], schema=None).max_tokens == (
        DEFAULT_MAX_OUTPUT_TOKENS
    )
    assert RenderedCall(messages=[], schema=None, max_tokens=512).max_tokens == 512


def test_confirmation_echo_is_not_part_of_durable_prompt_identity() -> None:
    spec = classify_spec(1, context="Same provider prompt")
    recipe = model_plan(spec).program
    before = prompt_hash_of(recipe, spec)
    retry = {
        **spec,
        "confirmed": True,
        "consented_promise_set_hash": "scope-and-quote-token",
    }
    assert prompt_hash_of(recipe, retry) == before


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def classify_spec(sheet_id, context="Test rows."):
    return _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "map.classify",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "idempotency_key": "typed-runner-fixture",
                "params": {
                    "engine": "llm",
                    "model": MODEL,
                    "source": ["text"],
                    "context": context,
                    "fields": [
                        {
                            "name": "relevance",
                            "type": "score",
                            "description": "0-10 relevance to topic",
                        }
                    ],
                },
            }
        )
    ).spec_dict()


async def run_with_output_claim(runner, spec, **kwargs):
    """Give the runner its explicit typed program and real output claims."""
    runner.allow_action_lifecycle_only_recipes = True
    return await _run_with_output_claim(runner, spec, program=_program(spec), **kwargs)


async def run_with_exact_confirmation(runner, spec, **kwargs):
    try:
        return await run_with_output_claim(runner, spec, **kwargs)
    except CostGate as gate:
        assert gate.promise_set_hash
        return await run_with_output_claim(
            runner,
            {**spec, "consented_promise_set_hash": gate.promise_set_hash},
            confirmed=True,
            **kwargs,
        )


def _program(spec):
    return (
        model_plan(spec).program
        if spec["action_kind"] == "map.classify"
        else validation.recipe_for_spec(spec)
    )


def seed_sheet(p: Project, texts: list[str]) -> tuple[int, dict]:
    sheet = p.add_sheet("data")
    cols = {"text": p.add_column(sheet, "text")}
    p.add_rows(sheet, [{"text": t} for t in texts], cols)
    return sheet, cols


def prime_cache(
    cache: ResponseCache,
    p: Project,
    sheet: int,
    spec: dict,
    row_texts: list[str],
    score: int = 7,
) -> None:
    """Pre-compute the EXACT requests the runner will make for these rows and
    cache real-shaped responses for them."""
    recipe = _program(spec)
    for text in row_texts:
        call = recipe.render({"text": text}, spec)
        req = LLMRequest(
            model=MODEL,
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        cache.put(
            request_key(req, recipe.version),
            LLMResponse(
                content=None,
                data={"relevance": score},
                tokens_in=50,
                tokens_out=10,
                cost=0.0001,
                model=MODEL,
            ),
        )


def confirm_remote_spec(
    runner: MapRunner,
    spec: dict,
) -> dict:
    """Exercise the real 402 handshake and return its exact confirmed retry."""
    with pytest.raises(CostGate) as gate:
        runner.prepare_run(
            spec,
            program=model_plan(spec).program,
            confirmed=False,
        )
    assert gate.value.promise_set_hash
    return {
        **spec,
        "consented_promise_set_hash": gate.value.promise_set_hash,
    }


def _prepare_claimed_runner_run(
    runner: MapRunner,
    spec: dict,
    *,
    confirmed: bool,
):
    recipe = _program(spec)
    output_names = [str(field["name"]) for field in recipe.output_fields(spec)]
    claim_token = f"output-claim:test:{recipe.name}:{id(runner)}"
    claims, conflict = OutputColumnClaimStore(runner.project).acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=output_names,
        action_kind=recipe.name,
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == len(output_names)
    prepared = runner._prepare(  # noqa: SLF001 - runner contract probe
        spec,
        program=recipe,
        confirmed=confirmed,
        resume_run_id=None,
    )
    assert OutputColumnClaimStore(runner.project).bind_to_run(
        claim_token=claim_token,
        run_id=prepared.run_id,
        expected_output_names=output_names,
    ) == len(output_names)
    return prepared, claim_token


async def _wait_for_calls_or_task_exit(
    task: asyncio.Task,
    adapter,
    expected_calls: int,
    *,
    timeout: float = 2.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while adapter.calls < expected_calls:
        remaining = deadline - loop.time()
        assert remaining > 0, (
            f"runner did not start {expected_calls} calls within {timeout}s"
        )
        done, _pending = await asyncio.wait(
            {task},
            timeout=min(0.01, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task


class TestHappyPath:
    def test_full_run_from_cache(self, project, tmp_path):
        sheet, _ = seed_sheet(project, [f"text {i}" for i in range(10)])
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, [f"text {i}" for i in range(10)])
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )

        progress = asyncio.run(run_with_output_claim(runner, spec))
        assert progress.done and progress.completed == 10 and progress.failed == 0

        col = next(c for c in project.columns(sheet) if c["name"] == "relevance")
        vals = project.get_values(sheet, col["id"])
        assert all(v == 7 for v in vals.values())
        assert col["current_run_id"] == progress.run_id

    def test_output_column_marked_ai(self, project, tmp_path):
        sheet, _ = seed_sheet(project, ["a"])
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, ["a"])
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        asyncio.run(run_with_output_claim(runner, spec))
        col = next(c for c in project.columns(sheet) if c["name"] == "relevance")
        assert col["ai_generated"] == 1

    def test_subset_rerun_keeps_untouched_values_and_origins(self, project, tmp_path):
        sheet, _ = seed_sheet(project, ["alpha", "beta", "gamma"])
        row_ids = project.visible_row_ids(sheet)
        first_spec = classify_spec(sheet, context="Original classification")
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(
            cache,
            project,
            sheet,
            first_spec,
            ["alpha", "beta", "gamma"],
            score=7,
        )
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        first = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    router,
                    authority=UnroutedOnlyAuthority(project),
                    allow_action_lifecycle_only_recipes=True,
                ),
                first_spec,
            )
        )
        output = next(
            column for column in project.columns(sheet) if column["name"] == "relevance"
        )
        output_id = int(output["id"])
        before_values, before_refs = project.get_values_with_refs(
            sheet,
            output_id,
            row_ids=row_ids,
        )

        second_spec = {
            **first_spec,
            "params": {**first_spec["params"], "context": "Revised classification"},
            "row_ids": [row_ids[0]],
            "overwrite": True,
        }
        prime_cache(cache, project, sheet, second_spec, ["alpha"], score=9)
        second = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    router,
                    authority=UnroutedOnlyAuthority(project),
                    allow_action_lifecycle_only_recipes=True,
                ),
                second_spec,
            )
        )
        after_values, after_refs = project.get_values_with_refs(
            sheet,
            output_id,
            row_ids=row_ids,
        )

        assert after_values[row_ids[0]] == 9
        assert after_refs[row_ids[0]]["run_id"] == second.run_id
        for row_id in row_ids[1:]:
            assert after_values[row_id] == before_values[row_id] == 7
            assert after_refs[row_id] == before_refs[row_id]
            assert after_refs[row_id]["run_id"] == first.run_id
        heads = ResultGenerationStore(project).read_cell_heads(output_id, row_ids)
        assert {row_id: head.run_id for row_id, head in heads.items()} == {
            row_ids[0]: second.run_id,
            row_ids[1]: first.run_id,
            row_ids[2]: first.run_id,
        }
        assert project.get_column(output_id)["current_run_id"] == first.run_id

        final_spec = {
            **first_spec,
            "params": {**first_spec["params"], "context": "Final classification"},
            "overwrite": True,
        }
        prime_cache(
            cache,
            project,
            sheet,
            final_spec,
            ["alpha", "beta", "gamma"],
            score=4,
        )
        final = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    router,
                    authority=UnroutedOnlyAuthority(project),
                    allow_action_lifecycle_only_recipes=True,
                ),
                final_spec,
            )
        )
        final_heads = ResultGenerationStore(project).read_cell_heads(output_id, row_ids)
        assert {head.run_id for head in final_heads.values()} == {final.run_id}
        assert set(project.get_values(sheet, output_id).values()) == {4}
        assert project.get_column(output_id)["current_run_id"] == first.run_id


class TestPartialFailure:
    def test_failed_rows_recorded_run_continues(self, project, tmp_path, monkeypatch):
        """5 rows cached (succeed), 5 rows chaos-500 (fail after retries):
        the run completes with per-row errors, never crashes."""
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        texts = [f"row {i}" for i in range(10)]
        sheet, _ = seed_sheet(project, texts)
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, texts[:5])
        router = ModelRouter(
            keys={"anthropic": "k"},
            cache=cache,
            cache_mode="replay",
            chaos=ChaosConfig(seed=3, enabled=True, fail_rate=1.0),
            max_retries=1,
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        confirmed_spec = confirm_remote_spec(runner, spec)
        progress = asyncio.run(
            run_with_output_claim(runner, confirmed_spec, confirmed=True)
        )

        assert progress.done
        assert progress.completed == 10
        assert progress.failed == 5
        errors = project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=? AND error IS NOT NULL",
            (progress.run_id,),
        ).fetchone()[0]
        assert errors == 5
        run = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "completed"
        assert run["failed_rows"] == 5

    def test_fresh_subset_retry_replaces_only_failures(
        self, project, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        texts = [f"row {i}" for i in range(6)]
        sheet, _ = seed_sheet(project, texts)
        row_ids = project.visible_row_ids(sheet)
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, texts[:3])
        router = ModelRouter(
            keys={"anthropic": "k"},
            cache=cache,
            cache_mode="replay",
            chaos=ChaosConfig(seed=3, enabled=True, fail_rate=1.0),
            max_retries=0,
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        confirmed_spec = confirm_remote_spec(runner, spec)
        p1 = asyncio.run(run_with_output_claim(runner, confirmed_spec, confirmed=True))
        assert p1.failed == 3

        # now the missing rows become cacheable (provider "recovered")
        prime_cache(cache, project, sheet, spec, texts[3:])
        router2 = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner2 = MapRunner(
            project,
            router2,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        retry_spec = {
            **spec,
            "row_ids": row_ids[3:],
            "overwrite": True,
        }
        p2 = asyncio.run(run_with_output_claim(runner2, retry_spec))
        # Retry is a fresh subset generation: successful heads retain their
        # original attribution while failed rows move to the successor.
        assert p2.total == 3 and p2.failed == 0
        output = next(
            column for column in project.columns(sheet) if column["name"] == "relevance"
        )
        heads = ResultGenerationStore(project).read_cell_heads(
            int(output["id"]), row_ids
        )
        assert {heads[row_id].run_id for row_id in row_ids[:3]} == {p1.run_id}
        assert {heads[row_id].run_id for row_id in row_ids[3:]} == {p2.run_id}
        assert set(project.get_values(sheet, int(output["id"])).values()) == {7}


class TestCostGate:
    def test_gate_blocks_expensive_unconfirmed(self, project, tmp_path):
        sheet, _ = seed_sheet(project, ["long text " * 200] * 300)
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        router = ModelRouter(keys={"anthropic": "k"})
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        est = runner.estimate(spec, program=model_plan(spec).program)
        assert est["cost"] > 1.0
        with pytest.raises(CostGate):
            asyncio.run(runner.run(spec, program=model_plan(spec).program))

    def test_confirmed_retry_cannot_expand_beyond_the_402_row_scope(
        self, project, monkeypatch
    ):
        """The confirmation approves the rows and amount shown by the 402.

        An all-visible-rows request used to recompute its scope on the
        confirmed retry and let the boolean skip the gate unconditionally.
        Rows added while the modal was open therefore joined the paid run
        without ever appearing in the estimate the user approved.
        """
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        sheet, columns = seed_sheet(project, ["long text " * 200])
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )

        with pytest.raises(CostGate) as first_gate:
            runner.prepare_run(spec, program=model_plan(spec).program, confirmed=False)
        assert (
            first_gate.value.estimate
            == runner.estimate(spec, program=model_plan(spec).program)["cost"]
        )
        assert first_gate.value.promise_set_hash

        project.add_rows(
            sheet,
            [{"text": "long text " * 200} for _ in range(99)],
            columns,
        )
        assert (
            runner.estimate(spec, program=model_plan(spec).program)["cost"]
            > first_gate.value.estimate
        )

        # A stale confirmation must produce a fresh 402 for the expanded
        # scope, not create a 100-row run from a one-row approval.
        spec["consented_promise_set_hash"] = first_gate.value.promise_set_hash
        with pytest.raises(CostGate):
            runner.prepare_run(spec, program=model_plan(spec).program, confirmed=True)
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_confirmed_retry_without_hash_cannot_bypass_legacy_gate(
        self, project, monkeypatch
    ):
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        sheet, _ = seed_sheet(project, ["long text " * 200])
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )

        with pytest.raises(CostGate) as first_gate:
            runner.prepare_run(spec, program=model_plan(spec).program)
        with pytest.raises(CostGate) as omitted_echo:
            runner.prepare_run(spec, program=model_plan(spec).program, confirmed=True)

        assert omitted_echo.value.promise_set_hash == first_gate.value.promise_set_hash
        spec["consented_promise_set_hash"] = first_gate.value.promise_set_hash
        assert (
            runner.prepare_run(
                spec, program=model_plan(spec).program, confirmed=True
            ).run_id
            > 0
        )

    def test_preview_subset_under_gate(self, project, tmp_path, monkeypatch):
        """Preview (row_ids subset) keeps the same big sheet under the gate."""
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        texts = ["long text " * 200] * 300
        sheet, _ = seed_sheet(project, texts)
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        rows = [r["id"] for r in project.db.execute("SELECT id FROM rows LIMIT 3")]
        spec["row_ids"] = rows
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, [texts[0]] * 1)
        router = ModelRouter(keys={"anthropic": "k"}, cache=cache, cache_mode="replay")
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        est = runner.estimate(spec, program=model_plan(spec).program)
        assert est["cost"] < 1.0
        confirmed_spec = confirm_remote_spec(runner, spec)
        progress = asyncio.run(
            run_with_output_claim(runner, confirmed_spec, confirmed=True)
        )
        assert progress.total == 3


class TestResumeExtendCostGate:
    """run.backfill resumes an existing run but supplies explicit row_ids to
    EXTEND it to newly-visible rows. Those new rows were never estimated by the
    original run, so the resume path must honor COST_GATE_USD for exactly the
    rows it will actually run.
    Ordinary resumes (no explicit row_ids) stay ungated — the original run's
    gate already covered their scope.
    """

    def _prepared_run_id(self, project, spec, *, program: Recipe | None = None) -> int:
        # Create the run row + output columns + recorded scope WITHOUT executing
        # any rows, after the same 402 -> exact echo flow as a live launch.
        program = program or _program(spec)
        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        try:
            return runner.prepare_run(spec, program=program, confirmed=False).run_id
        except CostGate as gate:
            assert gate.promise_set_hash
            confirmed_spec = {
                **spec,
                "consented_promise_set_hash": gate.promise_set_hash,
            }
            return runner.prepare_run(
                confirmed_spec, program=program, confirmed=True
            ).run_id

    def test_extend_to_new_rows_gates_expensive_unconfirmed(self, project):
        sheet, cols = seed_sheet(project, ["seed a", "seed b"])
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        run_id = self._prepared_run_id(project, spec)

        new_ids = project.add_rows(
            sheet, [{"text": "long text " * 200} for _ in range(300)], cols
        )
        resume_spec = classify_spec(sheet)
        resume_spec["model"] = resume_spec["params"]["model"] = (
            "anthropic/claude-opus-4-8"
        )
        resume_spec["row_ids"] = new_ids

        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        # The estimate is scoped to the NEW rows only, and exceeds the gate.
        assert (
            runner.estimate(resume_spec, program=model_plan(resume_spec).program)[
                "cost"
            ]
            > 1.0
        )
        with pytest.raises(CostGate):
            validation.validate_spec(
                runner.project,
                runner.router,
                runner.run_store,
                resume_spec,
                program=_program(resume_spec),
                confirmed=False,
                resume_run_id=run_id,
                pricing_policy=runner.pricing_policy,
                composition=runner.execution_composition,
            )

    def test_extend_to_new_rows_requires_exact_confirmation_hash(self, project):
        sheet, cols = seed_sheet(project, ["seed a", "seed b"])
        spec = classify_spec(sheet)
        spec["model"] = spec["params"]["model"] = "anthropic/claude-opus-4-8"
        run_id = self._prepared_run_id(project, spec)

        new_ids = project.add_rows(
            sheet, [{"text": "long text " * 200} for _ in range(300)], cols
        )
        resume_spec = classify_spec(sheet)
        resume_spec["model"] = resume_spec["params"]["model"] = (
            "anthropic/claude-opus-4-8"
        )
        resume_spec["row_ids"] = new_ids

        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        # A bare confirmation cannot approve a backfill scope the original run
        # never estimated. The retry must echo the hash from this backfill's 402.
        with pytest.raises(CostGate) as omitted_echo:
            validation.validate_spec(
                runner.project,
                runner.router,
                runner.run_store,
                resume_spec,
                program=_program(resume_spec),
                confirmed=True,
                resume_run_id=run_id,
                pricing_policy=runner.pricing_policy,
                composition=runner.execution_composition,
            )
        resume_spec["consented_promise_set_hash"] = omitted_echo.value.promise_set_hash
        validated = validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            resume_spec,
            program=_program(resume_spec),
            confirmed=True,
            resume_run_id=run_id,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
        assert validated is not None

    def test_extend_to_new_rows_deterministic_not_gated(self, project):
        sheet, cols = seed_sheet(project, ["seed a", "seed b"])
        program = _FreeResumeRecipe()
        spec = {
            "action_kind": program.name,
            "sheet_id": sheet,
            "input_columns": ["text"],
        }
        run_id = self._prepared_run_id(project, spec, program=program)

        new_ids = project.add_rows(
            sheet, [{"text": "long text " * 200} for _ in range(300)], cols
        )
        resume_spec = {
            "action_kind": program.name,
            "sheet_id": sheet,
            "input_columns": ["text"],
            "row_ids": new_ids,
        }

        runner = MapRunner(
            project,
            ModelRouter(keys={"anthropic": "k"}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        # Deterministic (non-LLM) work costs $0 — a large backfill never prompts.
        assert runner.estimate(resume_spec, program=program)["cost"] == 0.0
        validated = validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            resume_spec,
            program=program,
            confirmed=False,
            resume_run_id=run_id,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
        assert validated is not None


@dataclass
class _FreeResumeRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"
    name: str = "test.free_resume"
    llm: bool = False

    def source_columns(self, spec: dict) -> list[str]:
        del spec
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict]:
        del spec
        return [{"name": "greeting", "column_type": "text"}]


class TestUndoIntegration:
    def test_undo_after_run_restores_empty_column(self, project, tmp_path):
        sheet, _ = seed_sheet(project, ["a", "b"])
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, ["a", "b"])
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        progress = asyncio.run(run_with_output_claim(runner, spec))

        col = next(c for c in project.columns(sheet) if c["name"] == "relevance")
        assert set(project.get_values(sheet, col["id"]).values()) == {7}
        project.undo()
        cols_now = [c["name"] for c in project.columns(sheet)]
        assert "relevance" not in cols_now  # column hidden on undo
        project.redo()
        col = next(c for c in project.columns(sheet) if c["name"] == "relevance")
        assert col["current_run_id"] == progress.run_id


class TestCodexFinalReviewRegressions:
    def test_output_name_collision_with_source_column_rejected(
        self, project, tmp_path, monkeypatch
    ):
        """A regression: AI output must not silently shadow source data."""
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        sheet, _ = seed_sheet(project, ["a"])
        spec = classify_spec(sheet)
        spec["params"]["fields"] = [{"name": "text", "type": "score"}]  # collides!
        router = ModelRouter(keys={"anthropic": "k"})
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        confirmed_spec = confirm_remote_spec(runner, spec)
        with pytest.raises(ValueError, match="collides with a source column"):
            asyncio.run(
                runner.run(
                    confirmed_spec,
                    program=model_plan(confirmed_spec).program,
                    confirmed=True,
                )
            )

    def test_cached_responses_not_billed(self, project, tmp_path):
        """A regression: cache hits cost the provider nothing; bill zero."""
        sheet, _ = seed_sheet(project, ["a"])
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, ["a"])
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        progress = asyncio.run(run_with_output_claim(runner, spec))
        run = project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["cost_actual"] == 0.0


class _SlowAdapter:
    """Deterministic structured reply with a per-call delay, so a run stays
    in flight long enough to be cancelled mid-way."""

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.calls = 0

    async def complete(self, req, client):  # noqa: ANN001
        self.calls += 1
        await asyncio.sleep(self.delay)
        return LLMResponse(
            content='{"relevance": 5}',
            data={"relevance": 5},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


class _SlowCancellationRecipe(Recipe):
    """A bounded local row call used to exercise cancellation, not transport."""

    consumes_resolution = False
    cost_class = "free"

    def __init__(self, delay: float = 0.02):
        super().__init__(name="test.slow_cancellation", llm=False)
        self.delay = delay
        self.calls = 0

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict]:
        return [
            {
                "name": "relevance",
                "column_type": "number",
                "schema": {"type": "number"},
            }
        ]

    async def execute(self, row_values, spec, ctx):  # noqa: ANN001
        del row_values, spec, ctx
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"relevance": 5}


class TestCancel:
    """Cancel a running run: queued rows
    are dropped, in-flight rows drain, partial results stay, status=cancelled."""

    def test_claimless_raw_output_runner_refuses_within_a_bound(
        self, project, monkeypatch
    ):
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        sheet, _ = seed_sheet(project, ["must not reach the provider"])
        spec = classify_spec(sheet)
        adapter = _SlowAdapter()
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        confirmed_spec = confirm_remote_spec(runner, spec)

        async def exercise() -> None:
            with pytest.raises(
                ExecutionRouteVerificationFailed,
                match="no output claim token",
            ):
                await asyncio.wait_for(
                    runner.run(
                        confirmed_spec,
                        program=model_plan(confirmed_spec).program,
                        confirmed=True,
                    ),
                    timeout=2,
                )

        asyncio.run(exercise())
        assert adapter.calls == 0

    def test_cancel_mid_run_drops_queued_rows(
        self,
        project,
        monkeypatch: pytest.MonkeyPatch,
    ):
        sheet, _ = seed_sheet(project, [f"text {i}" for i in range(20)])
        recipe = _SlowCancellationRecipe()
        original_get_recipe = validation.get_recipe
        monkeypatch.setattr(
            validation,
            "get_recipe",
            lambda name: recipe if name == recipe.name else original_get_recipe(name),
        )
        spec = {
            "action_kind": recipe.name,
            "sheet_id": sheet,
            "input_columns": ["text"],
            "output_name": "relevance",
        }
        router = ModelRouter(keys={}, cache=None, cache_mode="off")
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        prepared, claim_token = _prepare_claimed_runner_run(
            runner,
            spec,
            confirmed=False,
        )

        async def go():
            seen: dict = {}
            runner.on_progress = lambda p: seen.setdefault("prog", p)
            task = asyncio.create_task(
                runner.run(
                    spec,
                    confirmed=False,
                    prepared_run=prepared,
                    claim_token=claim_token,
                )
            )
            await _wait_for_calls_or_task_exit(task, recipe, 3)
            seen["prog"].cancel()
            return await asyncio.wait_for(task, timeout=2)

        progress = asyncio.run(go())
        assert progress.cancelled and progress.done
        # queued rows were dropped, not executed (and not billed)
        assert 0 < progress.completed < progress.total
        assert recipe.calls < progress.total
        run = project.db.execute(
            "SELECT status, completed_rows FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        # partial results survived the cancel
        assert run["completed_rows"] == progress.completed > 0

    def test_durable_cancel_intent_stops_at_next_row_and_defers_owner_close(
        self,
        project,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The queue worker leaves its exact writer tuple for the kernel."""

        from frisket.engine.store.runs import RunResultStore

        sheet, _ = seed_sheet(project, [f"text {i}" for i in range(8)])
        recipe = _SlowCancellationRecipe()
        original_get_recipe = validation.get_recipe
        monkeypatch.setattr(
            validation,
            "get_recipe",
            lambda name: recipe if name == recipe.name else original_get_recipe(name),
        )
        spec = {
            "action_kind": recipe.name,
            "sheet_id": sheet,
            "input_columns": ["text"],
            "output_name": "relevance",
        }
        run_store = RunResultStore(project)
        runner = MapRunner(
            project,
            ModelRouter(keys={}, cache=None, cache_mode="off"),
            concurrency=1,
            should_cancel=run_store.cancellation_requested,
            defer_cancel_terminal_close=True,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        prepared, claim_token = _prepare_claimed_runner_run(
            runner,
            spec,
            confirmed=False,
        )

        async def go():
            task = asyncio.create_task(
                runner.run(
                    spec,
                    confirmed=False,
                    prepared_run=prepared,
                    claim_token=claim_token,
                )
            )
            await _wait_for_calls_or_task_exit(task, recipe, 1)
            assert run_store.request_cancel(prepared.run_id)
            return await asyncio.wait_for(task, timeout=2)

        progress = asyncio.run(go())
        assert progress.cancel_requested is True
        assert progress.cancelled is True
        assert progress.done is True
        assert recipe.calls == 1
        assert progress.completed == 0
        assert progress.writer_attempt_id is not None
        assert progress.writer_attempt_claimed is True
        run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (progress.run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "running"
        assert run["finished_at"] is None
        assert run["current_attempt_id"] == progress.writer_attempt_id
        assert run["cancel_requested_at"] is not None
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (progress.writer_attempt_id,),
            ).fetchone()[0]
            == "dispatching"
        )

    def test_cancel_intent_between_attempt_load_and_claim_defers_unclaimed_close(
        self,
        project,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """An admitted attempt is not current-writer authority before its CAS."""

        from frisket.engine.store.runs import RunResultStore

        sheet, _ = seed_sheet(project, ["first", "second"])
        recipe = _SlowCancellationRecipe()
        original_get_recipe = validation.get_recipe
        monkeypatch.setattr(
            validation,
            "get_recipe",
            lambda name: recipe if name == recipe.name else original_get_recipe(name),
        )
        spec = {
            "action_kind": recipe.name,
            "sheet_id": sheet,
            "input_columns": ["text"],
            "output_name": "relevance",
        }
        run_store = RunResultStore(project)
        checks = 0

        def cancel_after_attempt_load(run_id: int) -> bool:
            nonlocal checks
            checks += 1
            if checks == 1:
                return False
            assert run_store.request_cancel(run_id)
            return True

        runner = MapRunner(
            project,
            ModelRouter(keys={}, cache=None, cache_mode="off"),
            concurrency=1,
            should_cancel=cancel_after_attempt_load,
            defer_cancel_terminal_close=True,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        prepared, claim_token = _prepare_claimed_runner_run(
            runner,
            spec,
            confirmed=False,
        )

        progress = asyncio.run(
            runner.run(
                spec,
                confirmed=False,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )

        assert checks == 2
        assert recipe.calls == 0
        assert progress.cancel_requested is True
        assert progress.cancelled is True
        assert progress.writer_attempt_id is not None
        assert progress.writer_attempt_claimed is False
        run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (progress.run_id,),
        ).fetchone()
        assert run is not None
        assert tuple(run)[:3] == ("running", None, None)
        assert run["cancel_requested_at"] is not None
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (progress.writer_attempt_id,),
            ).fetchone()["state"]
            == "admitted"
        )

    def test_cancel_one_row_at_fence_finalizes_cancelled_zero_completed(
        self, project, monkeypatch
    ):
        """sandbox-cancellation contract: a cancellation observed AFTER a row's
        result is ready but before it is accepted drops the row at the lock's
        fence — no result/error/completed progress. The provider cost that was
        already incurred remains accounting truth. A one-row run therefore
        finalizes with zero completed/failed rows and status 'cancelled' (the
        row stays resumable via backfill), instead of the old unconditional
        completed++ that made a cancelled one-row run look completed."""
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        sheet, _ = seed_sheet(project, ["only row"])
        spec = classify_spec(sheet)
        adapter = _SlowAdapter()
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        # Cancel becomes observable only once the row's model call has run, so
        # the row executes and is dropped at the post-execute fence rather than
        # at the queue-drain check.
        runner.should_cancel = lambda run_id: adapter.calls >= 1

        confirmed_spec = confirm_remote_spec(runner, spec)
        progress = asyncio.run(
            run_with_output_claim(runner, confirmed_spec, confirmed=True)
        )
        assert progress.cancelled and progress.done
        assert progress.completed == 0 and progress.failed == 0
        assert progress.cost == 0.0001
        assert adapter.calls == 1  # the row DID run; its result was just dropped
        run = project.db.execute(
            "SELECT status, completed_rows FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        assert run["completed_rows"] == 0

    def test_uncancelled_run_still_completes(self, project, tmp_path):
        sheet, _ = seed_sheet(project, ["a", "b"])
        spec = classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        prime_cache(cache, project, sheet, spec, ["a", "b"])
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        progress = asyncio.run(run_with_output_claim(runner, spec))
        run = project.db.execute(
            "SELECT status FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "completed" and not progress.cancelled


ARRAY_ITEM_SCHEMA = {  # a top-level-array output schema, e.g. ops/entities.py
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


@dataclass
class _FakeArrayRecipe(Recipe):
    """An llm=True recipe declaring a TOP-LEVEL ARRAY output schema — the
    shape structured.py wraps as {"items": [...]} for the wire (StructuredDict
    requires an object, PoC INV1d) and unwraps on return."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    name: str = "fake_array"
    llm: bool = True

    def render(self, row_values: dict, spec: dict) -> RenderedCall:
        return RenderedCall(
            messages=[{"role": "user", "content": "go"}],
            schema=ARRAY_ITEM_SCHEMA,
            max_tokens=256,
        )


class _ArrayToolUseAdapter:
    """Mimics the REAL Anthropic tool_use wire shape (adapters.py:104-121):
    the payload lives in `.data`, `.content` is None. The wire reply carries
    the WRAPPED {"items": [...]} form a real `emit` tool call would return
    for this schema."""

    def __init__(self, items: list):
        self.items = items

    async def complete(self, req, client):  # noqa: ANN001
        return LLMResponse(
            content=None,
            data={"items": self.items},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


@dataclass
class _FakeSchemalessRecipe(Recipe):
    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)
    name: str = "fake_schemaless"
    llm: bool = True

    def render(self, row_values: dict, spec: dict) -> RenderedCall:
        return RenderedCall(
            messages=[{"role": "user", "content": str(row_values)}],
            schema=None,
            max_tokens=64,
        )


class _ScriptedRowRepairAdapter:
    api_key = "test-key"

    def __init__(self, outputs: list[dict]):
        self.outputs = outputs
        self.seen = []
        self.seen_cost_models = []

    async def complete(  # noqa: ANN001
        self, request, client, *, cost_model=None, **kwargs
    ):
        self.seen.append(request)
        self.seen_cost_models.append(cost_model)
        attempt = len(self.seen)
        rated_model = cost_model or request.model
        cost, cost_source = cost_of_with_source(
            rated_model,
            attempt,
            attempt * 10,
        )
        assert cost is not None
        return LLMResponse(
            content=None,
            data=self.outputs[attempt - 1],
            tokens_in=attempt,
            tokens_out=attempt * 10,
            cost=cost,
            cost_source=cost_source,
            model=request.model,
        )


def _scripted_row_repair_router(
    model: str, outputs: list[dict]
) -> tuple[ModelRouter, _ScriptedRowRepairAdapter]:
    adapter = _ScriptedRowRepairAdapter(outputs)
    if model.startswith("ollama/"):
        endpoint = LocalModelEndpointConfig(
            endpoint_id="test-local",
            display_name="Test local",
            origin="http://127.0.0.1:11434",
            source="local_file",
        )
        router = ModelRouter(
            use_env_keys=False,
            cache=None,
            cache_mode="off",
            local_endpoints=(endpoint,),
        )
        local_adapter = router._adapters["ollama"]  # noqa: SLF001
        endpoint_adapter = local_adapter._adapters["test-local"]  # noqa: SLF001
        endpoint_adapter.complete = adapter.complete
    else:
        router = ModelRouter(
            keys={"openai": "test-key"},
            key_sources={"openai": "org_byok"},
            use_env_keys=False,
            cache=None,
            cache_mode="off",
        )
        router._adapters["openai"] = adapter  # noqa: SLF001
    return router, adapter


_REPAIR_CASES = [
    pytest.param(
        "openai/gpt-5-mini",
        [0.00002025, 0.0000405, 0.00006075, 0.000081],
        "pricing_data",
        None,
        id="hosted-priced",
    ),
    pytest.param(
        "ollama/@test-local/qwen3:0.6b",
        [0.0] * 4,
        "free_local",
        "ollama/@test-local/qwen3:0.6b",
        id="canonical-ollama",
    ),
]


@pytest.mark.parametrize(
    ("model", "expected_costs", "expected_source", "expected_cost_model"),
    _REPAIR_CASES,
)
def test_map_row_repairs_three_invalid_outputs_then_persists_fourth_success(
    project,
    model,
    expected_costs,
    expected_source,
    expected_cost_model,
):
    sheet, _ = seed_sheet(project, ["Ada Lovelace"])
    spec = classify_spec(sheet)
    spec["model"] = spec["params"]["model"] = model
    router, adapter = _scripted_row_repair_router(
        model,
        [{"wrong": 1}, {"wrong": 2}, {"wrong": 3}, {"relevance": 7}],
    )
    runner = MapRunner(
        project,
        router,
        concurrency=1,
        authority=UnroutedOnlyAuthority(project),
        allow_action_lifecycle_only_recipes=True,
    )

    async def run():
        try:
            return await run_with_exact_confirmation(runner, spec)
        finally:
            await router.aclose()

    progress = asyncio.run(run())

    assert progress.done is True
    assert progress.completed == 1
    assert progress.failed == 0
    assert len(adapter.seen) == 4
    assert adapter.seen_cost_models == [expected_cost_model] * 4

    calls = [dict(row) for row in RunResultStore(project).model_calls(progress.run_id)]
    assert len(calls) == 4
    calls.sort(key=lambda call: json.loads(call["units"])["tokens_in"])
    assert [call["provider_cost_usd"] for call in calls] == pytest.approx(
        expected_costs
    )
    assert [call["provider_reported_cost_usd"] for call in calls] == pytest.approx(
        expected_costs
    )
    assert [call["cost_source"] for call in calls] == [expected_source] * 4
    assert [json.loads(call["units"]) for call in calls] == [
        {"tokens_in": 1, "tokens_out": 10},
        {"tokens_in": 2, "tokens_out": 20},
        {"tokens_in": 3, "tokens_out": 30},
        {"tokens_in": 4, "tokens_out": 40},
    ]
    run_row = RunResultStore(project).get_run(progress.run_id)
    assert run_row is not None
    assert run_row["cost_actual"] == pytest.approx(sum(expected_costs))


@pytest.mark.parametrize(
    ("model", "expected_costs", "expected_source", "expected_cost_model"),
    _REPAIR_CASES,
)
def test_map_row_exhausts_after_exactly_three_repairs_and_keeps_accounting(
    project,
    model,
    expected_costs,
    expected_source,
    expected_cost_model,
):
    sheet, _ = seed_sheet(project, ["Ada Lovelace"])
    spec = classify_spec(sheet)
    spec["model"] = spec["params"]["model"] = model
    router, adapter = _scripted_row_repair_router(
        model,
        [{"wrong": 1}, {"wrong": 2}, {"wrong": 3}, {"wrong": 4}],
    )

    async def run_row():
        try:
            return await execute_row(
                project,
                router,
                AdaptiveThrottle(),
                model_plan(spec).program,
                {"text": "Ada Lovelace"},
                spec,
                OpContext(project=project),
                output_field_names=("relevance",),
            )
        finally:
            await router.aclose()

    result = asyncio.run(run_row())

    assert len(adapter.seen) == 4
    assert adapter.seen_cost_models == [expected_cost_model] * 4
    cell = result["relevance"]
    assert cell["value"] is None
    assert cell["error_code"] == "invalid_output"
    assert cell["outcome"] == "invalid_output"
    assert cell["tokens_in"] == 10
    assert cell["tokens_out"] == 100
    assert cell["cost"] == pytest.approx(sum(expected_costs))
    assert [call["provider_cost_usd"] for call in cell["model_calls"]] == (
        pytest.approx(expected_costs)
    )
    assert [call["cost_source"] for call in cell["model_calls"]] == [
        expected_source
    ] * 4


def test_schemaless_map_row_persists_direct_response_fact(project):
    class _Router:
        async def complete(self, request, **kwargs):  # noqa: ANN001
            return LLMResponse(
                content="plain text",
                data={"answer": "plain text"},
                tokens_in=7,
                tokens_out=3,
                cost=0.004,
                model=request.model,
                credential_source="org_byok",
            )

    router = _Router()
    row_data, meta = asyncio.run(
        llm_row(
            router,
            AdaptiveThrottle(),
            _FakeSchemalessRecipe(),
            {"text": "hello"},
            {"model": "openai/gpt-5-mini"},
        )
    )

    assert row_data == {"answer": "plain text"}
    assert meta["cost"] == pytest.approx(0.004)
    [fact] = meta["model_calls"]
    assert fact["credential_source"] == "org_byok"
    assert fact["units"] == {"tokens_in": 7, "tokens_out": 3}


class TestStructuredArrayUnwrapMapPath:
    """A regression: the map path must return the completer's UNWRAPPED
    array (frisket.runner.row_execution's `llm_row`), not the wire's wrapped
    ``{"items": [...]}`` form — neither via `result.response.data` (structured.py
    `_sum_response`) nor via a stale `resp.data` read in row_execution.py."""

    def test_array_schema_llm_row_returns_bare_list(self, project):
        items = [{"text": "Ada"}, {"text": "NYC"}]
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = _ArrayToolUseAdapter(items)  # noqa: SLF001
        recipe = _FakeArrayRecipe()
        spec = {"model": "anthropic/claude-haiku-4-5"}

        data, meta = asyncio.run(llm_row(router, AdaptiveThrottle(), recipe, {}, spec))

        assert data == items  # the bare list, NOT {"items": [...]}
        assert meta["tokens_in"] == 10

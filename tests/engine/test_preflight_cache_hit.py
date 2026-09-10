from __future__ import annotations

import asyncio

from frisket.ai.llm import (
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from typed_model_fixtures import model_plan
from frisket.engine.runner import MapRunner, MissingProviderKey
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import (
    prepare_with_exact_confirmation,
    run_with_exact_confirmation,
)

MODEL = "anthropic/claude-haiku-4-5"


def _seed_two_rows(project: Project) -> tuple[int, dict[str, int], list[int]]:
    sheet_id = project.add_sheet("data")
    cols = {"text": project.add_column(sheet_id, "text")}
    row_ids = project.add_rows(
        sheet_id,
        [{"text": "hello there"}, {"text": "genuinely uncached row"}],
        cols,
    )
    return sheet_id, cols, row_ids


def _classify_spec(sheet_id: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "test",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


def test_replay_mode_cache_hit_completes_keyless_no_missing_provider_key(tmp_path):
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id, _cols, row_ids = _seed_two_rows(project)
        spec = _classify_spec(sheet_id)
        cache = ResponseCache(tmp_path / "c.db")
        recipe = model_plan(spec).program
        call = recipe.render({"text": "hello there"}, spec)
        req = LLMRequest(
            model=spec["model"],
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        # Only the FIRST row's exact rendered prompt is pre-cached — the
        # second row is a genuine miss, proving this isn't "block nothing".
        cache.put(
            request_key(req, recipe.version),
            LLMResponse(
                content=None,
                data={"relevance": 7},
                tokens_in=10,
                tokens_out=2,
                cost=0.0001,
                model=spec["model"],
            ),
        )
        router = ModelRouter(cache=cache, cache_mode="replay")  # no keys configured
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))

        # The real bug: this used to raise MissingProviderKey before any row
        # ran, even though row 1 is fully servable from cache.
        progress = asyncio.run(run_with_exact_confirmation(runner, spec))

        # progress.completed counts every finished row (failures included —
        # see map_runner.py); one of the two rows failed (the genuine miss).
        assert progress.completed == 2
        assert progress.failed == 1

        col = next(c for c in project.columns(sheet_id) if c["name"] == "relevance")
        rows = {
            r["row_id"]: r
            for r in project.db.execute(
                "SELECT row_id, value, error, outcome FROM results "
                "WHERE run_id=? AND column_id=?",
                (progress.run_id, col["id"]),
            ).fetchall()
        }
        hit_row_id, miss_row_id = row_ids
        assert rows[hit_row_id]["error"] is None
        assert rows[hit_row_id]["outcome"] == "ok"
        # cache stores the score under the field's own JSON shape
        import json

        assert json.loads(rows[hit_row_id]["value"]) == 7

        # The genuine miss surfaces the SAME typed, actionable copy the
        # eager pre-flight would have raised — as a per-row failure, not a
        # run-wide crash.
        assert rows[miss_row_id]["error"] is not None
        assert "No API key is configured" in rows[miss_row_id]["error"]
        assert rows[miss_row_id]["outcome"] == "model_error"
    finally:
        project.close()


def test_replay_mode_without_a_cache_object_still_blocks_eagerly(tmp_path):
    """'replay' with no ResponseCache configured can NEVER serve a hit
    (ModelRouter.complete only reads the cache when self.cache is not None),
    so it must still hard-block at confirm time exactly like 'fresh'/'off' —
    this is the case the exemption must NOT swallow."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id, _cols, _row_ids = _seed_two_rows(project)
        spec = _classify_spec(sheet_id)
        router = ModelRouter(cache=None, cache_mode="replay")  # no keys, no cache
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        try:
            prepare_with_exact_confirmation(runner, spec)
            raised = False
        except MissingProviderKey as exc:
            raised = True
            assert exc.provider == "anthropic"
        assert raised, "a cacheless replay run must still block eagerly"
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()

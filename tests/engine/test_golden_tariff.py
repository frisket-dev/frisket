"""Golden project #1: the tariff country-search — Dylan's live
IRE demo reproduced: countries CSV → web_search with {{country}} substitution →
summarize → 0-10 impact score with justification. Chained recipes, mixed
LLM/non-LLM ops.

Network-marked (live DDG search + live/cached model calls):
    set -a; source .secrets/frisket.env; set +a
    FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay uv run pytest -m network tests/test_golden_tariff.py
"""

import asyncio
import os
import time

import pytest

from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.testing import llm_cache_path
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from action_test_helpers import run_typed_map_request
from runner_test_helpers import run_with_exact_confirmation

CACHE_PATH = llm_cache_path()
MODE = os.environ.get("FRISKET_CACHE_MODE", "replay")

COUNTRIES = ["Canada", "Mexico", "China", "Germany", "Vietnam", "Brazil"]
MODEL = "gemini/gemini-2.5-flash"


@pytest.mark.network
@pytest.mark.realtime
def test_golden_tariff_chain(tmp_path):
    # one event loop for the whole chain: the router's httpx client is
    # loop-bound; multiple asyncio.run() calls would strand it
    _loop = asyncio.new_event_loop()

    def run_async(coro):
        return _loop.run_until_complete(coro)

    p = Project.create(tmp_path / "tariff.frisket", name="tariff-impacts")
    try:
        sheet = p.add_sheet("countries")
        cols = {"country": p.add_column(sheet, "country")}
        p.add_rows(sheet, [{"country": c} for c in COUNTRIES], cols)
        row_ids = list(p.visible_row_ids(sheet))

        router = ModelRouter(cache=ResponseCache(CACHE_PATH), cache_mode=MODE)
        runner = MapRunner(p, router, concurrency=3, authority=UnroutedOnlyAuthority(p))

        async def sleep(n):
            await asyncio.sleep(n)

        # step 1: web search per row (non-LLM, {{column}} substitution)
        search_request = {
            "action_id": "research.web_search",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "query": {"text": "US tariff impacts on {{country}} economy 2026"},
                "max_results": 8,
            },
            "output_names": {"search_results": "search_results"},
            "idempotency_key": "golden-tariff-web-search",
        }
        challenge = run_typed_map_request(p, search_request, project_id="golden-tariff")
        search_request["confirmation"] = challenge.errors[0].details["promise_set_hash"]
        prog1 = run_typed_map_request(p, search_request, project_id="golden-tariff")
        assert prog1.status in {"completed", "partial"}, prog1.errors
        failed_search_rows = p.db.execute(
            "SELECT failed_rows FROM runs WHERE id=?", (prog1.run_id,)
        ).fetchone()["failed_rows"]
        assert failed_search_rows <= 1, "search should mostly succeed"

        sr_col = next(c for c in p.columns(sheet) if c["name"] == "search_results")
        results = p.get_values(sheet, sr_col["id"])
        ok_rows = [
            rid for rid, v in results.items() if isinstance(v, list) and len(v) >= 3
        ]
        assert len(ok_rows) >= len(COUNTRIES) - 1
        sample = results[ok_rows[0]][0]
        assert sample["title"] and sample["url"]

        # step 2: summarize the search results (chained: AI reads AI input)
        sum_spec = {
            "action_kind": "map.summarize",
            "model": MODEL,
            "sheet_id": sheet,
            "input_columns": ["country", "search_results"],
            "instruction": "Summarize what these search results say about US "
            "tariff impacts on this country in one paragraph.",
            "output_name": "summary",
        }
        prog2 = run_async(run_with_exact_confirmation(runner, sum_spec))
        if prog2.failed:
            time.sleep(20)  # provider per-minute quota recovery before fresh retry
            failed_row_ids = runner.run_store.result_row_failure_states(
                prog2.run_id, set(row_ids)
            )
            retry_row_ids = [
                row_id for row_id in row_ids if failed_row_ids.get(row_id, False)
            ]
            assert len(retry_row_ids) == prog2.failed
            prog2 = run_async(
                run_with_exact_confirmation(
                    runner,
                    {**sum_spec, "row_ids": retry_row_ids, "overwrite": True},
                )
            )
        assert prog2.failed <= 1

        # step 3: score severity (chained again)
        score_spec = {
            "action_kind": "map.classify",
            "model": MODEL,
            "sheet_id": sheet,
            "input_columns": ["country", "summary"],
            "context": "Each row is a country and a summary of search results "
            "about US tariff impacts on it.",
            "fields": [
                {
                    "name": "tariff_impact",
                    "type": "score",
                    "description": "0 = no economic impact, 10 = severe "
                    "economic impact from US tariffs",
                }
            ],
            "include_justification": True,
        }
        prog3 = run_async(run_with_exact_confirmation(runner, score_spec))
        if prog3.failed:
            time.sleep(20)  # provider per-minute quota recovery before fresh retry
            failed_row_ids = runner.run_store.result_row_failure_states(
                prog3.run_id, set(row_ids)
            )
            retry_row_ids = [
                row_id for row_id in row_ids if failed_row_ids.get(row_id, False)
            ]
            assert len(retry_row_ids) == prog3.failed
            prog3 = run_async(
                run_with_exact_confirmation(
                    runner,
                    {**score_spec, "row_ids": retry_row_ids, "overwrite": True},
                )
            )
        assert prog3.failed <= 1

        score_col = next(c for c in p.columns(sheet) if c["name"] == "tariff_impact")
        scores = {
            rid: v
            for rid, v in p.get_values(sheet, score_col["id"]).items()
            if v is not None
        }
        assert len(scores) >= len(COUNTRIES) - 1
        assert all(0 <= v <= 10 for v in scores.values())
        # the tip-line property, weak form: top economies score meaningfully
        assert max(scores.values()) >= 5

        # full chain visible in history
        stage_labels = [o["label"] for o in p.history() if o["kind"] == "map"]
        assert list(dict.fromkeys(stage_labels)) == [
            "web_search: search_results",
            "summarize: summary",
            "classify: tariff_impact, tariff_impact_justification",
        ]
        run_async(router.aclose())
        _loop.close()
    finally:
        p.close()

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    _insert_running_reservation,
    case_env,
    run_action_with_confirmation,
)
from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.pricing import cost_of_with_source
from frisket.engine.store import Project
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage

# Reset by _patch_agent at the start of every harness test for this case;
# asserts on these are only meaningful behind that patch.
_ADAPTER: list[Any] = []
_SEARCH_CALLS: list[str] = []


def test_catalog_separates_receipt_evidence_from_diagnostic_traces() -> None:
    from frisket.actions.registry import ACTION_REGISTRY

    entry = ACTION_REGISTRY.get("research.answer").catalog_entry()

    assert "public sources" in entry["description"]
    assert (
        ACTION_REGISTRY.get("research.answer").definition.description
        == entry["description"]
    )
    assert "write_trace" in entry["side_effects"]


def test_agent_output_fields_honor_include_sources_toggle():
    # Sources are a caller toggle (``research.answer.include_sources``).
    # Default ON declares the
    # companion `{name}_sources` column; OFF declares answer only, so the map
    # runner never creates a source column (runner/map_runner.py iterates
    # output_fields).
    from frisket.actions.row_research import ANSWER, ResearchParams

    params = ResearchParams(
        source=["company"],
        model="anthropic/claude-haiku-4-5",
        question={"text": "Research"},
    )
    on = [f.key for f in ANSWER.run.resolve_output_fields(params)]
    assert on == ["answer", "sources"]
    off = [
        f.key
        for f in ANSWER.run.resolve_output_fields(
            params.model_copy(update={"include_sources": False})
        )
    ]
    assert off == ["answer"]


def test_agent_render_keeps_the_bounded_live_step_budget() -> None:
    from frisket.ai.research.row_answer import MAX_STEPS, MAX_STEP_OUTPUT_TOKENS

    assert MAX_STEPS == 5
    assert MAX_STEP_OUTPUT_TOKENS == 2_048


class _ResearchAdapter:
    """Scripts the agent's two-tool loop (the caller migration, ops/agent.py -- search/fetch as
    @agent.tool_plain functions, a plain-text response is the finish signal):
    odd calls request the ``search`` tool, even calls answer in plain text."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        call_number = len(self.requests)
        if call_number % 2 == 1:
            query = f"official source {call_number}"
            return LLMResponse(
                content=None,
                data=None,
                tool_calls=[
                    {
                        "name": "search",
                        "args": {"query": query},
                        "id": f"call_{call_number}",
                    }
                ],
                tokens_in=90 + call_number,
                tokens_out=12,
                cost=0.004,
                model=req.model,
            )
        row_number = call_number // 2
        return LLMResponse(
            content=f"Cited answer {row_number}",
            data=None,
            tokens_in=90 + call_number,
            tokens_out=12,
            cost=0.004,
            model=req.model,
        )


class _PricedResearchAdapter(OpenAICompatAdapter):
    """Agent loop whose amount/source come from the production price seam."""

    def __init__(self) -> None:
        super().__init__("test", "http://127.0.0.1:11434/v1")
        self.requests: list[LLMRequest] = []

    async def complete(  # noqa: ANN001
        self, req: LLMRequest, client, *, cost_model: str | None = None
    ) -> LLMResponse:
        del client
        self.requests.append(req)
        call_number = len(self.requests)
        cost, cost_source = cost_of_with_source(
            cost_model or req.model,
            90 + call_number,
            12,
        )
        if call_number % 2 == 1:
            content = None
            tool_calls = [
                {
                    "name": "search",
                    "args": {"query": f"official source {call_number}"},
                    "id": f"call_{call_number}",
                }
            ]
        else:
            content = f"Cited answer {call_number // 2}"
            tool_calls = None
        return LLMResponse(
            content=content,
            data=None,
            tool_calls=tool_calls,
            tokens_in=90 + call_number,
            tokens_out=12,
            cost=cost,
            cost_source=cost_source,
            model=req.model,
        )


class _FailingResearchAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        raise LLMError("research answer provider unavailable", status=503)


def _router() -> tuple[ModelRouter, _ResearchAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _ResearchAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _failing_router() -> tuple[ModelRouter, _FailingResearchAdapter]:
    router = ModelRouter(
        keys={"anthropic": "k"},
        cache=None,
        cache_mode="off",
        max_retries=0,
    )
    adapter = _FailingResearchAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _patch_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from frisket.engine.executor import research_read

    _ADAPTER.clear()
    _SEARCH_CALLS.clear()

    async def fake_search(query: str) -> tuple[str, list[str]]:
        _SEARCH_CALLS.append(query)
        idx = len(_SEARCH_CALLS)
        url = f"https://example.test/research/source-{idx}"
        return f"Source {idx} says the claim is cited. {url}", [url]

    monkeypatch.setattr(research_read, "search_web", fake_search)
    router, adapter = _router()
    _ADAPTER.append(adapter)
    return {"router": router}


def _research_answer_action(
    sheet_id: int,
    *,
    idempotency_key: str = "research_answer@sha256:stable",
    output_name: str = "answer",
    model: str = "anthropic/claude-haiku-4-5",
) -> dict[str, Any]:
    return {
        "action_id": "research.answer",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"answer": output_name, "sources": f"{output_name}_sources"},
        "params": {
            "source": ["company", "topic"],
            "question": {"text": "Answer using cited public sources for this row."},
            "model": model,
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Subjects")
    columns = {
        "company": project.add_column(sheet_id, "company", type="text"),
        "topic": project.add_column(sheet_id, "topic", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {"company": "Acme Corp", "topic": "battery recall"},
            {"company": "Globex", "topic": "export controls"},
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


@pytest.mark.parametrize(
    ("model", "expected_source", "expected_cost_known"),
    [
        ("ollama/@test-local/frisket-unlisted", "free_local", True),
        ("anthropic/claude-haiku-4-5", "pricing_data", True),
        ("anthropic/frisket-unpriced", "unknown", False),
    ],
)
def test_research_answer_persists_pricing_source_by_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_source: str,
    expected_cost_known: bool,
) -> None:
    """The durable receipt keeps the pricing fact minted before routing."""
    from frisket.contracts.action import Receipt
    from frisket.engine.store.runs import RunResultStore
    from frisket.engine.executor import research_read

    search_calls: list[str] = []

    async def fake_search(query: str) -> tuple[str, list[str]]:
        search_calls.append(query)
        url = f"https://example.test/research/source-{len(search_calls)}"
        return f"A cited source. {url}", [url]

    monkeypatch.setattr(research_read, "search_web", fake_search)
    adapter = _PricedResearchAdapter()
    if model.startswith("ollama/"):
        router = ModelRouter(
            cache=None,
            cache_mode="off",
            use_env_keys=False,
            local_endpoints=(
                LocalModelEndpointConfig(
                    endpoint_id="test-local",
                    display_name="Test local",
                    origin="http://127.0.0.1:11434",
                    source="local_file",
                ),
            ),
        )
        router._adapters["ollama"]._adapters["test-local"] = adapter  # type: ignore[attr-defined]  # noqa: SLF001
    else:
        router = ModelRouter(
            keys={"anthropic": "k"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = adapter  # noqa: SLF001

    project = Project.create(tmp_path / f"research-{expected_source}.frisket")
    try:
        seeded = _seed(project, tmp_path)
        action = _research_answer_action(
            seeded["sheet_id"],
            idempotency_key=f"research_answer@sha256:{expected_source}",
            model=model,
        )
        result = run_action_with_confirmation(
            project,
            action,
            project_id=f"project-research-{expected_source}",
            router=router,
        )

        assert result.status == "completed", (
            result.errors,
            [
                dict(row)
                for row in project.db.execute("SELECT error, error_code FROM results")
            ],
        )
        calls = RunResultStore(project).model_calls(result.run_id)
        assert len(calls) == 4
        assert {call["cost_source"] for call in calls} == {expected_source}
        if expected_cost_known:
            assert all(call["provider_cost_usd"] is not None for call in calls)
        else:
            assert all(call["provider_cost_usd"] is None for call in calls)

        receipt = Receipt.model_validate_json(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
        llm_use = next(
            item for item in receipt.provider_use if item.get("model") == model
        )
        assert (llm_use["cost_actual"] is not None) is expected_cost_known
    finally:
        project.close()


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _research_answer_action(seeded["sheet_id"])


def _legacy_capabilities_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _research_answer_action(
        seeded["sheet_id"],
        idempotency_key="research_answer@sha256:legacy-capabilities",
    )
    action["capabilities"] = ["project:write", "model:complete"]
    return action


def _bad_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _research_answer_action(
        seeded["sheet_id"],
        idempotency_key="research_answer@sha256:bad-input",
    )
    action["params"]["source"] = ["missing"]
    return action


def _collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _research_answer_action(
        seeded["sheet_id"],
        idempotency_key="research_answer@sha256:collision",
        output_name="company",
    )


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _make_action(seeded)
    action["params"]["question"] = {"text": "Answer a different question."}
    return action


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.store.runs import RunResultStore
    from frisket.operability.trace import read_trace
    from frisket.ai.research.row_answer import MAX_STEP_OUTPUT_TOKENS

    sheet_id = seeded["sheet_id"]
    assert len(_ADAPTER[0].requests) == 4
    assert {request.max_tokens for request in _ADAPTER[0].requests} == {
        MAX_STEP_OUTPUT_TOKENS
    }
    assert len(_SEARCH_CALLS) == 2
    assert [output.name for output in result.outputs] == ["answer", "answer_sources"]
    assert result.outputs[0].ref["role"] == "answer"
    assert result.outputs[1].ref["role"] == "sources"

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "research.answer"
    assert run["model"] == "anthropic/claude-haiku-4-5"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0
    assert float(run["cost_actual"]) > 0

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    assert columns["answer"]["type"] == "text"
    assert columns["answer_sources"]["type"] == "json"
    assert columns["answer"]["current_run_id"] == result.run_id
    assert columns["answer_sources"]["current_run_id"] == result.run_id
    answers = project.get_values(sheet_id, int(columns["answer"]["id"]))
    sources = project.get_values(sheet_id, int(columns["answer_sources"]["id"]))
    assert list(answers.values()) == ["Cited answer 1", "Cited answer 2"]
    assert all(
        source_list[0].startswith("https://example.test/research/source-")
        for source_list in sources.values()
    )

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 4
    assert {row["capability"] for row in model_calls} == {"llm.complete"}
    assert {row["provider"] for row in model_calls} == {"anthropic"}
    # The router measures every live wire call; _receipts must carry that
    # measurement onto each fact rather than a fabricated NULL.
    for row in model_calls:
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0

    trace = read_trace(project.path, result.run_id)
    assert trace is not None
    assert trace["action_kind"] == "research.answer"
    assert len(trace["rows"]) == 2
    assert {len(row["calls"]) for row in trace["rows"]} == {2}

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "research.answer"
    assert receipt.status == "completed"
    assert len(receipt.provider_use) == 1
    assert receipt.provider_use[0]["provider"] == "anthropic"
    assert receipt.provider_use[0]["model_call_count"] == 4
    output_refs = {item.name: item.ref for item in receipt.outputs}
    assert output_refs["answer"]["kind"] == "map_result_column"
    assert output_refs["answer"]["role"] == "answer"
    assert output_refs["answer_sources"]["role"] == "sources"
    assert output_refs["answer"]["value_hash"].startswith("sha256:")
    assert output_refs["answer_sources"]["value_hash"].startswith("sha256:")
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    assert "typed_action_request" in evidence
    assert "model_rows_model_calls" in evidence
    assert sorted(url for urls in sources.values() for url in urls) == [
        "https://example.test/research/source-1",
        "https://example.test/research/source-2",
    ]
    assert "research_answer_trace" not in evidence


CASES = [
    ExecutorCase(
        kind="research.answer",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=(
                "project:write",
                "model:complete",
                "external:web_search",
                "external:http_fetch",
            ),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_model_router",
                    "call_external_provider",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_model_calls",
                    "write_trace",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "output_column_exists",
                    "model_cost_requires_confirmation",
                    "model_run_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="unknown",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_agent,
        gates=(
            Gate(
                "legacy_capabilities",
                _legacy_capabilities_action,
                "invalid_action_request",
            ),
            Gate("invalid_input_ref", _bad_input_action, "invalid_input_ref"),
            Gate("output_column_exists", _collision_action, "output_column_exists"),
            Gate(
                "idempotency_conflict",
                _conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "columns": 2,
            "runs": 1,
            "results": 4,
            "model_calls": 4,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


@pytest.mark.parametrize(
    ("action_id", "effects"),
    [
        ("map.ask", {"call_model_router"}),
        ("map.summarize", {"call_model_router"}),
        ("map.mcp_extract", {"call_model_router"}),
        ("research.web_search", {"call_external_provider"}),
        ("media.ytdlp_download", {"call_external_provider"}),
        ("research.answer", {"call_model_router", "call_external_provider"}),
    ],
)
def test_research_catalog_preserves_additive_provider_effects(action_id, effects):
    from frisket.actions.system import root_action_catalog

    entry = next(
        action for action in root_action_catalog().actions if action.kind == action_id
    )
    assert (
        set(entry.side_effects) & {"call_model_router", "call_external_provider"}
        == effects
    )


def test_typed_research_publishes_complete_durable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run_primary()
        assert result.status == "completed", result.errors
        _check_state(env.project, env.seeded, result)


def test_unconfirmed_run_pauses_before_model_or_search_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        # Exercise explicit confirmation independently of the default preapproval.
        env.run_kwargs["deps"] = ExecutorDeps(
            consent_coverage=ConsentCoverage(
                instance_principal(env.project), Decimal("0")
            )
        )
        unconfirmed = _research_answer_action(
            env.seeded["sheet_id"],
            idempotency_key="research_answer@sha256:unconfirmed",
        )
        before = env.counts()
        result = env.run_once(unconfirmed)
        assert result.status == "needs_confirmation"
        assert result.errors[0].code == "model_cost_requires_confirmation"
        details = result.errors[0].details
        assert len(details["promise_set_hash"]) == 64
        assert details["estimate"]["rows"] == 2
        assert details["estimate"]["remote_capability"] in {
            "external:web_search",
            "external:http_fetch",
        }
        assert _ADAPTER[0].requests == []
        assert _SEARCH_CALLS == []
        assert env.counts() == before


def test_stale_replay_probes_fail_without_recalling_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay trust rungs: tampered receipt role, stored result, and hidden
    output refuse replay, while loss of best-effort trace detail does not,
    (as ``stale_replay``)
    without re-entering the model or search provider."""
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        before = env.counts()

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_ADAPTER[0].requests) == 4
        assert len(_SEARCH_CALLS) == 2

        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        tampered = receipt.model_copy(deep=True)
        tampered.outputs[1].ref["role"] = "answer"
        env.project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (
                json.dumps(tampered.model_dump(mode="json"), sort_keys=True),
                first.receipt_id,
            ),
        )
        env.project.db.commit()
        stale_role = env.run_primary()
        assert stale_role.status == "failed"
        assert stale_role.errors[0].code == "stale_replay"
        env.project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (receipt_row["body"], first.receipt_id),
        )
        env.project.db.commit()

        columns = {
            row["name"]: row
            for row in env.project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?",
                (env.seeded["sheet_id"],),
            ).fetchall()
        }
        source_result = env.project.db.execute(
            "SELECT row_id, value FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id LIMIT 1",
            (first.run_id, columns["answer_sources"]["id"]),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            env.project.db.execute(
                "UPDATE results SET value=? "
                "WHERE run_id=? AND row_id=? AND column_id=?",
                (
                    json.dumps(["https://example.test/research/tampered"]),
                    first.run_id,
                    source_result["row_id"],
                    columns["answer_sources"]["id"],
                ),
            )
        env.project.db.rollback()

        from frisket.operability.trace import trace_path

        trace_path(env.project.path, first.run_id).unlink()
        trace_free_replay = env.run_primary()
        assert trace_free_replay.status == "completed"
        assert trace_free_replay.receipt_id == first.receipt_id

        env.project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (columns["answer"]["id"],)
        )
        env.project.db.commit()
        stale_hidden = env.run_primary()
        assert stale_hidden.status == "failed"
        assert stale_hidden.errors[0].code == "stale_replay"

        assert env.counts() == before
        assert len(_ADAPTER[0].requests) == 4
        assert len(_SEARCH_CALLS) == 2


def test_running_reservation_blocks_duplicate_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = env.case.make_action(env.seeded)
        receipt_id = _insert_running_reservation(env, action)
        before = env.counts()
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_in_progress"
        assert result.errors[0].details == {"receipt_id": receipt_id}
        assert env.counts() == before
        assert len(_ADAPTER[0].requests) == 0


def test_output_name_suffix_names_both_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _research_answer_action(
            env.seeded["sheet_id"],
            idempotency_key="research_answer@sha256:suffix-name",
            output_name="brief_sources",
        )
        result = env.run(action)
        assert result.status == "completed", result.errors
        assert [output.name for output in result.outputs] == [
            "brief_sources",
            "brief_sources_sources",
        ]
        assert result.outputs[0].ref["role"] == "answer"
        assert result.outputs[1].ref["role"] == "sources"
        receipt = Receipt.model_validate(
            json.loads(
                env.project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
                ).fetchone()["body"]
            )
        )
        output_refs = {item.name: item.ref for item in receipt.outputs}
        assert output_refs["brief_sources"]["role"] == "answer"
        assert output_refs["brief_sources_sources"]["role"] == "sources"
        evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
        assert "model_rows_model_calls" in evidence

        replay = env.run(action)
        assert replay.status == "completed", replay.errors
        assert [output.name for output in replay.outputs] == [
            "brief_sources",
            "brief_sources_sources",
        ]
        assert len(_ADAPTER[0].requests) == 4


def test_subset_overwrite_receipt_uses_the_run_output_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert first.run_id is not None

        row_ids = [
            int(row["id"])
            for row in env.project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position, id",
                (env.seeded["sheet_id"],),
            ).fetchall()
        ]
        selected_row, untouched_row = row_ids
        answer_column_id = int(
            env.project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='answer'",
                (env.seeded["sheet_id"],),
            ).fetchone()["id"]
        )

        def result_snapshot(run_id: int, row_id: int) -> tuple[Any, ...] | None:
            row = env.project.db.execute(
                "SELECT * FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                (run_id, row_id, answer_column_id),
            ).fetchone()
            return None if row is None else tuple(row)

        untouched_result_before = result_snapshot(first.run_id, untouched_row)
        assert untouched_result_before is not None

        overwrite = _research_answer_action(
            env.seeded["sheet_id"],
            idempotency_key="research_answer@sha256:subset-overwrite",
        )
        overwrite["params"]["question"] = {
            "text": "Answer a changed question for this row."
        }
        overwrite["scope"]["row_ids"] = [selected_row]
        overwrite["replace_existing"] = True
        second = env.run(overwrite)

        assert second.status == "completed", second.errors
        assert second.run_id is not None and second.run_id != first.run_id
        assert second.receipt_id is not None
        receipt = Receipt.model_validate(
            json.loads(
                env.project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (second.receipt_id,)
                ).fetchone()["body"]
            )
        )
        assert receipt.run_id == second.run_id

        values = env.project.get_values(
            env.seeded["sheet_id"], answer_column_id, row_ids=row_ids
        )
        assert values[selected_row] == "Cited answer 3"

        def answer_heads() -> dict[int, int]:
            return {
                int(row["row_id"]): int(row["run_id"])
                for row in env.project.db.execute(
                    "SELECT row_id,run_id FROM cell_result_heads "
                    "WHERE column_id=? ORDER BY row_id",
                    (answer_column_id,),
                ).fetchall()
            }

        expected_heads = {
            selected_row: second.run_id,
            untouched_row: first.run_id,
        }
        assert answer_heads() == expected_heads
        assert result_snapshot(second.run_id, untouched_row) is None
        assert result_snapshot(first.run_id, untouched_row) == untouched_result_before

        calls_before_replay = len(_ADAPTER[0].requests)
        assert calls_before_replay == 6
        replay = env.run(overwrite)
        assert replay.status == "completed", replay.errors
        assert replay.run_id == second.run_id
        assert replay.receipt_id == second.receipt_id
        assert len(_ADAPTER[0].requests) == calls_before_replay
        assert answer_heads() == expected_heads


def test_all_rows_failed_writes_failed_receipt_and_replays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.executor import research_read

    async def fake_search(query: str) -> tuple[str, list[str]]:
        return "unused", []

    monkeypatch.setattr(research_read, "search_web", fake_search)
    router, adapter = _failing_router()
    project = Project.create(tmp_path / "failed.frisket", name="research failed")
    try:
        seeded = _seed(project, tmp_path)
        action = _research_answer_action(
            seeded["sheet_id"],
            idempotency_key="research_answer@sha256:all-failed",
        )
        failed = run_action_with_confirmation(
            project, action, project_id="project-research-answer", router=router
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "model_run_failed"
        assert len(adapter.requests) == 2
        failed_receipt = Receipt.model_validate(
            json.loads(
                project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (failed.receipt_id,)
                ).fetchone()["body"]
            )
        )
        assert failed_receipt.status == "failed"
        assert failed_receipt.errors[0].code == "model_run_failed"

        replay = run_action_with_confirmation(
            project, action, project_id="project-research-answer", router=router
        )
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert len(adapter.requests) == 2
    finally:
        project.close()


# The exact tool-JSON debris an owner-QA run persisted as an ANSWER value: a
# model's final turn emitting a tool call into the TEXT channel (dialect
# misfire), which pydantic-ai handed back as the str answer.
_TOOL_JSON_LEAK = json.dumps(
    {
        "tool": "fetch",
        "args": {
            "url": "https://en.wikipedia.org/wiki/List_of_heads_of_state_of_Libya"
        },
    }
)


class _MalformedFinalAdapter:
    """Scripts the loop so its FINAL answer turn is tool-JSON emitted as plain
    text. Keyed off request shape (tools present + whether an Observation was
    already returned), so it is independent of per-row call ordering. The same
    tool-bearing agent corrects the answer only when ``retry_recovers``."""

    def __init__(self, *, retry_recovers: bool) -> None:
        self.requests: list[LLMRequest] = []
        self.retry_recovers = retry_recovers

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        assert req.tools is not None
        messages = [str(message.get("content")) for message in req.messages]
        observed = any("Observation:" in content for content in messages)
        corrective = any(
            "Return the final answer as plain prose" in content for content in messages
        )
        if not observed:
            return LLMResponse(
                content=None,
                data=None,
                tool_calls=[
                    {
                        "name": "search",
                        "args": {"query": "official source"},
                        "id": f"call_{len(self.requests)}",
                    }
                ],
                tokens_in=20,
                tokens_out=8,
                cost=0.002,
                model=req.model,
            )
        text = (
            "Libya has been led by a succession of heads of state per the cited sources."
            if corrective and self.retry_recovers
            else _TOOL_JSON_LEAK
        )
        return LLMResponse(
            content=text,
            data=None,
            tokens_in=20,
            tokens_out=8,
            cost=0.002,
            model=req.model,
        )


class _StepBudgetExhaustAdapter:
    """Never finishes: every tool-bearing turn requests search again, so the
    loop hits its request limit (UsageLimitExceeded)."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        assert req.tools is not None
        return LLMResponse(
            content=None,
            data=None,
            tool_calls=[
                {
                    "name": "search",
                    "args": {"query": "keep searching"},
                    "id": f"call_{len(self.requests)}",
                }
            ],
            tokens_in=6,
            tokens_out=3,
            cost=0.001,
            model=req.model,
        )


def _run_with_scripted_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: Any,
    *,
    idempotency_key: str,
) -> tuple[Project, Any]:
    from frisket.engine.executor import research_read

    calls: list[str] = []

    async def fake_search(query: str) -> tuple[str, list[str]]:
        calls.append(query)
        url = f"https://example.test/research/source-{len(calls)}"
        return f"Source {len(calls)} says the claim is cited. {url}", [url]

    monkeypatch.setattr(research_read, "search_web", fake_search)
    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    project = Project.create(tmp_path / "scripted.frisket", name="research guard")
    seeded = _seed(project, tmp_path)
    action = _research_answer_action(
        seeded["sheet_id"], idempotency_key=idempotency_key
    )
    result = run_action_with_confirmation(
        project, action, project_id="project-research-answer", router=router
    )
    return project, result


def _answer_cells(project: Project, run_id: int) -> list[Any]:
    column = project.db.execute(
        "SELECT id FROM columns WHERE current_run_id=? AND name='answer'", (run_id,)
    ).fetchone()
    return project.db.execute(
        "SELECT value, error, error_code, outcome FROM results "
        "WHERE run_id=? AND column_id=? ORDER BY row_id",
        (run_id, int(column["id"])),
    ).fetchall()


def test_tool_json_final_turn_is_corrected_by_output_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _MalformedFinalAdapter(retry_recovers=True)
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:malformed-recovered",
    )
    try:
        assert result.status == "completed", result.errors
        for cell in _answer_cells(project, result.run_id):
            assert cell["value"] != _TOOL_JSON_LEAK
            assert '"tool"' not in str(cell["value"])
            assert cell["outcome"] == "ok"
            assert cell["error"] is None
        # Two rows, each making search -> malformed answer -> corrected answer.
        assert (
            len(
                projected := project.db.execute(
                    "SELECT provider_cost_usd FROM model_calls WHERE run_id=?",
                    (result.run_id,),
                ).fetchall()
            )
            == 6
        )
        assert sum(
            float(row["provider_cost_usd"]) for row in projected
        ) == pytest.approx(0.012)
        assert len(adapter.requests) == 6
        corrective = [
            req
            for req in adapter.requests
            if any(
                "Return the final answer as plain prose" in str(message.get("content"))
                for message in req.messages
            )
        ]
        assert len(corrective) == 2
        assert all(
            any(
                "Observation:" in str(message.get("content"))
                for message in req.messages
            )
            for req in corrective
        )
    finally:
        project.close()


def test_tool_json_final_turn_fails_typed_when_retry_also_misbehaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _MalformedFinalAdapter(retry_recovers=False)
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:malformed-failed",
    )
    try:
        # Every row failed the same way -> action-level failure, never debris.
        assert result.status == "failed"
        assert result.errors[0].code == "model_run_failed"
        for cell in _answer_cells(project, result.run_id):
            assert cell["value"] is None
            assert cell["outcome"] == "row_error"
            assert cell["error_code"] == "research_answer_malformed"
            assert _TOOL_JSON_LEAK not in str(cell["error"])
        assert len(adapter.requests) == 6
        assert all(request.tools is not None for request in adapter.requests)
    finally:
        project.close()


def test_step_budget_exhaustion_does_not_synthesize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _StepBudgetExhaustAdapter()
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:exhausted-recovered",
    )
    try:
        assert result.status == "failed"
        for cell in _answer_cells(project, result.run_id):
            assert cell["value"] is None
            assert cell["outcome"] == "row_error"
            assert cell["error_code"] == "research_incomplete_step_budget"
        assert len(adapter.requests) == 10
        assert all(req.tools is not None for req in adapter.requests)
    finally:
        project.close()


class _MemoryAnswerAdapter:
    """Answers straight from parametric memory (plain text, no tool call, no
    search). On the verification nudge it grounds via search only when
    ``ground_on_nudge`` — otherwise it answers from memory again."""

    def __init__(
        self,
        *,
        ground_on_nudge: bool,
        malformed_after_observation: bool = False,
        retry_recovers: bool = True,
        empty_after_observation: bool = False,
    ) -> None:
        self.requests: list[LLMRequest] = []
        self.ground_on_nudge = ground_on_nudge
        self.malformed_after_observation = malformed_after_observation
        self.retry_recovers = retry_recovers
        self.empty_after_observation = empty_after_observation

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        contents = [str(m.get("content")) for m in req.messages]
        nudged = any("verify your" in c for c in contents)
        observed = any("Observation:" in c for c in contents)
        corrective = any(
            "Return the final answer as plain prose" in c for c in contents
        )
        if nudged and self.ground_on_nudge and not observed:
            return LLMResponse(
                content=None,
                data=None,
                tool_calls=[
                    {"name": "search", "args": {"query": "head of state"}, "id": "c"}
                ],
                tokens_in=5,
                tokens_out=2,
                cost=0.001,
                model=req.model,
            )
        if observed and self.empty_after_observation:
            text = ""
        elif (
            observed
            and self.malformed_after_observation
            and (not corrective or not self.retry_recovers)
        ):
            text = _TOOL_JSON_LEAK
        elif observed:
            text = "Grounded answer citing the source."
        else:
            text = "Answer straight from model memory (no source consulted)."
        return LLMResponse(
            content=text,
            data=None,
            tokens_in=5,
            tokens_out=2,
            cost=0.001,
            model=req.model,
        )


def test_uncited_memory_answer_is_kept_but_marked_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        _MemoryAnswerAdapter(ground_on_nudge=False),
        idempotency_key="research_answer@sha256:memory-unverified",
    )
    try:
        # "mark, don't destroy": the run still completes and the answer is KEPT.
        assert result.status == "completed", result.errors
        for cell in _answer_cells(project, result.run_id):
            assert cell["value"] is not None
            assert cell["outcome"] == "unverified_memory"
        sources_col = project.db.execute(
            "SELECT id, sheet_id FROM columns "
            "WHERE current_run_id=? AND name='answer_sources'",
            (result.run_id,),
        ).fetchone()
        sources = project.get_values(
            int(sources_col["sheet_id"]), int(sources_col["id"])
        )
        assert all(v == [] for v in sources.values())
    finally:
        project.close()


def test_verification_nudge_grounds_a_memory_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _MemoryAnswerAdapter(
        ground_on_nudge=True, malformed_after_observation=True
    )
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:memory-grounded",
    )
    try:
        assert result.status == "completed", result.errors
        for cell in _answer_cells(project, result.run_id):
            # nudge grounded it -> normal ok outcome, no unverified marker.
            assert cell["outcome"] == "ok"
            assert json.loads(cell["value"]) == "Grounded answer citing the source."
        # The verification run uses the same validator: per row it performs
        # memory answer -> search -> malformed text -> corrected prose.
        assert len(adapter.requests) == 8
        assert (
            sum(
                any(
                    "Return the final answer as plain prose"
                    in str(message.get("content"))
                    for message in request.messages
                )
                for request in adapter.requests
            )
            == 2
        )
        model_calls = project.db.execute(
            "SELECT provider_cost_usd FROM model_calls WHERE run_id=?",
            (result.run_id,),
        ).fetchall()
        assert len(model_calls) == 8
        assert sum(float(call["provider_cost_usd"]) for call in model_calls) == (
            pytest.approx(0.008)
        )
    finally:
        project.close()


def test_repeated_malformed_verification_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _MemoryAnswerAdapter(
        ground_on_nudge=True,
        malformed_after_observation=True,
        retry_recovers=False,
    )
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:memory-malformed",
    )
    try:
        assert result.status == "failed"
        for cell in _answer_cells(project, result.run_id):
            assert cell["value"] is None
            assert cell["outcome"] == "row_error"
            assert cell["error_code"] == "research_answer_malformed"
        assert len(adapter.requests) == 8
        model_calls = project.db.execute(
            "SELECT provider_cost_usd FROM model_calls WHERE run_id=?",
            (result.run_id,),
        ).fetchall()
        assert len(model_calls) == 8
        assert sum(float(call["provider_cost_usd"]) for call in model_calls) == (
            pytest.approx(0.008)
        )
    finally:
        project.close()


def test_failed_verification_does_not_ground_the_original_memory_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _MemoryAnswerAdapter(
        ground_on_nudge=True,
        empty_after_observation=True,
    )
    project, result = _run_with_scripted_adapter(
        tmp_path,
        monkeypatch,
        adapter,
        idempotency_key="research_answer@sha256:memory-verification-empty",
    )
    try:
        assert result.status == "completed", result.errors
        for cell in _answer_cells(project, result.run_id):
            assert json.loads(cell["value"]) == (
                "Answer straight from model memory (no source consulted)."
            )
            assert cell["outcome"] == "unverified_memory"
        assert len(adapter.requests) == 8
    finally:
        project.close()

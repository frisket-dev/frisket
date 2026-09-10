from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.pricing import cost_of_with_source
from frisket.authoring.actions import run_trace, run_trace_row
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.operability.trace import read_trace, read_trace_row, trace_path


class _SummaryAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        prompt = str(req.messages[-1]["content"])
        summary = (
            "Accountability stories share contracting risk."
            if "accountability" in prompt
            else "Infrastructure stories share service disruption risk."
        )
        return LLMResponse(
            content=summary,
            data=None,
            tokens_in=83,
            tokens_out=17,
            cost=0.006,
            model=req.model,
        )


class _FailingAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        from frisket.ai.llm import LLMError

        self.requests.append(req)
        raise LLMError("summary provider unavailable", status=503, retryable=False)


class _UnknownCostSummaryAdapter(_SummaryAdapter):
    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        response = await super().complete(req, client)
        # A live provider can return usage without a price (for example when
        # the selected model is absent from the local price card).  The router
        # preserves that distinction as a NULL durable provider cost.
        response.cost = None  # type: ignore[assignment]
        return response


class _PricedSummaryAdapter(OpenAICompatAdapter):
    """Summary double whose amount and source come from the pricing seam."""

    def __init__(self) -> None:
        super().__init__("test", "http://127.0.0.1:11434/v1")
        self.requests: list[LLMRequest] = []

    async def complete(  # noqa: ANN001
        self, req: LLMRequest, client, *, cost_model: str | None = None
    ) -> LLMResponse:
        del client
        self.requests.append(req)
        prompt = str(req.messages[-1]["content"])
        summary = (
            "Accountability stories share contracting risk."
            if "accountability" in prompt
            else "Infrastructure stories share service disruption risk."
        )
        cost, cost_source = cost_of_with_source(
            cost_model or req.model,
            83,
            17,
        )
        return LLMResponse(
            content=summary,
            data=None,
            tokens_in=83,
            tokens_out=17,
            cost=cost,
            cost_source=cost_source,
            model=req.model,
        )


def _summary_router() -> tuple[ModelRouter, _SummaryAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _SummaryAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _failing_router() -> tuple[ModelRouter, _FailingAdapter]:
    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    adapter = _FailingAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


# Reset by _patch_router at the start of every harness test for this case;
# asserts on it are only meaningful behind that patch.
_ADAPTERS: list[_SummaryAdapter] = []


def _patch_router(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    del monkeypatch
    router, adapter = _summary_router()
    _ADAPTERS.clear()
    _ADAPTERS.append(adapter)
    return {"router": router}


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Feature Tour")
    columns = {
        "story": project.add_column(sheet_id, "story", type="text"),
        "beat": project.add_column(sheet_id, "beat", type="text"),
        "risk_score": project.add_column(sheet_id, "risk_score", type="integer"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "story": "City hall awarded a no-bid software contract",
                "beat": "accountability",
                "risk_score": 8,
            },
            {
                "story": "Audit found duplicate vendor payments",
                "beat": "accountability",
                "risk_score": 9,
            },
            {
                "story": "Water main repairs closed two blocks",
                "beat": "infrastructure",
                "risk_score": 4,
            },
        ],
        columns,
    )
    return {
        "sheet_id": sheet_id,
        "seeded_op_id": project.op_cursor,
        "source_row_ids": [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
            ).fetchall()
        ],
    }


def _reduce_action(
    sheet_id: int,
    *,
    idempotency_key: str = "reduce_summary@sha256:stable",
    model: str = "anthropic/claude-haiku-4-5",
) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "sheet_name": "Beat Summaries",
        "output_names": {},
        "params": {
            "source": ["story", "risk_score"],
            "group_by": "beat",
            "model": model,
            "instruction": "Summarize common reporting risks for each beat.",
        },
        "idempotency_key": idempotency_key,
    }


def _bound_request(action: dict[str, Any]):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest

    return BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("reduce.group_summary"),
        ActionRequest.model_validate(action),
    )


def test_semantic_sources_admit_group_by_without_rewriting_saved_params(
    tmp_path: Path,
) -> None:
    from frisket.actions.types import discover_references
    from frisket.engine.executor.group_summary_action import (
        GroupSummaryRefused,
        prepare_group_summary_action,
    )

    project = Project.create(tmp_path / "reduce-inputs.frisket", name="reduce-inputs")
    try:
        seeded = _seed(project, tmp_path)
        action = _reduce_action(seeded["sheet_id"])
        bound = _bound_request(action)
        assert {
            reference.column for reference in discover_references(bound.params)
        } == {
            "story",
            "risk_score",
            "beat",
        }
        assert bound.params.model_dump()["source"] == ["story", "risk_score"]
        assert bound.params.model_dump()["group_by"] == "beat"
        prepared = prepare_group_summary_action(project, bound)
        resolved = prepared.resolved
        assert set(resolved["source_columns"]) == {"story", "risk_score"}
        assert resolved["group_column"]["name"] == "beat"
        assert [group["name"] for group in resolved["groups"]] == [
            "accountability",
            "infrastructure",
        ]
        for param, value, expected_field in (
            ("source", ["missing"], "params.source"),
            ("group_by", "missing", "params.group_by"),
        ):
            missing_action = _reduce_action(seeded["sheet_id"])
            missing_action["params"][param] = value
            with pytest.raises(GroupSummaryRefused) as caught:
                prepare_group_summary_action(project, _bound_request(missing_action))
            assert caught.value.error.code == "invalid_input_ref"
            assert caught.value.error.field == expected_field
            assert caught.value.error.details == {"missing": ["missing"]}
    finally:
        project.close()


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _reduce_action(seeded["sheet_id"])


def _missing_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _reduce_action(
        seeded["sheet_id"], idempotency_key="reduce_summary@sha256:missing-capability"
    )
    action["capabilities"] = ["project:write"]
    return action


def _bad_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _reduce_action(
        seeded["sheet_id"], idempotency_key="reduce_summary@sha256:bad-input"
    )
    action["params"]["source"] = ["missing"]
    return action


def _bad_rows_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _reduce_action(
        seeded["sheet_id"], idempotency_key="reduce_summary@sha256:bad-rows"
    )
    action["scope"]["row_ids"] = [seeded["source_row_ids"][0], 999_999]
    return action


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary run, different instruction.
    action = _reduce_action(seeded["sheet_id"])
    action["params"]["instruction"] = "Summarize in a different style."
    return action


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _reduce_action(
        seeded["sheet_id"], idempotency_key="reduce_summary@sha256:duplicate-sheet"
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    source_row_ids = seeded["source_row_ids"]
    adapter = _ADAPTERS[0]
    assert len(adapter.requests) == 2
    assert result.run_id == 1
    assert result.op_ids == [seeded["seeded_op_id"] + 1]
    assert {output.kind for output in result.outputs} == {"sheet", "column", "rows"}
    assert {output.name for output in result.outputs} >= {
        "Beat Summaries",
        "group",
        "rows",
        "summary",
    }

    run = project.db.execute("SELECT * FROM runs WHERE id=1").fetchone()
    assert run["action_kind"] == "reduce.group_summary"
    assert run["model"] == "anthropic/claude-haiku-4-5"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0
    assert run["cost_actual"] == 0.012

    summary_sheet = project.db.execute(
        "SELECT * FROM sheets WHERE name='Beat Summaries'"
    ).fetchone()
    assert summary_sheet["parent_sheet_id"] == sheet_id
    assert summary_sheet["parent_op_id"] == result.op_ids[0]
    summary_sheet_id = int(summary_sheet["id"])
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (summary_sheet_id,),
        ).fetchall()
    }
    assert columns["group"]["type"] == "text"
    assert columns["rows"]["type"] == "integer"
    assert columns["summary"]["type"] == "text"
    assert columns["summary"]["ai_generated"] == 1
    assert columns["summary"]["current_run_id"] == result.run_id

    summary_rows = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
            (summary_sheet_id,),
        ).fetchall()
    ]
    assert len(summary_rows) == 2
    assert project.get_values(summary_sheet_id, int(columns["group"]["id"])) == {
        summary_rows[0]: "accountability",
        summary_rows[1]: "infrastructure",
    }
    assert project.get_values(summary_sheet_id, int(columns["rows"]["id"])) == {
        summary_rows[0]: 2,
        summary_rows[1]: 1,
    }
    assert project.get_values(summary_sheet_id, int(columns["summary"]["id"])) == {
        summary_rows[0]: "Accountability stories share contracting risk.",
        summary_rows[1]: "Infrastructure stories share service disruption risk.",
    }
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=? AND column_id=?",
            (result.run_id, int(columns["summary"]["id"])),
        ).fetchone()[0]
        == 2
    )
    generation = project.db.execute(
        "SELECT state,terminal_disposition FROM run_output_generations "
        "WHERE run_id=? AND column_id=?",
        (result.run_id, int(columns["summary"]["id"])),
    ).fetchone()
    assert dict(generation) == {
        "state": "sealed",
        "terminal_disposition": "completed",
    }
    assert {
        int(row["row_id"]): int(row["run_id"])
        for row in project.db.execute(
            "SELECT row_id,run_id FROM cell_result_heads WHERE column_id=?",
            (int(columns["summary"]["id"]),),
        )
    } == {row_id: result.run_id for row_id in summary_rows}

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 2
    assert {row["provider"] for row in model_calls} == {"anthropic"}
    assert {row["engine"] for row in model_calls} == {"anthropic/claude-haiku-4-5"}
    assert {row["provider_cost_usd"] for row in model_calls} == {0.006}
    # The router measures every live wire call; a group-summary fact must
    # carry that measurement rather than a fabricated NULL.
    for row in model_calls:
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row["run_id"] == result.run_id
    assert receipt_row["action_kind"] == "reduce.group_summary"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.run_id == result.run_id
    assert receipt.provider_use
    assert receipt.provider_use[0]["provider"] == "anthropic"
    assert receipt.provider_use[0]["model_call_count"] == 2
    assert receipt.provider_use[0]["cost_actual"] == 0.012
    assert {item.ref["kind"] for item in receipt.inputs} >= {
        "reduce_group_summary_source_rows",
        "reduce_group_summary_input_column",
        "reduce_group_summary_group_column",
    }
    assert {item.ref["kind"] for item in receipt.outputs} >= {
        "materialized_sheet",
        "materialized_rows",
        "materialized_column",
        "reduce_summary_output_column",
    }
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "reduce_group_summary_groups",
        "reduce_group_summary_output_roles",
        "reduce_group_summary_model_calls",
        "reduce_group_summary_prompt",
    }
    group_ref = next(
        item.ref
        for item in receipt.evidence
        if item.ref["kind"] == "reduce_group_summary_groups"
    )
    assert group_ref["groups"] == [
        {
            "name": "accountability",
            "source_row_ids": source_row_ids[:2],
            "summary_row_id": summary_rows[0],
            "source_row_count": 2,
        },
        {
            "name": "infrastructure",
            "source_row_ids": source_row_ids[2:],
            "summary_row_id": summary_rows[1],
            "source_row_count": 1,
        },
    ]


CASES = [
    ExecutorCase(
        kind="reduce.group_summary",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="grouped",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_model_router",
                    "create_sheet",
                    "write_model_calls",
                    "write_trace",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "model_cost_requires_confirmation",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "idempotency_stale_running",
                    "project_write_failed",
                    "stale_input",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_router,
        gates=(
            Gate(
                "forged_capabilities",
                _missing_capability_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_input_ref_bad_column",
                _bad_input_action,
                "invalid_input_ref",
            ),
            Gate(
                "invalid_input_ref_bad_rows",
                _bad_rows_action,
                "invalid_input_ref",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_params_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 3,
            "rows": 2,
            "runs": 1,
            "results": 2,
            "model_calls": 2,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


def test_replay_and_gates_never_recall_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert len(_ADAPTERS[0].requests) == 2

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_ADAPTERS[0].requests) == 2

        conflict = env.run(_conflicting_params_action(env.seeded))
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert len(_ADAPTERS[0].requests) == 2


def test_unconfirmed_model_cost_blocks_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost gate surfaces as needs_confirmation (the 402 envelope), not a
    plain failure, and nothing is written or billed."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        unconfirmed = _reduce_action(
            env.seeded["sheet_id"], idempotency_key="reduce_summary@sha256:cost-gate"
        )
        unconfirmed["params"]["model"] = "unknown/frontier"
        before = env.counts()
        cost_gate = env.run_once(unconfirmed)
        assert cost_gate.status == "needs_confirmation"
        assert cost_gate.errors[0].code == "model_cost_requires_confirmation"
        assert env.counts() == before
        assert _ADAPTERS[0].requests == []


def test_model_cost_gate_binds_full_estimate_and_scope_to_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        unconfirmed = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:bound-context",
        )
        unconfirmed["scope"]["row_ids"] = env.seeded["source_row_ids"]
        before = env.counts()

        cost_gate = env.run_once(unconfirmed)

        assert cost_gate.status == "needs_confirmation"
        details = cost_gate.errors[0].details
        estimate = details["estimate"]
        assert estimate["groups"] == 2
        assert estimate["avg_input_tokens"] > 0
        assert "cost" in estimate
        assert estimate["cost_source"] == "pricing_data"
        assert estimate["pricing_key"] == "anthropic/claude-haiku-4-5.tokens"
        # Bare-hex quote hash; the spelling closure lives in
        # test_confirmation_echo_gate_closure.py.
        assert len(details["promise_set_hash"]) == 64
        assert env.counts() == before
        assert _ADAPTERS[0].requests == []


@pytest.mark.parametrize(
    ("model", "has_cost", "source", "key"),
    [
        ("ollama/@desk/unlisted", True, "free_local", None),
        (
            "anthropic/claude-haiku-4-5",
            True,
            "pricing_data",
            "anthropic/claude-haiku-4-5.tokens",
        ),
        ("anthropic/unpriced", False, "unknown", None),
    ],
)
def test_reduce_quote_preserves_model_rate_provenance(
    model: str,
    has_cost: bool,
    source: str,
    key: str | None,
) -> None:
    from frisket.engine.executor.group_summary_plan import GroupSummaryPlan
    from frisket.engine.executor.group_summary_runtime import (
        _estimate_reduce_group_summary_cost,
    )

    params = GroupSummaryPlan(
        action_kind="reduce.group_summary",
        request={},
        group_by=None,
        row_ids=None,
        group_column_name="group",
        row_count_column_name="rows",
        summary_column_name="summary",
        sheet_id=1,
        input_columns=["story"],
        model=model,
        instruction="Summarize.",
        target_sheet_name="Summaries",
    )
    estimate = _estimate_reduce_group_summary_cost(
        params,
        {"groups": [{"name": "news", "source_row_ids": [1], "rows": [{"story": "x"}]}]},
    )
    assert (estimate["cost"] is not None) is has_cost
    if source == "free_local":
        assert estimate["cost"] == 0.0
    assert estimate["cost_source"] == source
    assert estimate.get("pricing_key") == key


def test_model_child_sheet_stale_scope_hash_repauses_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first_scope = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:stale-context",
        )
        first_scope["params"]["model"] = "unknown/frontier"
        first_scope["scope"]["row_ids"] = [env.seeded["source_row_ids"][0]]
        before = env.counts()
        quote = env.run_once(first_scope)

        assert quote.status == "needs_confirmation"
        quoted_hash = quote.errors[0].details["promise_set_hash"]
        assert len(quoted_hash) == 64

        stale_retry = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:stale-context",
        )
        stale_retry["params"]["model"] = "unknown/frontier"
        stale_retry["scope"]["row_ids"] = env.seeded["source_row_ids"]
        stale_retry["confirmation"] = quoted_hash

        stale = env.run_once(stale_retry)

        assert stale.status == "needs_confirmation"
        assert stale.errors[0].code == "model_cost_requires_confirmation"
        assert stale.errors[0].details["promise_set_hash"] != quoted_hash
        assert env.counts() == before
        assert _ADAPTERS[0].requests == []


def test_model_child_sheet_stale_resolved_rows_repauses_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consent for a dynamic all-visible-rows scope cannot authorize a later
    row set merely because its aggregate cost quote happens to be identical."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:stale-resolved-rows",
        )

        quote = env.run_once(action)

        assert quote.status == "needs_confirmation"
        quoted_details = quote.errors[0].details
        quoted_hash = quoted_details["promise_set_hash"]
        assert _ADAPTERS[0].requests == []

        # Replace one visible row with a different row whose prompt payload is
        # exactly the same length. The numeric estimate therefore remains the
        # same while both the row identity and the content being purchased
        # change under the unchanged authored all-visible-rows action.
        replaced_row_id = env.seeded["source_row_ids"][-1]
        env.project.db.execute(
            "UPDATE rows SET hidden=1 WHERE id=?",
            (replaced_row_id,),
        )
        columns = {
            str(row["name"]): int(row["id"])
            for row in env.project.db.execute(
                "SELECT id, name FROM columns WHERE sheet_id=?",
                (env.seeded["sheet_id"],),
            ).fetchall()
        }
        env.project.add_rows(
            env.seeded["sheet_id"],
            [
                {
                    "story": "Power grid repairs closed two blocks",
                    "beat": "infrastructure",
                    "risk_score": 4,
                }
            ],
            columns,
        )

        stale_retry = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:stale-resolved-rows",
        )
        stale_retry["confirmation"] = quoted_hash
        stale = env.run_once(stale_retry)

        assert stale.status == "needs_confirmation"
        assert stale.errors[0].code == "model_cost_requires_confirmation"
        fresh_details = stale.errors[0].details
        fresh_hash = fresh_details["promise_set_hash"]
        assert fresh_hash != quoted_hash
        # This is specifically an ownership/scope failure, not a price change.
        quoted_estimate = quoted_details["estimate"]
        fresh_estimate = fresh_details["estimate"]
        assert quoted_estimate["resolved_prompt_hash"].startswith("sha256:")
        assert fresh_estimate["resolved_prompt_hash"].startswith("sha256:")
        assert (
            fresh_estimate["resolved_prompt_hash"]
            != quoted_estimate["resolved_prompt_hash"]
        )
        assert {
            key: value
            for key, value in fresh_estimate.items()
            if key != "resolved_prompt_hash"
        } == {
            key: value
            for key, value in quoted_estimate.items()
            if key != "resolved_prompt_hash"
        }
        assert _ADAPTERS[0].requests == []

        # Echoing the freshly resolved quote performs exactly that work. The
        # ownership digest is confirmation metadata, not provider prompt text
        # or receipt evidence.
        stale_retry["confirmation"] = fresh_hash
        completed = env.run_once(stale_retry)

        assert completed.status == "completed", completed.errors
        assert len(_ADAPTERS[0].requests) == 2
        assert all(
            fresh_estimate["resolved_prompt_hash"]
            not in json.dumps(request.messages, sort_keys=True)
            for request in _ADAPTERS[0].requests
        )
        receipt_body = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?",
            (completed.receipt_id,),
        ).fetchone()["body"]
        assert fresh_estimate["resolved_prompt_hash"] not in receipt_body


def test_model_child_sheet_missing_confirmation_repauses_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        omitted_echo = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:omitted-context",
        )
        omitted_echo["params"]["model"] = "unknown/frontier"
        omitted_echo["scope"]["row_ids"] = env.seeded["source_row_ids"]
        before = env.counts()

        regated = env.run_once(omitted_echo)

        assert regated.status == "needs_confirmation"
        assert regated.errors[0].code == "model_cost_requires_confirmation"
        assert len(regated.errors[0].details["promise_set_hash"]) == 64
        assert env.counts() == before
        assert _ADAPTERS[0].requests == []


def test_reduce_group_summary_refuses_an_exhausted_project_key_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct child-sheet model body must enforce the same project-key
    spend cap as MapRunner.  A quote/confirmation gate is not a budget gate:
    once the key is exhausted, even a confirmed grouped reduce refuses before
    its first per-group request."""
    from frisket.ai.llm import ModelRouter
    from frisket.team.security.secrets import encrypt_secret, key_hint

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        env.project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("k"),
            hint=key_hint("k"),
            spend_cap_micro=0,
        )
        router = ModelRouter(
            keys={"anthropic": "k"},
            key_sources={"anthropic": "project_key"},
            cache=None,
            cache_mode="off",
        )
        adapter = _SummaryAdapter()
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        env.run_kwargs["router"] = router

        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"],
                idempotency_key="reduce_summary@sha256:exhausted-project-key",
            )
        )

        assert result.status == "failed"
        assert result.run_id is None
        assert result.errors[0].code == "provider_spend_cap_exceeded"
        assert adapter.requests == []


def test_unknown_live_call_cost_never_becomes_exact_zero_in_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt aggregates come from durable call facts, and the run column is
    a projection of the same facts.  One live call with unknown price makes
    every cost surface unknown — receipt aggregates AND ``runs.cost_actual``
    — never an exact zero.
    """
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        adapter = _UnknownCostSummaryAdapter()
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        env.run_kwargs["router"] = router

        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"],
                idempotency_key="reduce_summary@sha256:unknown-live-cost",
                model="anthropic/frisket-unpriced",
            )
        )

        assert result.status == "completed", result.errors
        calls = RunResultStore(env.project).model_calls(result.run_id)
        assert calls
        assert all(call["credential_source"] != "cache" for call in calls)
        assert all(call["provider_cost_usd"] is None for call in calls)
        assert {call["cost_source"] for call in calls} == {"unknown"}
        # The projection keeps the run figure honest: unknown, not $0.
        run = env.project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run["cost_actual"] is None

        receipt = Receipt.model_validate_json(
            env.project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
        assert receipt.provider_use[0]["cost_actual"] is None
        run_counts = next(
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "reduce_group_summary_run_counts"
        )
        assert run_counts["cost_actual"] is None

        def cost_claims(value: Any) -> list[Any]:
            if isinstance(value, dict):
                return [
                    *([value["cost_actual"]] if "cost_actual" in value else []),
                    *[
                        claim
                        for child in value.values()
                        for claim in cost_claims(child)
                    ],
                ]
            if isinstance(value, list):
                return [claim for child in value for claim in cost_claims(child)]
            return []

        assert cost_claims(receipt.model_dump(mode="json"))
        assert 0.0 not in cost_claims(receipt.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("model", "expected_source", "expected_cost_known"),
    [
        ("ollama/@test-local/frisket-unlisted", "free_local", True),
        ("anthropic/claude-haiku-4-5", "pricing_data", True),
        ("anthropic/frisket-unpriced", "unknown", False),
    ],
)
def test_reduce_group_summary_persists_pricing_source_by_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_source: str,
    expected_cost_known: bool,
) -> None:
    adapter = _PricedSummaryAdapter()
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

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        env.run_kwargs["router"] = router
        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"],
                idempotency_key=f"reduce_summary@sha256:{expected_source}",
                model=model,
            )
        )

        assert result.status == "completed", result.errors
        calls = RunResultStore(env.project).model_calls(result.run_id)
        assert len(calls) == 2
        assert {call["cost_source"] for call in calls} == {expected_source}
        assert all(
            (call["provider_cost_usd"] is not None) is expected_cost_known
            for call in calls
        )


@pytest.mark.parametrize(
    ("model", "execution_path"),
    [
        ("ollama/@test-local/frisket-unlisted", "free"),
        ("anthropic/claude-haiku-4-5", "checkpointed"),
    ],
)
def test_reduce_group_summary_traces_free_and_checkpointed_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    execution_path: str,
) -> None:
    if model.startswith("ollama/"):
        adapter: Any = _PricedSummaryAdapter()
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
        router, adapter = _summary_router()

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        env.run_kwargs["router"] = router
        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"],
                idempotency_key=f"reduce_summary@sha256:trace-{execution_path}",
                model=model,
            )
        )

        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 2
        path = trace_path(env.project.path, result.run_id)
        assert path.read_bytes().startswith(b"\x1f\x8b")
        assert list(path.parent.glob(f"run-{result.run_id}.jsonl.gz")) == [path]
        trace = read_trace(env.project.path, result.run_id)
        assert trace is not None
        assert trace["action_kind"] == "reduce.group_summary"
        assert len(trace["rows"]) == 2
        summary_rows = next(
            output.row_ids for output in result.outputs if output.kind == "rows"
        )
        summary_column_id = next(
            output.column_id
            for output in result.outputs
            if output.ref["kind"] == "reduce_summary_output_column"
        )
        assert {row["row_id"] for row in trace["rows"]} == set(summary_rows)
        assert all(len(row["calls"]) == 1 for row in trace["rows"])
        for row_id in summary_rows:
            stored = read_trace_row(env.project.path, result.run_id, row_id)
            assert stored is not None
            assert stored["record_count"] == 1
            assert stored["row"]["row_id"] == row_id
            explained_row = run_trace_row(
                env.project_id,
                env.project,
                result.run_id,
                row_id,
                column_id=summary_column_id,
            )
            assert explained_row["recorded"] is True
            assert explained_row["trace"]["row"]["row_id"] == row_id
            assert explained_row["cell"]["error"] is None
        explained = run_trace(env.project_id, env.project, result.run_id)
        assert explained["recorded"] is True
        assert explained["trace"]["action_kind"] == "reduce.group_summary"
        assert len(explained["trace"]["rows"]) == 2


def test_model_failure_writes_failed_receipt_and_replays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        failing_router, failing_adapter = _failing_router()
        env.run_kwargs["router"] = failing_router
        action = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:model-failure",
        )
        failing_result = env.run(action)
        assert failing_result.status == "failed"
        assert failing_result.errors[0].code == "model_run_failed"
        assert len(failing_adapter.requests) == 2
        receipt = Receipt.model_validate(
            json.loads(
                env.project.db.execute(
                    "SELECT body FROM receipts WHERE id=?",
                    (failing_result.receipt_id,),
                ).fetchone()["body"]
            )
        )
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "model_run_failed"
        path = trace_path(env.project.path, failing_result.run_id)
        assert path.read_bytes().startswith(b"\x1f\x8b")
        trace = read_trace(env.project.path, failing_result.run_id)
        assert trace is not None
        assert len(trace["rows"]) == 2
        assert {row["error"] for row in trace["rows"]} == {
            "summary provider unavailable"
        }
        assert all(
            "summary provider unavailable" in row["calls"][0]["error"]
            for row in trace["rows"]
        )
        failed_row_id = next(
            output.row_ids[0]
            for output in failing_result.outputs
            if output.kind == "rows"
        )
        summary_column_id = next(
            output.column_id
            for output in failing_result.outputs
            if output.ref["kind"] == "reduce_summary_output_column"
        )
        failed_row_trace = read_trace_row(
            env.project.path, failing_result.run_id, failed_row_id
        )
        assert failed_row_trace is not None
        assert failed_row_trace["row"]["row_id"] == failed_row_id
        failed_explanation = run_trace_row(
            env.project_id,
            env.project,
            failing_result.run_id,
            failed_row_id,
            column_id=summary_column_id,
        )
        assert failed_explanation["recorded"] is True
        assert failed_explanation["cell"]["value"] is None
        assert "summary provider unavailable" in failed_explanation["cell"]["error"]
        assert run_trace(env.project_id, env.project, failing_result.run_id)["recorded"]

        replay = env.run(action)
        assert replay.status == "failed"
        assert replay.receipt_id == failing_result.receipt_id
        assert len(failing_adapter.requests) == 2


def test_provider_error_trace_survives_without_a_materialized_summary_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import group_summary_runtime

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        failing_router, failing_adapter = _failing_router()
        env.run_kwargs["router"] = failing_router

        def refuse_materialization(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("summary materialization unavailable")

        monkeypatch.setattr(
            group_summary_runtime, "write_aggregate_sheet", refuse_materialization
        )
        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"],
                idempotency_key="reduce_summary@sha256:failed-materialization-trace",
            )
        )

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert len(failing_adapter.requests) == 2
        run_id = int(
            env.project.db.execute(
                "SELECT id FROM runs WHERE action_kind='reduce.group_summary'"
            ).fetchone()["id"]
        )
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Beat Summaries'"
            ).fetchone()[0]
            == 0
        )
        trace = read_trace(env.project.path, run_id)
        assert trace is not None
        assert len(trace["rows"]) == 2
        assert {row["row_id"] for row in trace["rows"]} == {None}
        assert {row["error"] for row in trace["rows"]} == {
            "summary provider unavailable"
        }


def _insert_running_reservation(
    env: Any, action: dict[str, Any], *, created_at: str | None = None
) -> str:
    from frisket.contracts.action import (
        Receipt,
        ReceiptIO,
    )
    from frisket.engine.executor.map_rows_action import typed_request_hash

    params_hash = typed_request_hash(_bound_request(action))
    receipt_id = "receipt-reduce-running"
    running_receipt = Receipt(
        receipt_id=receipt_id,
        project_id=env.project_id,
        action_id="act-reduce-running",
        action_kind="reduce.group_summary",
        idempotency_key=str(action["idempotency_key"]),
        params_hash=params_hash,
        status="running",
        inputs=[
            ReceiptIO(
                name="idempotency",
                ref={
                    "kind": "reduce_group_summary_idempotency_reservation",
                    "params_hash": params_hash,
                },
            )
        ],
    )
    columns = "id, action_kind, action_id, idempotency_key, params_hash, status, body"
    placeholders = "?, ?, ?, ?, ?, ?, ?"
    values: list[Any] = [
        running_receipt.receipt_id,
        running_receipt.action_kind,
        running_receipt.action_id,
        running_receipt.idempotency_key,
        running_receipt.params_hash,
        running_receipt.status,
        json.dumps(running_receipt.model_dump(mode="json"), sort_keys=True),
    ]
    if created_at is not None:
        columns += ", created_at"
        placeholders += ", ?"
        values.append(created_at)
    env.project.db.execute(
        f"INSERT INTO receipts ({columns}) VALUES ({placeholders})", values
    )
    env.project.db.commit()
    return receipt_id


def test_running_reservation_blocks_duplicate_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:running-reservation",
        )
        receipt_id = _insert_running_reservation(env, action)
        before = env.counts()
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_in_progress"
        assert result.errors[0].details == {"receipt_id": receipt_id}
        assert env.counts() == before
        assert _ADAPTERS[0].requests == []


def test_stale_running_reservation_returns_recovery_without_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired running reservation is deleted and surfaced as a retryable
    idempotency_stale_running error, without re-entering the model."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _reduce_action(
            env.seeded["sheet_id"],
            idempotency_key="reduce_summary@sha256:stale-running",
        )
        receipt_id = _insert_running_reservation(
            env, action, created_at="2000-01-01 00:00:00"
        )
        before = env.counts()
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_stale_running"
        assert result.errors[0].details["receipt_id"] == receipt_id
        assert result.errors[0].details["retryable"] is True
        # the stale reservation row itself is deleted; nothing else moves
        assert env.counts() == {**before, "receipts": before["receipts"] - 1}
        assert _ADAPTERS[0].requests == []

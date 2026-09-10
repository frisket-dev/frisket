from __future__ import annotations

import importlib
from pathlib import Path

from frisket.engine.store import Project


def test_run_action_spec_rejects_non_object_payload(tmp_path: Path) -> None:
    actions = importlib.import_module("frisket.engine.executor.actions")
    project = Project.create(tmp_path / "invalid.frisket", name="Invalid action")

    result = actions.run_action_spec(
        project,
        ["not", "an", "object"],
        project_id="proj_invalid",
    )

    assert result.status == "failed"
    assert result.project_id == "proj_invalid"
    assert result.action.kind == "unknown"
    assert result.errors[0].code == "invalid_action_request"
    assert result.errors[0].message == "ActionRequest must be a JSON object"


def test_model_call_provider_use_tolerates_missing_or_invalid_units() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_support")

    provider_use = runtime._model_call_provider_use(  # noqa: SLF001
        [
            {
                "provider": "anthropic",
                "engine": "anthropic/claude-haiku-4-5",
                "provider_cost_usd": 0.001,
            },
            {
                "provider": "anthropic",
                "engine": "anthropic/claude-haiku-4-5",
                "provider_cost_usd": 0.002,
                "units": "not-json",
            },
            {
                "provider": "anthropic",
                "engine": "anthropic/claude-haiku-4-5",
                "provider_cost_usd": 0.003,
                "units": {"tokens_in": 7, "tokens_out": 3},
            },
        ],
        model="anthropic/claude-haiku-4-5",
        run={"cost_actual": 0.006},
    )

    assert provider_use == [
        {
            "provider": "anthropic",
            "model": "anthropic/claude-haiku-4-5",
            "model_call_count": 3,
            "cost_actual": 0.006,
            "tokens_in": 7,
            "tokens_out": 3,
        }
    ]


def test_model_call_provider_use_preserves_unknown_cost_vs_known_zero() -> None:
    """A durable NULL provider cost is unknown, not a free provider call."""
    runtime = importlib.import_module("frisket.engine.executor.action_support")

    provider_use = runtime._model_call_provider_use(  # noqa: SLF001
        [
            {
                "provider": "unknown-price",
                "engine": "unknown-price/model",
                "provider_cost_usd": None,
                "units": {"tokens_in": 11},
            },
            {
                "provider": "known-free",
                "engine": "known-free/model",
                "provider_cost_usd": 0.0,
                "units": {"tokens_in": 7},
            },
        ],
        model="unknown-price/model",
        run={"cost_actual": 0.0},
    )

    by_provider = {item["provider"]: item for item in provider_use}
    assert by_provider["unknown-price"]["cost_actual"] is None
    assert by_provider["known-free"]["cost_actual"] == 0.0


def test_receipt_cost_derivation_excludes_historical_cache_cost() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_support")
    calls = [
        {
            "provider": "anthropic",
            "engine": "anthropic/claude-haiku-4-5",
            "credential_source": "cache",
            # Even a legacy cache row carrying its original provider price did
            # not incur that cost in this execution.
            "provider_cost_usd": 0.5,
        },
        {
            "provider": "anthropic",
            "engine": "anthropic/claude-haiku-4-5",
            "credential_source": "platform_key",
            "provider_cost_usd": 0.001,
        },
    ]

    assert runtime._model_calls_cost_actual(calls) == 0.001  # noqa: SLF001
    provider_use = runtime._model_call_provider_use(  # noqa: SLF001
        calls,
        model="anthropic/claude-haiku-4-5",
        run={"cost_actual": 0.501},
    )
    assert provider_use[0]["cost_actual"] == 0.001

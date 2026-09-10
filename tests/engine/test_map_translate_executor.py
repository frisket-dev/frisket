from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    case_env,
)
from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project


class _TranslateAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=54,
            tokens_out=18,
            cost=0.003,
            model=req.model,
        )


class _FailingTranslateAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        raise LLMError("translation provider unavailable", status=503, retryable=False)


# Replaced by _patch_translate at the start of every harness test for this
# case; asserts on .requests are only meaningful behind that patch.
_ADAPTER = _TranslateAdapter({})


def _translate_router(adapter: Any, *, max_retries: int | None = None) -> ModelRouter:
    kwargs: dict[str, Any] = {"keys": {"anthropic": "k"}, "cache": None}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    router = ModelRouter(cache_mode="off", **kwargs)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router


def _patch_translate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    del monkeypatch
    global _ADAPTER
    _ADAPTER = _TranslateAdapter(
        {
            "translation": "Hello world",
            "detected_language": "fr",
        }
    )
    return {"router": _translate_router(_ADAPTER)}


def _map_translate_action(
    sheet_id: int,
    *,
    idempotency_key: str = "map_translate@sha256:stable",
    output_name: str = "translation",
    target_language: str = "English",
    model: str = "anthropic/claude-haiku-4-5",
) -> dict[str, Any]:
    return {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["statement", "speaker"],
            "model": model,
            "target_language": target_language,
            "language": ["es"],
            "save_detected_language": True,
        },
        "output_names": {
            "translation": output_name,
            "detected_language": output_name + "_detected_language",
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Statements")
    columns = {
        "statement": project.add_column(sheet_id, "statement", type="text"),
        "speaker": project.add_column(sheet_id, "speaker", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {"statement": "Bonjour le monde", "speaker": "Ada"},
            {"statement": "Gracias por venir", "speaker": "Grace"},
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_translate_action(seeded["sheet_id"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_translate_action(
        seeded["sheet_id"],
        idempotency_key="map_translate@sha256:missing-source",
    )
    action["params"]["source"] = ["missing"]
    return action


def _unconfirmed_cost_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_translate_action(
        seeded["sheet_id"],
        idempotency_key="map_translate@sha256:cost-gate",
        output_name="translation_cost_gate",
        model="unpriced-provider/mystery-model",
    )


def _colliding_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_translate_action(
        seeded["sheet_id"],
        idempotency_key="map_translate@sha256:collision",
        output_name="statement",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.store.runs import RunResultStore

    sheet_id = seeded["sheet_id"]
    assert len(_ADAPTER.requests) == 2
    assert [output.name for output in result.outputs] == [
        "translation",
        "translation_detected_language",
    ]
    assert [output.ref["role"] for output in result.outputs] == [
        "translation",
        "detected_language",
    ]

    rendered_request_text = "\n".join(
        part.get("text", "")
        for request in _ADAPTER.requests
        for message in request.messages
        for part in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(part, dict)
    )
    assert "statement: Bonjour le monde" in rendered_request_text
    assert "speaker: Ada" in rendered_request_text
    assert "Translate the content into English." in rendered_request_text

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "map.translate"
    assert run["model"] == "anthropic/claude-haiku-4-5"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 0
    assert run["cost_actual"] == 0.006

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    assert columns["translation"]["type"] == "text"
    assert columns["translation"]["ai_generated"] == 1
    assert columns["translation"]["current_run_id"] == result.run_id
    assert columns["translation_detected_language"]["type"] == "category"
    assert columns["translation_detected_language"]["current_run_id"] == result.run_id
    translated = project.get_values(sheet_id, int(columns["translation"]["id"]))
    detected = project.get_values(
        sheet_id, int(columns["translation_detected_language"]["id"])
    )
    assert list(translated.values()) == ["Hello world", "Hello world"]
    # The detected-language column is stored as a BCP-47 tag (the LLM path
    # is instructed to return one and postprocess normalizes its casing),
    # unifying it with the DeepL/Google-derived values in the same column.
    assert list(detected.values()) == ["fr", "fr"]

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 2
    assert {row["provider"] for row in model_calls} == {"anthropic"}
    assert {row["provider_cost_usd"] for row in model_calls} == {0.003}

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "map.translate"
    assert receipt.run_id == result.run_id
    assert receipt.provider_use == [
        {
            "provider": "anthropic",
            "model": "anthropic/claude-haiku-4-5",
            "model_call_count": 2,
            "cost_actual": 0.006,
            "tokens_in": 108,
            "tokens_out": 36,
        }
    ]
    assert {item.ref["kind"] for item in receipt.inputs} >= {"model_rows_input_column"}
    output_refs = {item.name: item.ref for item in receipt.outputs}
    assert set(output_refs) == {"translation", "translation_detected_language"}
    assert output_refs["translation"]["role"] == "translation"
    assert output_refs["translation_detected_language"]["role"] == "detected_language"
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    assert evidence["typed_action_request"]["action_id"] == "map.translate"
    assert evidence["typed_action_request"]["output_names"] == {
        "translation": "translation",
        "detected_language": "translation_detected_language",
    }
    persisted_params = json.loads(run["params"])["params"]
    assert persisted_params["language"] == ["es"]
    assert persisted_params["target_language"] == "English"
    assert evidence["model_rows_model_calls"]["model_call_count"] == 2
    assert evidence["map_rows_run_counts"]["result_count"] == 2


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_translate_action(seeded["sheet_id"], target_language="Spanish")


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='translation'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


CASES = [
    ExecutorCase(
        kind="map.translate",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "call_model_router",
                    "read_input_rows",
                    "create_generated_columns",
                    "write_run_results",
                    "write_model_calls",
                    "write_map_op",
                    "write_receipt",
                    "write_trace",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "invalid_params",
                    "output_column_exists",
                    "model_cost_requires_confirmation",
                    "model_run_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_translate,
        gates=(
            Gate(
                "missing_source",
                _missing_source_action,
                "invalid_input_ref",
            ),
            Gate(
                "model_cost_requires_confirmation",
                _unconfirmed_cost_action,
                "model_cost_requires_confirmation",
                expected_status="needs_confirmation",
            ),
            Gate(
                "output_column_exists",
                _colliding_output_action,
                "output_column_exists",
            ),
        ),
        expect_counts={
            "columns": 2,
            "runs": 1,
            "results": 4,
            "model_calls": 2,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            stale_code="stale_replay",
            make_conflict=_conflicting_params_action,
            go_stale=_go_stale,
        ),
    )
]


def test_map_translate_replay_does_not_recall_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying the stored receipt must reuse the persisted results — the
    model adapter is never called again."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert len(_ADAPTER.requests) == 2

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_ADAPTER.requests) == 2


def test_map_translate_provider_failure_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every model call raises, the action fails as model_run_failed
    but still persists its failed run/receipt trail."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        adapter = _FailingTranslateAdapter()
        env.run_kwargs["router"] = _translate_router(adapter, max_retries=0)
        failed = env.run(
            _map_translate_action(
                env.seeded["sheet_id"],
                idempotency_key="map_translate@sha256:all-failed",
            )
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "model_run_failed"
        assert len(adapter.requests) == 2

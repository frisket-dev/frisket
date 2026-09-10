from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project


PROJECT_ID = "project-confirmation-contract"

# The generic reason vocabulary the confirmation contract allows.
ALLOWED_REASONS = {
    "model_cost",
    "external_metered",
    "remote_egress",
    "destructive",
    "irreversible_external",
    "unknown_estimate",
}


class _SummaryAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content="Shared reporting risk.",
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


def _seed_project(project_path: Path) -> int:
    project = Project.create(project_path, name="Confirmation Contract")
    try:
        sheet_id = project.add_sheet("Stories")
        columns = {
            "story": project.add_column(sheet_id, "story", type="text"),
            "beat": project.add_column(sheet_id, "beat", type="text"),
            "risk_score": project.add_column(sheet_id, "risk_score", type="integer"),
            "country": project.add_column(sheet_id, "country", type="text"),
        }
        project.add_rows(
            sheet_id,
            [
                {
                    "story": "City hall awarded a no-bid software contract",
                    "beat": "accountability",
                    "risk_score": 8,
                    "country": "USA",
                },
                {
                    "story": "Audit found duplicate vendor payments",
                    "beat": "accountability",
                    "risk_score": 9,
                    "country": "Canada",
                },
            ],
            columns,
        )
        return sheet_id
    finally:
        project.close()


def _reduce_action(sheet_id: int, *, model: str, key: str) -> dict[str, Any]:
    """The typed grouped-summary request. Consent is never inside ``params``:
    a confirmed retry echoes the quote's ``promise_set_hash`` as the
    top-level ``confirmation`` field."""

    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story", "risk_score"],
            "group_by": "beat",
            "model": model,
            "instruction": "Summarize common reporting risks for each beat.",
        },
        "sheet_name": "Beat Summaries",
        "idempotency_key": f"reduce_summary@sha256:{key}",
    }


def _counts(project: Project) -> dict[str, int]:
    # Cover every durable-state table the confirmation contract forbids before
    # approval: not just the obvious sheet/run/receipt rows but output claims,
    # blobs, sidecar evidence/source artifacts, model-call provider records, and
    # queued jobs. A regression that wrote any of these pre-confirmation must fail.
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "sheets",
            "columns",
            "rows",
            "runs",
            "results",
            "ops",
            "receipts",
            "output_column_claims",
            "blobs",
            "source_artifacts",
            "evidence_links",
            "model_calls",
        )
    }


def _confirmation_reason(result: Any) -> str:
    assert result.status == "needs_confirmation", result.status
    err = result.errors[0]
    assert err.field == "confirmation", err.field
    reason = (err.details or {}).get("reason")
    assert reason in ALLOWED_REASONS, f"non-generic / missing reason: {reason!r}"
    return reason


# --- model-metered confirmation --------------------------------------------


def test_unconfirmed_model_action_pauses_with_generic_reason_and_no_durable_state(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project_path = tmp_path / "model.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _summary_router()
    project = Project(project_path)
    try:
        before = _counts(project)
        result = executor_actions.run_action_spec(
            project,
            _reduce_action(sheet_id, model="unknown/frontier", key="unconfirmed"),
            project_id=PROJECT_ID,
            router=router,
        )
        # The pause itself: a 402 envelope carrying the exact quote token,
        # with no durable state and no provider call.
        assert result.status == "needs_confirmation", result.errors
        assert result.errors[0].needs_confirmation is True
        details = result.errors[0].details or {}
        assert isinstance(details.get("promise_set_hash"), str)
        assert isinstance(details.get("estimate"), dict)
        assert _counts(project) == before
        assert adapter.requests == []
        # unpriced model => the gate cannot certify "cheap" => unknown_estimate.
        assert _confirmation_reason(result) == "unknown_estimate"
    finally:
        project.close()


def test_confirmed_model_action_proceeds_and_creates_state(tmp_path: Path) -> None:
    from executor_harness import run_action_with_confirmation

    project_path = tmp_path / "confirmed.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _summary_router()
    project = Project(project_path)
    try:
        before = _counts(project)
        # Quote first, then echo the exact token: the only consent there is.
        result = run_action_with_confirmation(
            project,
            _reduce_action(
                sheet_id, model="anthropic/claude-haiku-4-5", key="confirmed"
            ),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        assert _counts(project) != before
        assert adapter.requests, "confirmed run must reach the provider"
    finally:
        project.close()


def test_post_provider_failure_is_terminal_not_confirmation(tmp_path: Path) -> None:
    from executor_harness import run_action_with_confirmation

    project_path = tmp_path / "failure.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _failing_router()
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            _reduce_action(sheet_id, model="anthropic/claude-haiku-4-5", key="failure"),
            project_id=PROJECT_ID,
            router=router,
        )
        # the provider was reached and failed: a terminal action error, never a
        # confirmation pause.
        assert result.status != "needs_confirmation"
        assert result.status in {"failed", "partial"}
        assert adapter.requests, "the provider should have been called"
    finally:
        project.close()


# --- builder-level reason vocabulary ---------------------------------------


def test_confirmation_error_builders_carry_generic_reason() -> None:
    from frisket.contracts.action import ActionIdentity
    from frisket.engine.executor.action_support import (
        _external_cost_requires_confirmation_error,
        _model_cost_requires_confirmation_error,
    )
    from frisket.engine.executor.map_rows_action import (
        _typed_external_cost_error,
        _typed_model_cost_error,
    )
    from frisket.engine.runner import CostGate

    action = ActionIdentity(kind="reduce.group_summary", action_id="act_builder")

    priced = _model_cost_requires_confirmation_error(action, CostGate(5.0))
    assert priced.details["reason"] == "model_cost"
    unknown = _model_cost_requires_confirmation_error(action, CostGate(None))
    assert unknown.details["reason"] == "unknown_estimate"

    web = _external_cost_requires_confirmation_error(
        ActionIdentity(kind="research.web_search", action_id="act_web"), CostGate(None)
    )
    assert web.details["reason"] == "external_metered"

    # The typed wrappers move the field to the top-level ``confirmation``
    # echo without dropping the generic reason.
    typed_priced = _typed_model_cost_error(action, CostGate(5.0))
    assert typed_priced.field == "confirmation"
    assert typed_priced.details["reason"] == "model_cost"
    typed_web = _typed_external_cost_error(
        ActionIdentity(kind="research.web_search", action_id="act_web"), CostGate(None)
    )
    assert typed_web.field == "confirmation"
    assert typed_web.details["reason"] == "external_metered"

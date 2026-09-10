from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import ddgs
import pytest

from action_test_helpers import run_typed_map_request
from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


class _FakeDDGS:
    seen: list[dict[str, Any]] = []

    def text(self, query: str, max_results: int) -> list[dict[str, str]]:
        self.seen.append({"query": query, "max_results": max_results})
        return [
            {
                "title": f"{query} result {index}",
                "href": f"https://example.test/search/{index}",
                "body": f"Snippet {index} about {query}",
            }
            for index in range(1, max_results + 1)
        ]


class _FailingDDGS:
    seen: list[dict[str, Any]] = []

    def text(self, query: str, max_results: int) -> list[dict[str, str]]:
        self.seen.append({"query": query, "max_results": max_results})
        raise RuntimeError("search provider unavailable")


def _patch_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDDGS.seen = []
    monkeypatch.setattr(ddgs, "DDGS", _FakeDDGS)


def _action(
    sheet_id: int,
    *,
    key: str = "research-web-search@stable",
    output_name: str = "search_results",
) -> dict[str, Any]:
    return {
        "action_id": "research.web_search",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "query": {"text": "US tariff impacts on {{country}} economy 2026"},
            "max_results": 2,
        },
        "output_names": {"search_results": output_name},
        "idempotency_key": key,
    }


def _seed(project: Project, _tmp_path: Path) -> dict[str, Any]:
    sheet_id = project.add_sheet("Countries")
    columns = {
        "country": project.add_column(sheet_id, "country", type="text"),
        "region": project.add_column(sheet_id, "region", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"country": "Canada", "region": "North America"},
            {"country": "Mexico", "region": "North America"},
        ],
        columns,
    )
    return {"sheet_id": sheet_id, "country_id": columns["country"], "row_ids": row_ids}


def _missing_source(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _action(seeded["sheet_id"], key="research-web-search@missing")
    action["params"]["query"] = {"text": "{{missing}}"}
    return action


def _output_collision(seeded: dict[str, Any]) -> dict[str, Any]:
    return _action(
        seeded["sheet_id"], key="research-web-search@collision", output_name="country"
    )


def _conflict(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _action(seeded["sheet_id"])
    action["params"]["max_results"] = 3
    return action


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    assert _FakeDDGS.seen == [
        {"query": "US tariff impacts on Canada economy 2026", "max_results": 2},
        {"query": "US tariff impacts on Mexico economy 2026", "max_results": 2},
    ]
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "search_results")
    ]
    output_id = result.outputs[0].column_id
    values = project.get_values(seeded["sheet_id"], output_id)
    assert {len(value) for value in values.values()} == {2}

    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt is not None
    assert receipt.status == "completed"
    assert receipt.inputs[0].ref == {
        "kind": "source_column",
        "name": "country",
        "sheet_id": seeded["sheet_id"],
        "column_id": seeded["country_id"],
        "type": "text",
        "row_ids": seeded["row_ids"],
    }
    assert receipt.outputs[0].ref["kind"] == "map_result_column"
    assert receipt.outputs[0].ref["column_id"] == output_id
    assert receipt.provider_use == [
        {
            "provider": "ddgs",
            "service": "ddgs.text",
            "external_api": True,
            "selected_row_count": 2,
            "successful_row_count": 2,
            "failed_row_count": 0,
            "max_attempts_per_row": 4,
            "operation_call_count": 2,
            "cost_actual": 0.0,
        }
    ]
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    calls = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "web_search_call"
    ]
    assert len({call["call_id"] for call in calls}) == 2
    assert all(call["succeeded"] and "query" not in call for call in calls)
    assert set(evidence) == {
        "typed_action_request",
        "map_rows_run_counts",
        "web_search_call",
    }
    assert evidence["typed_action_request"] == {
        "kind": "typed_action_request",
        "action_id": "research.web_search",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": seeded["sheet_id"],
            "row_ids": None,
        },
        "params": {
            "query": {"text": "US tariff impacts on {{country}} economy 2026"},
            "max_results": 2,
        },
        "output_names": {"search_results": "search_results"},
        "replace_existing": False,
        "params_hash": receipt.params_hash,
        "op_id": result.op_ids[0],
        "run_id": result.run_id,
    }
    assert receipt.outputs[0].ref["value_hash"].startswith("sha256:")
    assert evidence["map_rows_run_counts"]["result_count"] == 2


CASES = [
    ExecutorCase(
        kind="research.web_search",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "external:web_search"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_external_provider",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "output_column_exists",
                    "external_cost_requires_confirmation",
                    "external_rows_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="external_metered",
            cost_requires_confirmation=True,
            input_schema_properties=("query", "max_results"),
            output_schema_properties=("search_results",),
        ),
        seed=_seed,
        make_action=lambda seeded: _action(seeded["sheet_id"]),
        patch=_patch_provider,
        gates=(
            Gate("invalid_input_ref", _missing_source, "invalid_input_ref"),
            Gate("output_column_exists", _output_collision, "output_column_exists"),
            Gate(
                "idempotency_conflict",
                _conflict,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"columns": 1, "runs": 1, "results": 2, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def _run_with_confirmation(
    project: Project, action: dict[str, Any], *, project_id: str = "project-web-search"
) -> Any:
    challenge = run_typed_map_request(project, action, project_id=project_id)
    if challenge.status != "needs_confirmation":
        return challenge
    confirmed = dict(action)
    confirmed["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    return run_typed_map_request(project, confirmed, project_id=project_id)


def test_unknown_cost_requires_exact_confirmation_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch)
    project = Project.create(tmp_path / "confirmation.frisket")
    try:
        seeded = _seed(project, tmp_path)
        action = _action(seeded["sheet_id"])
        before = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "results", "ops", "receipts")
        }

        challenge = run_typed_map_request(
            project, action, project_id="project-web-search"
        )
        assert challenge.status == "needs_confirmation"
        error = challenge.errors[0]
        assert error.code == "external_cost_requires_confirmation"
        assert error.field == "confirmation"
        assert error.details["estimate"]["cost"] is None
        assert error.details["estimate"]["remote_capability"] == "external:web_search"
        assert len(error.details["promise_set_hash"]) == 64
        assert _FakeDDGS.seen == []
        assert {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        } == before

        wrong = dict(action, confirmation="0" * 64)
        rechallenge = run_typed_map_request(
            project, wrong, project_id="project-web-search"
        )
        assert rechallenge.status == "needs_confirmation"
        assert (
            rechallenge.errors[0].details["promise_set_hash"]
            == error.details["promise_set_hash"]
        )
        assert _FakeDDGS.seen == []

        confirmed = dict(action, confirmation=error.details["promise_set_hash"])
        result = run_typed_map_request(
            project, confirmed, project_id="project-web-search"
        )
        assert result.status == "completed", result.errors
        assert len(_FakeDDGS.seen) == 2
    finally:
        project.close()


def test_constant_query_needs_no_sources_and_output_can_be_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch)
    project = Project.create(tmp_path / "constant.frisket")
    try:
        seeded = _seed(project, tmp_path)
        action = _action(
            seeded["sheet_id"],
            key="research-web-search@constant",
            output_name="results",
        )
        action["params"]["query"] = {"text": "fixed public-records query"}
        first = _run_with_confirmation(project, action)
        assert first.status == "completed", first.errors
        first_output_id = first.outputs[0].column_id
        first_receipt = ReceiptStore(project).parsed_by_id(first.receipt_id)
        assert first_receipt is not None and first_receipt.inputs == []
        assert _FakeDDGS.seen == [
            {"query": "fixed public-records query", "max_results": 2},
            {"query": "fixed public-records query", "max_results": 2},
        ]

        replacement = _action(
            seeded["sheet_id"], key="research-web-search@replace", output_name="results"
        )
        replacement["params"]["query"] = {"text": "replacement query"}
        replacement["replace_existing"] = True
        replaced = _run_with_confirmation(project, replacement)
        assert replaced.status == "completed", replaced.errors
        assert replaced.outputs[0].column_id == first_output_id
        assert project.get_values(seeded["sheet_id"], first_output_id) == {
            row_id: [
                {
                    "title": f"replacement query result {index}",
                    "url": f"https://example.test/search/{index}",
                    "snippet": f"Snippet {index} about replacement query",
                }
                for index in (1, 2)
            ]
            for row_id in seeded["row_ids"]
        }
    finally:
        project.close()


def test_all_rows_failed_retries_four_times_and_replays_failed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    _FailingDDGS.seen = []
    monkeypatch.setattr(ddgs, "DDGS", _FailingDDGS)
    project = Project.create(tmp_path / "failed.frisket")
    try:
        seeded = _seed(project, tmp_path)
        action = _action(seeded["sheet_id"], key="research-web-search@failed")
        failed = _run_with_confirmation(project, action)
        assert failed.status == "failed"
        assert failed.errors[0].code == "external_rows_failed"
        assert len(_FailingDDGS.seen) == 8
        receipt = ReceiptStore(project).parsed_by_id(failed.receipt_id)
        assert isinstance(receipt, Receipt)
        assert receipt.status == "failed"
        assert receipt.provider_use[0]["successful_row_count"] == 0
        assert receipt.provider_use[0]["failed_row_count"] == 2
        assert receipt.provider_use[0]["operation_call_count"] == 8
        observed = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "web_search_call"
        ]
        assert len(observed) == 8
        assert all(
            item["provider"] == "ddgs" and not item["succeeded"] for item in observed
        )

        replay = _run_with_confirmation(project, action)
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert len(_FailingDDGS.seen) == 8
    finally:
        project.close()


def test_cancelled_search_receipt_counts_only_completed_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch)
    project = Project.create(tmp_path / "cancelled.frisket")
    try:
        seeded = _seed(project, tmp_path)
        action = _action(seeded["sheet_id"], key="research-web-search@cancelled")

        def dispatch(body: dict[str, Any]) -> Any:
            request = ActionRequest.model_validate(body)
            bound = BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get(request.action_id), request
            )

            def runner_factory(target: Project, _router: Any) -> MapRunner:
                return MapRunner(
                    target,
                    ModelRouter(cache=None, cache_mode="off"),
                    concurrency=1,
                    should_cancel=lambda _run_id: len(_FakeDDGS.seen) >= 2,
                    authority=UnroutedOnlyAuthority(target),
                )

            return run_typed_map_rows_action(
                project,
                "project-web-search",
                bound,
                None,
                runner_factory,
            )

        challenge = dispatch(action)
        assert challenge.status == "needs_confirmation"
        confirmed = dict(
            action,
            confirmation=challenge.errors[0].details["promise_set_hash"],
        )
        result = dispatch(confirmed)

        assert result.status == "cancelled", result.errors
        run = project.db.execute(
            "SELECT status,total_rows,completed_rows,failed_rows FROM runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert tuple(run) == ("cancelled", 2, 1, 0)
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None
        assert receipt.provider_use == [
            {
                "provider": "ddgs",
                "service": "ddgs.text",
                "external_api": True,
                "selected_row_count": 2,
                "successful_row_count": 1,
                "failed_row_count": 0,
                "max_attempts_per_row": 4,
                "operation_call_count": 2,
                "cost_actual": 0.0,
            }
        ]
        counts = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "map_rows_run_counts"
        )
        assert (
            counts["total_rows"],
            counts["completed_rows"],
            counts["failed_rows"],
        ) == (
            2,
            1,
            0,
        )
    finally:
        project.close()

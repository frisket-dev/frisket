from __future__ import annotations

import json
import importlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from executor_harness import assert_pre_dispatch_failure_is_durable
from executor_harness import case_env
from frisket.contracts.action import ActionError
from frisket.contracts.action import ActionIdentity
from frisket.contracts.action import ActionResult
from frisket.contracts.action import ActionSpec
from frisket.contracts.action import Receipt
from frisket.contracts.action import ReceiptIO
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage


PROJECT_ID = "project-reserved-receipt-helpers"
ACTION_KIND = "map.template"
ACTION_ID = "act_reserved_helper"
RECEIPT_ID = "receipt_reserved_helper"
IDEMPOTENCY_KEY = "map_template@sha256:reserved-helper"
PARAMS_HASH = "sha256:reserved-helper-params"


def test_reserved_receipt_helpers_have_singular_owners_without_facade_imports() -> None:
    actions = importlib.import_module("frisket.engine.executor.actions")
    reservations = importlib.import_module(
        "frisket.engine.executor.action_reservations"
    )
    support = importlib.import_module("frisket.engine.executor.action_support")
    reservation_names = {
        "_parse_receipt_created_at",
        "_running_receipt_is_stale",
        "_running_receipt_stale_result",
        "_reserved_receipt_result_from_existing",
        "_reserve_running_action_receipt",
        "_delete_reserved_action_receipt",
        "_cleanup_reserved_receipt_on_failed_result",
        "_finalize_reserved_action_receipt",
    }

    for name in reservation_names:
        assert not hasattr(actions, name)
        assert (
            getattr(reservations, name).__module__
            == "frisket.engine.executor.action_reservations"
        )

    assert support._row_value.__module__ == "frisket.engine.executor.action_support"
    assert reservations.RUNNING_RECEIPT_STALE_AFTER_SECONDS == 60 * 60
    assert not hasattr(actions, "RUNNING_RECEIPT_STALE_AFTER_SECONDS")


def _action() -> ActionSpec:
    return ActionSpec(
        kind=ACTION_KIND,
        params={"output_name": "rendered_note"},
        capabilities=["project:write"],
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _receipt(*, status: str = "running") -> Receipt:
    return Receipt(
        receipt_id=RECEIPT_ID,
        project_id=PROJECT_ID,
        action_id=ACTION_ID,
        action_kind=ACTION_KIND,
        idempotency_key=IDEMPOTENCY_KEY,
        params_hash=PARAMS_HASH,
        status=status,
        inputs=[
            ReceiptIO(
                name="idempotency",
                ref={
                    "kind": "reserved_helper_idempotency",
                    "params_hash": PARAMS_HASH,
                },
            )
        ],
    )


def _insert_receipt(project: Project, receipt: Receipt) -> None:
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, action_id, "
        "idempotency_key, params_hash, status, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.receipt_id,
            receipt.action_kind,
            receipt.action_id,
            receipt.idempotency_key,
            receipt.params_hash,
            receipt.status,
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
        ),
    )
    project.db.commit()


def _receipt_row(project: Project, receipt_id: str = RECEIPT_ID) -> Any:
    return project.db.execute(
        "SELECT * FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()


def _unexpected_existing_result(*args: Any, **kwargs: Any) -> ActionResult:
    raise AssertionError("existing receipt path should not be called")


def test_finalize_reserved_receipt_reports_lost_reservation(tmp_path: Path) -> None:
    from frisket.engine.executor import (
        action_reservations as action_runtime_reservations,
    )

    project = Project.create(tmp_path / "reservation-lost.frisket")
    try:
        result = action_runtime_reservations._finalize_reserved_action_receipt(  # noqa: SLF001
            project,
            _action(),
            params_hash=PARAMS_HASH,
            project_id=PROJECT_ID,
            receipt=_receipt(status="completed"),
            reservation_lost_message="test reservation was lost",
            update_failed_message="test receipt update failed",
            result_from_existing_fn=_unexpected_existing_result,
        )

        assert result is not None
        assert result.status == "failed"
        assert result.receipt_id is None
        assert result.errors[0].code == "project_write_failed"
        assert result.errors[0].message == "test reservation was lost"
    finally:
        project.close()


def test_finalize_reserved_receipt_reports_guarded_update_failure(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import (
        action_reservations as action_runtime_reservations,
    )

    project = Project.create(tmp_path / "reservation-update-failed.frisket")
    try:
        _insert_receipt(project, _receipt(status="completed"))

        result = action_runtime_reservations._finalize_reserved_action_receipt(  # noqa: SLF001
            project,
            _action(),
            params_hash=PARAMS_HASH,
            project_id=PROJECT_ID,
            receipt=_receipt(status="completed"),
            reservation_lost_message="test reservation was lost",
            update_failed_message="test receipt update failed",
            result_from_existing_fn=_unexpected_existing_result,
        )

        assert result is not None
        assert result.status == "failed"
        assert result.receipt_id is None
        assert result.errors[0].code == "project_write_failed"
        assert result.errors[0].message == "test receipt update failed"
        assert _receipt_row(project)["status"] == "completed"
    finally:
        project.close()


def test_finalize_reserved_receipt_commits_callback_atomically(tmp_path: Path) -> None:
    from frisket.engine.executor import (
        action_reservations as action_runtime_reservations,
    )

    project = Project.create(tmp_path / "reservation-callback.frisket")
    try:
        _insert_receipt(project, _receipt(status="running"))

        result = action_runtime_reservations._finalize_reserved_action_receipt(  # noqa: SLF001
            project,
            _action(),
            params_hash=PARAMS_HASH,
            project_id=PROJECT_ID,
            receipt=_receipt(status="completed"),
            reservation_lost_message="test reservation was lost",
            update_failed_message="test receipt update failed",
            result_from_existing_fn=_unexpected_existing_result,
            before_commit=lambda: project.db.execute(
                "INSERT INTO meta (key, value) VALUES ('callback', 'written')"
            ),
        )

        assert result is None
        assert _receipt_row(project)["status"] == "completed"
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key='callback'"
            ).fetchone()["value"]
            == "written"
        )
    finally:
        project.close()


def test_finalize_reserved_receipt_rolls_back_callback_failure(tmp_path: Path) -> None:
    from frisket.engine.executor import (
        action_reservations as action_runtime_reservations,
    )

    project = Project.create(tmp_path / "reservation-callback-failure.frisket")
    try:
        _insert_receipt(project, _receipt(status="running"))

        def fail_after_write() -> None:
            project.db.execute(
                "INSERT INTO meta (key, value) VALUES ('callback', 'partial')"
            )
            raise RuntimeError("evidence write failed")

        result = action_runtime_reservations._finalize_reserved_action_receipt(  # noqa: SLF001
            project,
            _action(),
            params_hash=PARAMS_HASH,
            project_id=PROJECT_ID,
            receipt=_receipt(status="completed"),
            reservation_lost_message="test reservation was lost",
            update_failed_message="test receipt update failed",
            result_from_existing_fn=_unexpected_existing_result,
            before_commit=fail_after_write,
        )

        assert result is not None and result.status == "failed"
        assert _receipt_row(project)["status"] == "running"
        assert (
            project.db.execute("SELECT 1 FROM meta WHERE key='callback'").fetchone()
            is None
        )
    finally:
        project.close()


def test_cleanup_reserved_receipt_deletes_failed_result_without_receipt(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import (
        action_reservations as action_runtime_reservations,
    )

    project = Project.create(tmp_path / "reservation-cleanup.frisket")
    try:
        _insert_receipt(project, _receipt(status="running"))

        failed = ActionResult(
            action=ActionIdentity(kind=ACTION_KIND, action_id=ACTION_ID),
            status="failed",
            project_id=PROJECT_ID,
            errors=[
                ActionError(
                    code="project_write_failed",
                    message="project write failed",
                    action_kind=ACTION_KIND,
                )
            ],
        )

        result = action_runtime_reservations._cleanup_reserved_receipt_on_failed_result(  # noqa: SLF001
            project,
            failed,
            receipt_id=RECEIPT_ID,
            action_kind=ACTION_KIND,
        )

        assert result is failed
        assert _receipt_row(project) is None
    finally:
        project.close()


@pytest.mark.parametrize(
    ("case_module", "fault", "expected_code"),
    [
        pytest.param(
            "tests.engine.test_map_classify_executor",
            "runner",
            "model_run_failed",
            id="map-runner",
        ),
        pytest.param(
            "tests.engine.test_reduce_group_summary_executor",
            "model_completion",
            "model_run_failed",
            id="single-shot-model",
        ),
        pytest.param(
            "tests.engine.test_reduce_group_summary_executor",
            "failed_result_child_sheet_finalization",
            "duplicate_sheet_name",
            id="reduce-failed-result-finalization",
        ),
        pytest.param(
            "tests.engine.test_research_answer_executor",
            "failed_result_maprunner_finalization",
            "project_write_failed",
            id="research-answer-failed-result-finalization",
        ),
        pytest.param(
            "tests.engine.test_research_answer_executor",
            "confirmation_retry_runner",
            # Typed research.answer is a model-backed map-rows terminal; its
            # runner faults surface the shared model_run_failed code.
            "model_run_failed",
            id="research-answer-confirmation-retry-runner",
        ),
    ],
)
def test_reserved_receipt_cleanup_is_wired_across_action_builder_families(
    case_module: str,
    fault: str,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each builder reaches the shared cleanup after its first post-reserve fault."""
    module = importlib.import_module(case_module)
    with case_env(module.CASES[0], tmp_path, monkeypatch) as env:
        action = env.case.make_action(env.seeded)
        seen_running: list[bool] = []
        finalization_seen: list[bool] = []

        def reservation_is_running(project: Project) -> bool:
            row = project.db.execute(
                "SELECT action_kind, status FROM receipts WHERE idempotency_key=?",
                (action["idempotency_key"],),
            ).fetchone()
            return (
                row is not None
                and row["action_kind"] == action["action_id"]
                and row["status"] == "running"
            )

        def saw_running(project: Project) -> None:
            seen_running.append(reservation_is_running(project))

        def saw_finalization(project: Project) -> None:
            finalization_seen.append(reservation_is_running(project))

        def failed_result_finalizer(
            code: str,
            message: str,
            *,
            field: str | None = None,
        ) -> Any:
            from frisket.engine.executor import action_support as action_runtime_support

            def fail(project: Project, *_args: Any, **_kwargs: Any) -> Any:
                saw_finalization(project)
                return action_runtime_support._failed_result(  # noqa: SLF001
                    project_id=env.project_id,
                    action_kind=action["action_id"],
                    error=ActionError(
                        code=code,
                        message=message,
                        action_kind=action["action_id"],
                        field=field,
                    ),
                )

            return fail

        if fault == "model_completion":
            from frisket.engine.executor import group_summary_runtime

            async def explode_completion(*_args: Any, **_kwargs: Any) -> Any:
                saw_running(env.project)
                raise RuntimeError("model completion exploded")

            monkeypatch.setattr(
                group_summary_runtime,
                "_complete_reduce_group_summaries",
                explode_completion,
            )
        elif fault == "failed_result_child_sheet_finalization":
            from frisket.engine.executor import group_summary_runtime

            monkeypatch.setattr(
                group_summary_runtime,
                "_write_reduce_group_summary_result",
                failed_result_finalizer(
                    "duplicate_sheet_name",
                    "reduce.group_summary target sheet name already exists",
                    field="params.target_sheet_name",
                ),
            )
        elif fault == "failed_result_maprunner_finalization":
            # The typed research.answer builder finalizes its reserved receipt
            # through the shared lifecycle's receipt finalizer (no per-kind
            # write override exists on that path); a failed result from that
            # step must still reach the shared cleanup.
            from frisket.engine.executor import action_lifecycle

            monkeypatch.setattr(
                action_lifecycle,
                "_finalize_reserved_action_receipt",
                failed_result_finalizer(
                    "project_write_failed", "simulated finalization failure"
                ),
            )
        else:
            from frisket.engine.runner import MapRunner
            from tests.execution_composition_helpers import open_attempt_authority

            class FaultingMapRunner(MapRunner):
                def __post_init__(self) -> None:
                    super().__post_init__()
                    if fault == "confirmation_retry_runner":
                        saw_running(self.project)

                async def run(self, *_args: Any, **_kwargs: Any) -> Any:
                    if fault == "confirmation_retry_runner":
                        raise RuntimeError("research answer runner exploded")
                    saw_running(self.project)
                    if fault == "runner":
                        raise RuntimeError("map runner exploded")
                    return SimpleNamespace(run_id=42)

            write_overrides = None
            if fault == "finalization":

                def explode_write(project: Project, *_args: Any, **_kwargs: Any) -> Any:
                    saw_finalization(project)
                    raise RuntimeError("receipt finalization exploded")

                write_overrides = {action["action_id"]: explode_write}
            deps = ExecutorDeps(
                consent_coverage=(
                    ConsentCoverage(instance_principal(env.project), Decimal("0"))
                    if fault == "confirmation_retry_runner"
                    else None
                ),
                map_runner_factory=lambda project, router: FaultingMapRunner(
                    project,
                    router,
                    authority=open_attempt_authority(project),
                ),
                reserved_maprunner_write_overrides=write_overrides,
            )
            env.run_kwargs["deps"] = deps

        before = env.counts()
        result = env.run(action)

        assert seen_running == {
            "confirmation_retry_runner": [True, True],
            "failed_result_child_sheet_finalization": [],
            "failed_result_maprunner_finalization": [],
        }.get(fault, [True])
        assert finalization_seen == (
            [True]
            if fault
            in {
                "finalization",
                "failed_result_child_sheet_finalization",
                "failed_result_maprunner_finalization",
            }
            else []
        )
        assert result.status == "failed"
        assert result.errors[0].code == expected_code
        if fault in {
            "failed_result_child_sheet_finalization",
            "failed_result_maprunner_finalization",
        }:
            assert result.receipt_id is None
        assert (
            env.project.db.execute(
                "SELECT 1 FROM receipts WHERE idempotency_key=?",
                (action["idempotency_key"],),
            ).fetchone()
            is None
        )
        if fault == "model_completion":
            assert env.counts() == before
        elif fault == "failed_result_child_sheet_finalization":
            assert env.counts() == {
                **before,
                "ops": before["ops"] + 1,
                "runs": before["runs"] + 1,
                "model_calls": before["model_calls"] + 2,
            }
            assert (
                env.project.db.execute(
                    "SELECT COUNT(*) FROM effect_checkpoints WHERE state='returned'"
                ).fetchone()[0]
                == 2
            )
            assert len(module._ADAPTERS[0].requests) == 2
        elif fault == "failed_result_maprunner_finalization":
            assert (
                int(
                    env.project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[
                        0
                    ]
                )
                == 0
            )
            assert len(module._ADAPTER[0].requests) == 4
            assert len(module._SEARCH_CALLS) == 2
        else:
            assert_pre_dispatch_failure_is_durable(env.project, before, env.counts())

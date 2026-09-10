from __future__ import annotations

import json

from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Row,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


class Params(ActionParams):
    source: ColumnRef[str]
    tag: str


class Output(BaseModel):
    rendered: str


def test_cancelled_typed_receipt_owns_only_produced_rows_and_replays_exact_heads(
    tmp_path,
):
    calls = []

    def render(params: Params, row: Row) -> RowResult[Output]:
        value = params.source.read(row)
        calls.append((params.tag, value))
        return RowResult(output=Output(rendered=f"{params.tag}:{value}"))

    registry = ActionRegistry(
        [
            ActionNamespace(
                "test",
                actions=[
                    action(
                        name="cancelled_render",
                        title="Cancelled render",
                        description="Exercise exact produced-row receipt scope.",
                        category=ActionCategory.CONVERT,
                        run=map_rows(render),
                    )
                ],
            )
        ]
    )
    project = Project.create(tmp_path / "cancelled-receipt.frisket")
    try:
        sheet = project.add_sheet("data")
        source = project.add_column(sheet, "source")
        rows = project.add_rows(
            sheet,
            [{"source": value} for value in ("one", "two", "three")],
            {"source": source},
        )

        def bind(selected, *, key, replace=False):
            request = ActionRequest(
                action_id="test.cancelled_render",
                scope=SheetRows(sheet_id=sheet, row_ids=tuple(selected)),
                params={"source": "source", "tag": key},
                output_names={"rendered": "Rendered"},
                replace_existing=replace,
                idempotency_key=key,
            )
            return BoundTypedActionRequest.bind(
                registry.get(request.action_id), request
            )

        def factory(store, router, *, cancel_after_result=False):
            return MapRunner(
                store,
                router,
                concurrency=1,
                should_cancel=(
                    lambda run_id: (
                        store.db.execute(
                            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
                        ).fetchone()[0]
                        >= 1
                    )
                )
                if cancel_after_result
                else None,
                authority=UnroutedOnlyAuthority(store),
            )

        def run(bound, runner_factory=factory):
            return run_typed_map_rows_action(
                project,
                "project-1",
                bound,
                ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
                runner_factory,
            )

        seed = run(bind(rows, key="seed"))
        assert seed.status == "completed", seed.errors
        assert len(calls) == 3
        output = next(
            column for column in project.columns(sheet) if column["name"] == "Rendered"
        )
        bound = bind(rows[:2], key="cancelled", replace=True)
        cancelled = run(
            bound,
            lambda store, router: factory(store, router, cancel_after_result=True),
        )
        assert cancelled.status == "cancelled", cancelled.errors
        assert calls[3:] == [("cancelled", "one")]
        produced = project.db.execute(
            "SELECT row_id FROM results WHERE run_id=? ORDER BY row_id",
            (cancelled.run_id,),
        ).fetchall()
        assert [row["row_id"] for row in produced] == rows[:1]

        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (cancelled.receipt_id,)
            ).fetchone()["body"]
        )
        refs = [item["ref"] for item in receipt["outputs"]]
        assert len(refs) == 1
        assert refs[0]["kind"] == "map_result_column"
        assert refs[0]["run_id"] == cancelled.run_id
        assert refs[0]["row_ids"] == rows[:1]
        # The selected-but-unproduced row and unselected row retain old heads;
        # neither is evidence of this cancelled replacement invocation.
        heads = ResultGenerationStore(project).read_cell_heads(output["id"])
        assert heads[rows[0]].run_id == cancelled.run_id
        assert heads[rows[1]].run_id == heads[rows[2]].run_id == seed.run_id

        def forbidden_runner(*_args):
            raise AssertionError("same-key replay must not create a runner")

        before_runs = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        replay = run(bound, forbidden_runner)
        assert replay.status == "cancelled", replay.errors
        assert replay.receipt_id == cancelled.receipt_id
        assert replay.run_id == cancelled.run_id
        assert len(calls) == 4
        assert (
            project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before_runs
        )

        successor = run(bind(rows[:1], key="successor", replace=True))
        assert successor.status == "completed", successor.errors
        stale = run(bound, forbidden_runner)
        assert stale.status == "failed"
        assert [error.code for error in stale.errors] == ["stale_replay"]
        assert stale.errors[0].details["changed_row_ids"] == rows[:1]
        assert len(calls) == 5
    finally:
        project.close()

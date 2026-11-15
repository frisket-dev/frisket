"""Actual row publication keeps known usage through retry outcomes and Stop."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

from contextlib import closing

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from executor_harness import run_action_with_confirmation
from tests.ai.test_retry_returned_usage import ReturnedAdapter
from test_run_trace import _action


@pytest.mark.parametrize("final", ["ok", "paid_error", "unknown", "stop"])
def test_row_retry_retains_each_known_purchase_without_publishing_after_stop(
    tmp_path, final
):
    with closing(Project.create(tmp_path / "row-retry.frisket")) as project:
        sheet = project.add_sheet("Rows")
        column = project.add_column(sheet, "story")
        [row] = project.add_rows(sheet, [{"story": "actual input"}], {"story": column})

        class Adapter(ReturnedAdapter):
            async def complete(self, req, client):
                if final == "stop" and self.calls == 0:
                    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[
                        0
                    ]
                    RunResultStore(project).request_cancel(run_id)
                response = await super().complete(req, client)
                response.data = {"beat": "a"}
                response.content = '{"beat":"a"}'
                return response

        adapter = Adapter(["paid_error"] if final == "stop" else ["paid_error", final])
        router = ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            keys={"anthropic": "test-key"},
            max_retries=1,
            cache=None,
        )
        router._adapters["anthropic"] = adapter
        result = run_action_with_confirmation(
            project,
            _action(sheet, key=f"retry-{final}"),
            project_id="row-retry",
            router=router,
        )
        run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        calls = RunResultStore(project).model_calls(run_id)
        expected = 1 if final in {"unknown", "stop"} else 2
        assert len(calls) == expected
        assert sum(call["provider_cost_usd"] for call in calls) == pytest.approx(
            0.01 if expected == 1 else 0.03
        )
        assert adapter.calls == (1 if final == "stop" else 2)
        if final == "ok":
            assert result.status == "completed", result.errors
        elif final == "stop":
            assert result.status == "cancelled", result.errors
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM cell_result_heads WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                == 0
            )
        elif final == "unknown":
            assert any("reconciliation" in error.code for error in result.errors), (
                result.errors
            )
            assert (
                project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
                == "reserved"
            )
        else:
            assert result.status == "failed", result.errors

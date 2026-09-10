from frisket.ai.llm import ModelRouter
import pytest
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.actions.file_fetch import FetchParams, FetchOutput
from frisket.actions.file_types import FileFetcher
from frisket.actions.python_types import PythonEvaluator
from frisket.actions.types import Row, RowResult


async def evaluate_and_fetch(
    params: FetchParams, row: Row, evaluator: PythonEvaluator, fetcher: FileFetcher
) -> RowResult[FetchOutput]:
    url = await evaluator.evaluate(
        code="result = row['url']", row={"url": params.source.read(row)}
    )
    return RowResult(output=FetchOutput(media=await fetcher.fetch(url)))


@pytest.mark.parametrize(
    ("cancel_after_return", "mixed"), [(False, False), (True, False), (False, True)]
)
def test_fetch_publishes_file_and_actual_durable_occurrence(
    tmp_path, monkeypatch, cancel_after_return, mixed
):
    if mixed:
        from dataclasses import replace
        from frisket.actions.core import RegisteredAction, map_rows
        from frisket.actions.registry import ACTION_REGISTRY

        original = ACTION_REGISTRY.get("media.fetch_url")
        action = RegisteredAction(
            "media.fetch_url",
            replace(
                original.definition,
                run=map_rows(evaluate_and_fetch),
                _example_params=(),
            ),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, "media.fetch_url": action},
        )
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
    calls = []

    def download(url, **kwargs):
        calls.append(url)
        return b"downloaded file", "text/plain", "result.txt", None

    monkeypatch.setattr("frisket.ops.enclosures.download_url", download)
    project = Project.create(tmp_path / "fetch.frisket")
    try:
        sheet = project.add_sheet("Data")
        column = project.add_column(sheet, "url", type="link")
        project.add_rows(sheet, [{"url": "https://example.test/file"}], {"url": column})
        body = {
            "action_id": "media.fetch_url",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "url"},
            "output_names": {"media": "download"},
            "idempotency_key": "fetch-proof",
        }
        router = ModelRouter()
        quote = run_action_spec(project, body, project_id="fetch", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        assert not calls
        body["confirmation"] = quote.errors[0].details["promise_set_hash"]
        if cancel_after_return:
            from frisket.engine.executor import actions
            from frisket.engine.store.runs import RunResultStore

            cancelled = False
            original_complete = RunResultStore.complete_row_effect_checkpoint
            original_factory = actions._default_map_runner_factory

            def complete(self, *args, **kwargs):
                nonlocal cancelled
                returned = original_complete(self, *args, **kwargs)
                cancelled = True
                return returned

            def factory(*args, **kwargs):
                runner = original_factory(*args, **kwargs)
                runner.should_cancel = lambda _run_id: cancelled
                return runner

            monkeypatch.setattr(
                RunResultStore, "complete_row_effect_checkpoint", complete
            )
            monkeypatch.setattr(actions, "_default_map_runner_factory", factory)
        result = run_action_spec(project, body, project_id="fetch", router=router)
        if cancel_after_return:
            assert result.status == "cancelled", result.errors
            receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
            assert len(calls) == 1
            assert any(
                p.get("external_api") and p.get("operation_call_count") == 1
                for p in receipt.provider_use
            )
            assert any(e.ref.get("kind") == "row_file_call" for e in receipt.evidence)
            assert not any(
                e.ref.get("kind") == "row_file_output" for e in receipt.evidence
            )
            assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
            assert (
                project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
                == "returned"
            )
            return
        assert result.status == "completed", result.model_dump(mode="json")
        assert calls == ["https://example.test/file"]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        occurrences = [
            e.ref for e in receipt.evidence if e.ref.get("kind") == "row_file_output"
        ]
        assert len(occurrences) == 1
        assert occurrences[0]["output_key"] == "media"
        assert occurrences[0]["facts"]["url"] == calls[0]
        with project.materialize_blob(occurrences[0]["primary"]["blob_hash"]) as path:
            assert path.read_bytes() == b"downloaded file"
        assert any(p.get("external_api") for p in receipt.provider_use)
        if mixed:
            assert any(
                p.get("service") == "frisket.sandbox" and p.get("cost_actual") == 0
                for p in receipt.provider_use
            )
            assert any(e.ref.get("kind") == "map_python_code" for e in receipt.evidence)
        replay = run_action_spec(project, body, project_id="fetch", router=router)
        assert replay.status == "completed"
        assert len(calls) == 1
    finally:
        project.close()

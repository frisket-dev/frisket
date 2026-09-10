from __future__ import annotations

import importlib
import asyncio

import httpx
import pytest

from frisket.contracts.action import (
    ActionError,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


EXPECTED_OUTPUT_REF_KINDS = {
    "materialized_sheet",
    "source_column",
    "source_rows",
    "materialized_column",
    "materialized_join_table",
    "materialized_join_column",
    "column_type_update",
    "column_patch",
    "added_column",
    "materialized_rows",
    "materialized_join_rows",
    "materialized_link_table",
    "materialized_link_column",
    "materialized_link_rows",
    "added_rows",
    "deleted_rows",
    "map_result_column",
    "map_ask_output_column",
    "map_classify_output_column",
    "map_extract_output_column",
    "map_translate_output_column",
    "map_ner_output_column",
    "map_find_topic_sections_output_column",
    "map_clean_names_output_column",
    "media_transcribe_output_column",
    "media_ocr_output_column",
    "media_ytdlp_download_output_column",
    "semantic_join_output_column",
    "semantic_join_link_sheet",
    "semantic_join_link_edges",
    "research_answer_output_column",
    "map_summarize_output_column",
    "geocode_output_column",
    "census_demographics_output_column",
    "reduce_summary_output_column",
    "named_result",
    "operation_status_transition",
    "run_backfill",
    "embedding_index",
    "embedding_index_refresh",
    "embedding_index_delete",
    "embedding_index_update_policy",
    "manual_edit_batch",
    "edit_overlay",
    "review_decision",
    "replay_accept",
    "replay_accept_column",
    "replay_dismiss",
    "export_artifact",
    "export_project_file",
    "external_export",
    "query_preview",
    "query_cell_edit_batch",
    "plugin_manifest",
    "source_create_source",
    "source_update_source",
    "source_delete_source",
    "rss_source",
    "rss_source_run",
    "source_poll_source",
    "source_poll_run",
    "source_check_source",
    "source_check_run",
    "source_sheet",
    "rss_feed_rows",
    "source_poll_rows",
    "enclosure_rows",
    "media_cell",
    "media_blob",
    "web_capture_page_output_column",
    "updated_import_rows",
}


def test_result_from_receipt_lives_only_in_receipt_projection() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    actions = importlib.import_module("frisket.engine.executor.actions")
    action_jobs = importlib.import_module("frisket.engine.executor.action_jobs")

    assert not hasattr(actions, "_result_from_receipt")
    assert not hasattr(actions, "_runtime_action_result_from_receipt")
    assert not hasattr(action_jobs, "_action_job_result_from_terminal_receipt")

    receipt = Receipt(
        receipt_id="receipt_replay",
        project_id="project-1",
        action_id="act_replay",
        action_kind="import.rows",
        op_ids=[7],
        status="completed",
        run_id=1,
        outputs=[
            ReceiptIO(
                name="Rows",
                ref={"kind": "materialized_sheet", "sheet_id": 11, "op_id": 7},
            ),
            ReceiptIO(
                name="column.summary",
                ref={
                    "kind": "source_column",
                    "sheet_id": 11,
                    "column_id": 22,
                    "op_id": 7,
                },
            ),
            ReceiptIO(
                name="rows",
                ref={
                    "kind": "source_rows",
                    "sheet_id": 11,
                    "row_ids": [101, 102],
                    "op_id": 7,
                },
            ),
            ReceiptIO(
                name="artifact",
                ref={
                    "kind": "export_artifact",
                    "path": "/tmp/export.csv",
                    "format": "csv",
                },
            ),
        ],
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert result.action.kind == "import.rows"
    assert result.action.action_id == "act_replay"
    assert result.status == "completed"
    assert result.project_id == "project-1"
    assert result.run_id == 1
    assert result.op_ids == [7]
    assert result.receipt_id == "receipt_replay"
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("sheet", "Rows"),
        ("column", "summary"),
        ("rows", "rows"),
        ("export", "artifact"),
    ]
    assert result.outputs[1].column_id == 22
    assert result.outputs[2].row_ids == [101, 102]


def test_result_from_receipt_uses_output_ref_registry() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")

    assert set(runtime._OUTPUT_REF_RESULT_BUILDERS) == EXPECTED_OUTPUT_REF_KINDS  # noqa: SLF001

    receipt = Receipt(
        receipt_id="receipt_registry_replay",
        project_id="project-1",
        action_id="act_registry_replay",
        action_kind="replay.registry",
        status="completed",
        outputs=[
            ReceiptIO(
                name="column.summary",
                ref={
                    "kind": "source_column",
                    "sheet_id": 11,
                    "column_id": 22,
                },
            ),
            ReceiptIO(
                name="linked rows",
                ref={
                    "kind": "semantic_join_link_edges",
                    "child_sheet_id": 33,
                    "child_row_ids": [301, 302],
                },
            ),
            ReceiptIO(
                name="plugin",
                ref={"kind": "plugin_manifest", "plugin_id": "csv-tools"},
            ),
            ReceiptIO(
                name="media",
                ref={"kind": "media_cell", "sheet_id": 44, "row_id": 401},
            ),
        ],
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "summary"),
        ("rows", "linked rows"),
        ("plugin_manifest", "csv-tools"),
        ("media_cell", "media"),
    ]
    assert result.outputs[1].sheet_id == 33
    assert result.outputs[1].row_ids == [301, 302]
    assert result.outputs[3].sheet_id == 44
    assert result.outputs[3].row_ids == [401]


def test_result_from_receipt_projects_replay_outputs() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_replay_outputs",
        project_id="project-1",
        action_id="act_replay_outputs",
        action_kind="replay.accept",
        status="completed",
        outputs=[
            ReceiptIO(
                name="replay.accept",
                ref={
                    "kind": "replay_accept",
                    "sheet_id": 11,
                    "column_id": 21,
                    "row_id": 31,
                },
            ),
            ReceiptIO(
                name="replay.accept_column",
                ref={
                    "kind": "replay_accept_column",
                    "sheet_id": 11,
                    "column_id": 21,
                },
            ),
            ReceiptIO(
                name="replay.dismiss",
                ref={
                    "kind": "replay_dismiss",
                    "sheet_id": 11,
                    "column_id": 21,
                    "row_id": 32,
                },
            ),
        ],
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert [(output.kind, output.name) for output in result.outputs] == [
        ("replay", "replay.accept"),
        ("replay", "replay.accept_column"),
        ("replay", "replay.dismiss"),
    ]
    assert [output.sheet_id for output in result.outputs] == [11, 11, 11]
    assert [output.column_id for output in result.outputs] == [21, 21, 21]
    assert [output.row_ids for output in result.outputs] == [[31], [], [32]]


def test_result_from_receipt_reconstructs_map_columns(tmp_path, monkeypatch) -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    project = Project.create(tmp_path / "api-receipt.frisket")
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"city": "London"})

    monkeypatch.setattr(
        "frisket.ops.netguard.safe_pinned_addresses",
        lambda url, **kwargs: ["93.184.216.34"],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = client
    try:
        sheet_id = project.add_sheet("Data")
        source_id = project.add_column(sheet_id, "id", type="text")
        row_ids = project.add_rows(
            sheet_id, [{"id": "1"}, {"id": "2"}], {"id": source_id}
        )
        request = {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"request": {"url": "https://api.example/items/{{id}}"}},
            "output_names": {"api_result": "HTTP response"},
            "idempotency_key": "api-receipt-projection",
        }
        quote = run_action_spec(project, request, project_id="project-1", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        completed = run_action_spec(
            project,
            {**request, "confirmation": quote.errors[0].details["promise_set_hash"]},
            project_id="project-1",
            router=router,
        )
        assert completed.status == "completed", completed.errors
        receipt = ReceiptStore(project).parsed_by_id(completed.receipt_id)
        api_output = receipt.outputs[0]
        assert api_output.name == "HTTP response"
        assert api_output.ref["kind"] == "map_result_column"
        assert api_output.ref["name"] == "HTTP response"
        column = next(
            column
            for column in project.columns(sheet_id, include_hidden=True)
            if column["id"] == api_output.ref["column_id"]
        )
        assert column["default_hidden"] and not column["hidden"]
        assert calls == ["/items/1", "/items/2"]

        # Keep the mixed typed/legacy map-column projection contract covered.
        receipt.outputs.append(
            ReceiptIO(
                name="city",
                ref={
                    "kind": "map_extract_output_column",
                    "sheet_id": sheet_id,
                    "column_id": 22,
                    "row_ids": api_output.ref["row_ids"],
                },
            )
        )
        result = runtime._result_from_receipt(receipt)  # noqa: SLF001

        assert [(output.kind, output.name) for output in result.outputs] == [
            ("column", "HTTP response"),
            ("column", "city"),
        ]
        assert result.outputs[0].ref == completed.outputs[0].ref
        assert [output.column_id for output in result.outputs] == [column["id"], 22]
        assert [output.row_ids for output in result.outputs] == [
            row_ids,
            row_ids,
        ]
        assert calls == ["/items/1", "/items/2"]
    finally:
        asyncio.run(client.aclose())
        project.close()


def test_result_from_receipt_fails_closed_on_unknown_first_party_output_ref() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_unknown_output_ref",
        project_id="project-1",
        action_id="act_unknown_output_ref",
        action_kind="plugin.load",
        status="completed",
        outputs=[
            ReceiptIO(
                name="future output",
                ref={"kind": "future_output_ref", "detail": "kept in receipt"},
            ),
        ],
    )

    with pytest.raises(
        ValueError,
        match=("unknown_receipt_output_ref_kind: .*ref_kind='future_output_ref'"),
    ):
        runtime._result_from_receipt(receipt)  # noqa: SLF001


def test_result_from_receipt_preserves_partial_order_and_receipt_owned_facts() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    errors = [
        ActionError(
            code="partial_output",
            message="one output was partial",
            details={"ordinal": 2},
        )
    ]
    receipt = Receipt(
        receipt_id="receipt_partial_projection",
        project_id="project-1",
        action_id="act_partial_projection",
        action_kind="projection.partial",
        run_id=41,
        op_ids=[7, 9],
        status="partial",
        outputs=[
            ReceiptIO(
                name="rows",
                ref={"kind": "source_rows", "sheet_id": 3, "row_ids": [8, 5]},
            ),
            ReceiptIO(
                name="plugin",
                ref={"kind": "plugin_manifest", "plugin_id": "csv-tools"},
            ),
        ],
        warnings=["first warning", "second warning"],
        errors=errors,
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert result.status == "partial"
    assert result.run_id == 41
    assert result.op_ids == [7, 9]
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("rows", "rows"),
        ("plugin_manifest", "csv-tools"),
    ]
    assert result.outputs[0].row_ids == [8, 5]
    assert result.warnings == receipt.warnings
    assert [error.model_dump(mode="json") for error in result.errors] == [
        error.model_dump(mode="json") for error in receipt.errors
    ]


def test_result_from_receipt_preserves_cancelled_status() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_cancelled_projection",
        project_id="project-1",
        action_id="act_cancelled_projection",
        action_kind="projection.cancelled",
        status="cancelled",
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert result.status == "cancelled"
    assert result.receipt_id == receipt.receipt_id


@pytest.mark.parametrize(
    "provider_use",
    [
        pytest.param([], id="no-cost"),
        pytest.param(
            [{"provider": "unknown", "cost_actual": None}],
            id="unknown-cost",
        ),
        pytest.param(
            [{"provider": "known", "cost_actual": 0.0}],
            id="zero-cost",
        ),
    ],
)
def test_result_from_receipt_keeps_settlement_facts_behind_receipt_boundary(
    provider_use: list[dict[str, object]],
) -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_settlement_projection",
        project_id="project-1",
        action_id="act_settlement_projection",
        action_kind="projection.settlement",
        status="completed",
        provider_use=provider_use,
        evidence=[ReceiptEvidence(ref={"kind": "audit_fact", "value": 1})],
        trace_ref={"kind": "trace", "trace_id": "trace-1"},
        review={"state": "accepted"},
        exports=[{"kind": "archive", "path": "result.zip"}],
    )
    source = receipt.model_dump(mode="json")

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001
    projected = result.model_dump(mode="json")

    assert receipt.model_dump(mode="json") == source
    assert receipt.provider_use == provider_use
    for receipt_only_field in (
        "provider_use",
        "evidence",
        "trace_ref",
        "review",
        "exports",
    ):
        assert receipt_only_field not in projected


def test_result_from_receipt_routes_trusted_plugin_ref_through_extension_boundary() -> (
    None
):
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_plugin_extension_projection",
        project_id="project-1",
        action_id="act_plugin_extension_projection",
        action_kind="frisket.runtime_demo.action",
        status="completed",
        outputs=[
            ReceiptIO(
                name="demo",
                ref={"kind": "runtime_action_result", "value": "handled"},
            )
        ],
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "workbench_runtime_binding",
                    "plugin": "frisket-runtime-demo",
                }
            )
        ],
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert [output.model_dump(mode="json") for output in result.outputs] == [
        {
            "kind": "runtime_action_result",
            "name": "demo",
            "sheet_id": None,
            "column_id": None,
            "row_ids": [],
            "ref": {"kind": "runtime_action_result", "value": "handled"},
        }
    ]


def test_result_from_receipt_preserves_exact_v1_serialization() -> None:
    runtime = importlib.import_module("frisket.engine.executor.action_receipts")
    receipt = Receipt(
        receipt_id="receipt_v1_projection",
        project_id="project-1",
        action_id="act_v1_projection",
        action_kind="projection.v1",
        status="completed",
    )

    result = runtime._result_from_receipt(receipt)  # noqa: SLF001

    assert result.model_dump(mode="json") == {
        "schema_version": "frisket.action_result.v1",
        "action": {
            "kind": "projection.v1",
            "action_id": "act_v1_projection",
        },
        "status": "completed",
        "project_id": "project-1",
        "run_id": None,
        "job_id": None,
        "op_ids": [],
        "outputs": [],
        "receipt_id": "receipt_v1_projection",
        "value": None,
        "warnings": [],
        "errors": [],
    }
    assert ReceiptIO(
        name="output",
        ref={"kind": "source_rows", "row_ids": []},
    ).model_dump(mode="json") == {
        "name": "output",
        "ref": {"kind": "source_rows", "row_ids": []},
    }

"""Receipt decoding and projection into public action results."""

from __future__ import annotations

from typing import Any, Callable, Mapping


from frisket.contracts.action import (
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptIO,
)


_OutputRefResultBuilder = Callable[[ReceiptIO, dict[str, Any]], ActionOutput]


def _receipt_ref_row_ids(ref: dict[str, Any], field: str = "row_ids") -> list[Any]:
    explicit = ref.get(field)
    if explicit is not None:
        return list(explicit or [])
    if field != "row_ids":
        return []
    first = ref.get("first_row_id")
    last = ref.get("last_row_id")
    count = ref.get("row_count")
    if (
        isinstance(first, int)
        and not isinstance(first, bool)
        and isinstance(last, int)
        and not isinstance(last, bool)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and first > 0
        and last >= first
        and last - first + 1 == count
    ):
        return list(range(first, last + 1))
    return []


def _receipt_sheet_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="sheet",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


def _receipt_sheet_rows_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="sheet",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_column_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="column",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        ref=ref,
    )


def _receipt_source_column_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="column",
        name=output.name.removeprefix("column."),
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_column_rows_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="column",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_rows_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="rows",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_replay_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="replay",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=(
            [ref["row_id"]]
            if isinstance(ref.get("row_id"), int)
            and not isinstance(ref.get("row_id"), bool)
            else []
        ),
        ref=ref,
    )


def _receipt_query_preview_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="query_preview",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_semantic_edges_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="rows",
        name=output.name,
        sheet_id=ref.get("child_sheet_id"),
        row_ids=_receipt_ref_row_ids(ref, "child_row_ids"),
        ref=ref,
    )


def _receipt_named_result_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="named_result",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=_receipt_ref_row_ids(ref),
        ref=ref,
    )


def _receipt_operation_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(kind="operation", name=output.name, ref=ref)


def _receipt_run_backfill_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="run_backfill",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=_receipt_ref_row_ids(ref, "filled_row_ids"),
        ref=ref,
    )


def _receipt_edit_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    row_ids = [
        int(target["row_id"])
        for target in ref.get("targets", [])
        if isinstance(target, dict) and target.get("row_id") is not None
    ]
    if ref.get("row_id") is not None:
        row_ids.append(int(ref["row_id"]))
    return ActionOutput(
        kind="edit",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        column_id=ref.get("column_id"),
        row_ids=row_ids,
        ref=ref,
    )


def _receipt_review_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(kind="review", name=output.name, ref=ref)


def _receipt_export_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(kind="export", name=output.name, ref=ref)


def _receipt_plugin_manifest_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind="plugin_manifest",
        name=ref.get("plugin_id")
        if isinstance(ref.get("plugin_id"), str)
        else output.name,
        ref=ref,
    )


def _receipt_source_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="source",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


def _receipt_source_run_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="source_run",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


def _receipt_media_cell_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="media_cell",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        row_ids=[int(ref["row_id"])] if ref.get("row_id") else [],
        ref=ref,
    )


def _receipt_media_blob_output(output: ReceiptIO, ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="media_blob",
        name=output.name,
        sheet_id=ref.get("sheet_id"),
        row_ids=[int(ref["row_id"])] if ref.get("row_id") else [],
        ref=ref,
    )


def _receipt_embedding_index_output(
    output: ReceiptIO, ref: dict[str, Any]
) -> ActionOutput:
    return ActionOutput(
        kind=ref.get("kind", "embedding_index"),
        name=ref.get("index_id") or output.name,
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


_OUTPUT_REF_RESULT_BUILDERS: Mapping[str, _OutputRefResultBuilder] = {
    "materialized_sheet": _receipt_sheet_output,
    "materialized_join_table": _receipt_sheet_output,
    "embedding_index": _receipt_embedding_index_output,
    "embedding_index_refresh": _receipt_embedding_index_output,
    "embedding_index_delete": _receipt_embedding_index_output,
    "embedding_index_update_policy": _receipt_embedding_index_output,
    "source_column": _receipt_source_column_output,
    "source_rows": _receipt_rows_output,
    "materialized_column": _receipt_source_column_output,
    "materialized_join_column": _receipt_column_output,
    "column_type_update": _receipt_column_output,
    "column_patch": _receipt_column_output,
    "added_column": _receipt_column_output,
    "materialized_rows": _receipt_rows_output,
    "materialized_join_rows": _receipt_rows_output,
    "added_rows": _receipt_rows_output,
    "deleted_rows": _receipt_rows_output,
    "map_result_column": _receipt_column_rows_output,
    "map_classify_output_column": _receipt_column_rows_output,
    "map_extract_output_column": _receipt_column_rows_output,
    "map_translate_output_column": _receipt_column_rows_output,
    "map_ner_output_column": _receipt_column_rows_output,
    "map_find_topic_sections_output_column": _receipt_column_rows_output,
    "map_clean_names_output_column": _receipt_column_rows_output,
    "media_transcribe_output_column": _receipt_column_rows_output,
    "media_ocr_output_column": _receipt_column_rows_output,
    "media_ytdlp_download_output_column": _receipt_column_rows_output,
    "web_capture_page_output_column": _receipt_column_rows_output,
    "query_preview": _receipt_query_preview_output,
    "materialized_link_table": _receipt_sheet_output,
    "materialized_link_column": _receipt_column_output,
    "materialized_link_rows": _receipt_rows_output,
    "semantic_join_output_column": _receipt_column_rows_output,
    "semantic_join_link_sheet": _receipt_sheet_rows_output,
    "semantic_join_link_edges": _receipt_semantic_edges_output,
    "research_answer_output_column": _receipt_column_rows_output,
    "map_summarize_output_column": _receipt_column_rows_output,
    "map_ask_output_column": _receipt_column_rows_output,
    "geocode_output_column": _receipt_column_rows_output,
    "census_demographics_output_column": _receipt_column_rows_output,
    "reduce_summary_output_column": _receipt_column_rows_output,
    "named_result": _receipt_named_result_output,
    "operation_status_transition": _receipt_operation_output,
    "run_backfill": _receipt_run_backfill_output,
    "manual_edit_batch": _receipt_edit_output,
    "updated_import_rows": _receipt_edit_output,
    "query_cell_edit_batch": _receipt_edit_output,
    "edit_overlay": _receipt_edit_output,
    "review_decision": _receipt_review_output,
    "replay_accept": _receipt_replay_output,
    "replay_accept_column": _receipt_replay_output,
    "replay_dismiss": _receipt_replay_output,
    "export_artifact": _receipt_export_output,
    "export_project_file": _receipt_export_output,
    "external_export": _receipt_export_output,
    "plugin_manifest": _receipt_plugin_manifest_output,
    "source_create_source": _receipt_source_output,
    "source_update_source": _receipt_source_output,
    "source_delete_source": _receipt_source_output,
    "rss_source": _receipt_source_output,
    "rss_source_run": _receipt_source_run_output,
    "source_poll_source": _receipt_source_output,
    "source_poll_run": _receipt_source_run_output,
    "source_check_source": _receipt_source_output,
    "source_check_run": _receipt_source_run_output,
    "source_sheet": _receipt_sheet_output,
    "rss_feed_rows": _receipt_rows_output,
    "source_poll_rows": _receipt_rows_output,
    "enclosure_rows": _receipt_rows_output,
    "media_cell": _receipt_media_cell_output,
    "media_blob": _receipt_media_blob_output,
}


def _result_from_receipt(receipt: Receipt) -> ActionResult:
    outputs: list[ActionOutput] = []
    plugin_extension = any(
        item.ref.get("kind") == "workbench_runtime_binding" for item in receipt.evidence
    )
    for output in receipt.outputs:
        ref = output.ref
        ref_kind = ref.get("kind")
        builder = (
            _OUTPUT_REF_RESULT_BUILDERS.get(ref_kind)
            if isinstance(ref_kind, str)
            else None
        )
        if builder is None:
            if plugin_extension and isinstance(ref_kind, str) and ref_kind:
                outputs.append(ActionOutput(kind=ref_kind, name=output.name, ref=ref))
                continue
            raise ValueError(
                "unknown_receipt_output_ref_kind: "
                f"receipt={receipt.receipt_id!r}, output={output.name!r}, "
                f"ref_kind={ref_kind!r}"
            )
        outputs.append(builder(output, ref))
    return ActionResult(
        action=ActionIdentity(
            kind=receipt.action_kind,
            action_id=receipt.action_id,
        ),
        status=receipt.status,
        project_id=receipt.project_id,
        run_id=receipt.run_id,
        op_ids=receipt.op_ids,
        outputs=outputs,
        value=receipt.value,
        receipt_id=receipt.receipt_id,
        errors=receipt.errors,
        warnings=receipt.warnings,
    )


def _receipt_ref(receipt: Receipt, kind: str) -> dict[str, Any] | None:
    for section in (receipt.inputs, receipt.outputs, receipt.evidence):
        for item in section:
            ref = item.ref
            if ref.get("kind") == kind:
                return dict(ref)
    return None


def _positive_ref_int(ref: dict[str, Any], field: str) -> int | None:
    value = ref.get(field)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _receipt_ops_are_applied(project: Any, op_ids: list[int]) -> bool:
    if not op_ids:
        return False
    placeholders = ",".join("?" for _ in op_ids)
    rows = project.db.execute(
        f"SELECT id, status FROM ops WHERE id IN ({placeholders})",
        op_ids,
    ).fetchall()
    by_id = {int(row["id"]): str(row["status"]) for row in rows}
    return all(by_id.get(op_id) == "applied" for op_id in op_ids)

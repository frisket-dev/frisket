from __future__ import annotations

import pytest

from frisket.contracts.action import Receipt, ReceiptIO
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
    SingleParentChildSheetPlan,
    SingleParentMaterializedRow,
    write_single_parent_child_sheet,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_output_scope import (
    OutputReadGrant,
    UnavailableOutput,
    derive_output_read_grants,
)
from frisket.server.services.project_qa_tools import ProjectQAScopeError, ProjectQATools


def _generated_column(project: Project, sheet_id: int, row_ids: list[int]):
    result = run_action_spec(
        project,
        {
            "action_id": "map.regex_extract",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"input_columns": ["source"], "pattern": r"\d+"},
            "output_names": {"extracted": "answer"},
            "idempotency_key": "output-scope@1",
        },
        project_id="project-1",
    )
    assert result.status == "completed", result.errors
    assert result.receipt_id is not None
    assert result.outputs[0].column_id is not None
    return result


def test_generated_column_grant_is_limited_to_its_exact_cells(tmp_path) -> None:
    project = Project.create(tmp_path / "column-grant.frisket")
    try:
        sheet_id = project.add_sheet("Data")
        source_id = project.add_column(sheet_id, "source")
        row_ids = project.add_rows(
            sheet_id,
            [{"source": "one 101"}, {"source": "two 202"}],
            {"source": source_id},
        )
        result = _generated_column(project, sheet_id, [row_ids[0]])
        output_id = int(result.outputs[0].column_id)

        resolved = derive_output_read_grants(project, [str(result.receipt_id)])

        assert resolved.grants == (
            OutputReadGrant(
                receipt_id=str(result.receipt_id),
                sheet_id=sheet_id,
                column_ids=frozenset({output_id}),
                row_ids=frozenset({row_ids[0]}),
            ),
        )
        assert resolved.unavailable == ()
        assert source_id not in resolved.grants[0].column_ids
        assert row_ids[1] not in resolved.grants[0].row_ids
    finally:
        project.close()


def test_generated_column_grant_fails_closed_after_one_cell_is_overwritten(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "stale-column-grant.frisket")
    try:
        sheet_id = project.add_sheet("Data")
        source_id = project.add_column(sheet_id, "source")
        row_ids = project.add_rows(
            sheet_id,
            [{"source": "one 101"}, {"source": "two 202"}],
            {"source": source_id},
        )
        result = _generated_column(project, sheet_id, row_ids)
        output_id = int(result.outputs[0].column_id)
        project.apply_edits(
            [{"row_id": row_ids[1], "column_id": output_id, "value": "new"}]
        )

        resolved = derive_output_read_grants(project, [str(result.receipt_id)])

        assert resolved.grants == ()
        assert resolved.unavailable == (
            UnavailableOutput(
                receipt_id=str(result.receipt_id),
                output_name="answer",
                reason="output_not_current",
            ),
        )
    finally:
        project.close()


def test_materialized_sheet_requires_its_current_applied_creation_op(tmp_path) -> None:
    project = Project.create(tmp_path / "sheet-grant.frisket")
    try:
        source_sheet_id = project.add_sheet("Source")
        source_column_id = project.add_column(source_sheet_id, "source")
        source_row_id = project.add_rows(
            source_sheet_id,
            [{"source": "one"}],
            {"source": source_column_id},
        )[0]
        cursor = project.db.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        write = write_single_parent_child_sheet(
            cursor,
            SingleParentChildSheetPlan(
                action_kind="table.test_output",
                label="test output",
                target_sheet_name="Research output",
                parent_sheet_id=source_sheet_id,
                op_spec={"kind": "test_output"},
                columns=[MaterializedColumnSpec("answer", "text")],
                rows=[
                    SingleParentMaterializedRow(
                        parent_row_id=source_row_id,
                        values={"answer": "result"},
                    )
                ],
            ),
        )
        project.db.commit()
        receipt = Receipt(
            receipt_id="receipt-sheet",
            project_id="project-1",
            action_id="table.test_output",
            action_kind="table.test_output",
            op_ids=[write.op_id],
            status="completed",
            outputs=[
                ReceiptIO(
                    name="Research output",
                    ref=write.materialized_sheet_ref,
                ),
                ReceiptIO(
                    name="answer",
                    ref=write.materialized_column_refs["answer"],
                ),
                ReceiptIO(name="rows", ref=write.materialized_rows_ref),
            ],
        )
        ReceiptStore(project).insert(receipt)

        resolved = derive_output_read_grants(project, [receipt.receipt_id])

        assert resolved.grants == (
            OutputReadGrant(
                receipt_id=receipt.receipt_id,
                sheet_id=write.sheet_id,
                column_ids=None,
                row_ids=None,
            ),
        )
        assert resolved.unavailable == ()

        project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (write.op_id,))
        project.db.commit()
        stale = derive_output_read_grants(project, [receipt.receipt_id])
        assert stale.grants == ()
        assert stale.unavailable == (
            UnavailableOutput(
                receipt_id=receipt.receipt_id,
                output_name="Research output",
                reason="output_not_current",
            ),
            UnavailableOutput(
                receipt_id=receipt.receipt_id,
                output_name="answer",
                reason="output_not_supported",
            ),
            UnavailableOutput(
                receipt_id=receipt.receipt_id,
                output_name="rows",
                reason="output_not_supported",
            ),
        )
    finally:
        project.close()


def test_missing_unsuccessful_and_source_outputs_report_unavailability(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "unavailable-output.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        source_column_id = project.add_column(sheet_id, "source")
        row_id = project.add_rows(
            sheet_id, [{"source": "one"}], {"source": source_column_id}
        )[0]
        failed = Receipt(
            receipt_id="receipt-failed",
            project_id="project-1",
            action_id="map.test",
            action_kind="map.test",
            status="failed",
        )
        source = Receipt(
            receipt_id="receipt-source",
            project_id="project-1",
            action_id="source.test",
            action_kind="source.test",
            status="completed",
            outputs=[
                ReceiptIO(
                    name="source",
                    ref={
                        "kind": "source_column",
                        "sheet_id": sheet_id,
                        "column_id": source_column_id,
                        "row_ids": [row_id],
                    },
                )
            ],
        )
        added_rows = Receipt(
            receipt_id="receipt-added-rows",
            project_id="project-1",
            action_id="rows.add",
            action_kind="rows.add",
            status="completed",
            outputs=[
                ReceiptIO(
                    name="rows",
                    ref={
                        "kind": "added_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [row_id],
                    },
                )
            ],
        )
        ReceiptStore(project).insert(failed)
        ReceiptStore(project).insert(source)
        ReceiptStore(project).insert(added_rows)

        resolved = derive_output_read_grants(
            project,
            [
                "receipt-missing",
                failed.receipt_id,
                source.receipt_id,
                added_rows.receipt_id,
            ],
        )

        assert resolved.grants == ()
        assert resolved.unavailable == (
            UnavailableOutput(
                receipt_id="receipt-missing",
                output_name=None,
                reason="receipt_not_found",
            ),
            UnavailableOutput(
                receipt_id=failed.receipt_id,
                output_name=None,
                reason="receipt_not_completed",
            ),
            UnavailableOutput(
                receipt_id=source.receipt_id,
                output_name="source",
                reason="output_not_supported",
            ),
            UnavailableOutput(
                receipt_id=added_rows.receipt_id,
                output_name="rows",
                reason="output_not_supported",
            ),
        )
    finally:
        project.close()


def test_file_scope_adds_only_current_generated_cells_to_reads_and_queries(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "tools-column-grant.frisket")
    try:
        sheet_id = project.add_sheet("Data")
        source_id = project.add_column(sheet_id, "source")
        sibling_id = project.add_column(sheet_id, "private")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"source": "one 101", "private": "secret one"},
                {"source": "two 202", "private": "secret two"},
            ],
            {"source": source_id, "private": sibling_id},
        )
        unrelated_sheet = project.add_sheet("Unrelated")
        unrelated_column = project.add_column(unrelated_sheet, "secret")
        project.add_rows(
            unrelated_sheet, [{"secret": "hidden"}], {"secret": unrelated_column}
        )
        result = _generated_column(project, sheet_id, [row_ids[0]])
        output_id = int(result.outputs[0].column_id)
        receipt_ids = [str(result.receipt_id)]
        calls = 0

        def grants():
            nonlocal calls
            calls += 1
            return derive_output_read_grants(project, receipt_ids)

        scope = {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet_id,
                    "row_id": row_ids[0],
                    "column_id": source_id,
                }
            ],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Research", scope=scope
        )
        tools = ProjectQATools(project, turn, store, output_grants_provider=grants)

        inspected = tools.inspect_sheets()
        assert calls == 1
        assert [sheet["sheet_id"] for sheet in inspected["sheets"]] == [sheet_id]
        assert {
            column["column_id"] for column in inspected["sheets"][0]["columns"]
        } == {source_id, output_id}

        read = tools.read_rows(sheet_id, [row_ids[0]], [source_id, output_id])
        assert calls == 2
        assert {cell["column_id"] for cell in read["rows"][0]["cells"]} == {
            source_id,
            output_id,
        }
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(sheet_id, [row_ids[0]], [sibling_id])
        with pytest.raises(ProjectQAScopeError, match="row is outside"):
            tools.read_rows(sheet_id, [row_ids[1]], [output_id])
        with pytest.raises(ProjectQAScopeError, match="sheet is outside"):
            tools.read_rows(unrelated_sheet)

        query = tools.query_rows(
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
                "filter": {"answer": {"eq": "101"}},
            }
        )
        assert query["row_ids"] == [row_ids[0]]
        with pytest.raises(ProjectQAScopeError):
            tools.query_rows(
                {
                    "schema_version": "frisket.query.v1",
                    "kind": "sheet.filter",
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filter": {"private": {"contains": "secret"}},
                }
            )

        project.apply_edits(
            [{"row_id": row_ids[0], "column_id": output_id, "value": "new"}]
        )
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(sheet_id, [row_ids[0]], [output_id])
        source = tools.read_rows(sheet_id, [row_ids[0]], [source_id])
        assert source["rows"][0]["cells"][0]["value"] == "one 101"
    finally:
        project.close()


def test_materialized_sheet_grant_is_visible_without_broadening_other_sheets(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "tools-sheet-grant.frisket")
    try:
        source_sheet_id = project.add_sheet("Source")
        source_column_id = project.add_column(source_sheet_id, "source")
        source_row_id = project.add_rows(
            source_sheet_id,
            [{"source": "one"}],
            {"source": source_column_id},
        )[0]
        unrelated_sheet = project.add_sheet("Unrelated")
        cursor = project.db.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        write = write_single_parent_child_sheet(
            cursor,
            SingleParentChildSheetPlan(
                action_kind="table.test_output",
                label="test output",
                target_sheet_name="Research output",
                parent_sheet_id=source_sheet_id,
                op_spec={"kind": "test_output"},
                columns=[MaterializedColumnSpec("answer", "text")],
                rows=[
                    SingleParentMaterializedRow(
                        parent_row_id=source_row_id,
                        values={"answer": "result"},
                    )
                ],
            ),
        )
        project.db.commit()
        receipt = Receipt(
            receipt_id="receipt-tools-sheet",
            project_id="project-1",
            action_id="table.test_output",
            action_kind="table.test_output",
            op_ids=[write.op_id],
            status="completed",
            outputs=[
                ReceiptIO(name="Research output", ref=write.materialized_sheet_ref)
            ],
        )
        ReceiptStore(project).insert(receipt)
        scope = {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": source_sheet_id,
                    "row_id": source_row_id,
                    "column_id": source_column_id,
                }
            ],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Research", scope=scope
        )
        tools = ProjectQATools(
            project,
            turn,
            store,
            output_grants_provider=lambda: derive_output_read_grants(
                project, [receipt.receipt_id]
            ),
        )

        inspected = tools.inspect_sheets()
        assert {sheet["sheet_id"] for sheet in inspected["sheets"]} == {
            source_sheet_id,
            write.sheet_id,
        }
        assert tools.read_rows(write.sheet_id)["rows"][0]["cells"][0]["value"] == (
            "result"
        )
        with pytest.raises(ProjectQAScopeError, match="sheet is outside"):
            tools.read_rows(unrelated_sheet)
    finally:
        project.close()


def test_research_reads_remain_per_call_bounded_without_the_ordinary_total_cap(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "research-read-budget.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        text_id = project.add_column(sheet_id, "text")
        row_id = project.add_rows(
            sheet_id,
            [{"text": "x" * 70_000}],
            {"text": text_id},
        )[0]
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Read the document"
        )
        ordinary = ProjectQATools(project, turn, store)
        citation_id = ordinary.read_rows(sheet_id, [row_id], [text_id])["rows"][0][
            "cells"
        ][0]["citation_id"]

        cursor = None
        ordinary_chars = 0
        for _ in range(20):
            opened = ordinary.open_source(citation_id, cursor=cursor)
            ordinary_chars += sum(len(part["text"]) for part in opened["passages"])
            cursor = opened.get("next_cursor")
            if not opened["passages"] or cursor is None:
                break
        assert ordinary_chars == 64_000
        assert opened["passages"] == []
        assert opened["coverage"] == "source reading budget exhausted"

        research = ProjectQATools(project, turn, store, research=True)
        cursor = None
        research_chars = 0
        for _ in range(20):
            opened = research.open_source(citation_id, cursor=cursor)
            chunk_chars = sum(len(part["text"]) for part in opened["passages"])
            assert chunk_chars <= 8_000
            research_chars += chunk_chars
            assert opened["remaining_read_chars"] is None
            cursor = opened["next_cursor"]
            if cursor is None:
                break
        assert research_chars == 70_000
        assert research_chars > ordinary_chars
    finally:
        project.close()


def test_research_semantic_search_gets_a_fresh_local_embedding_budget_each_call(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "research-embedding-budget.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        text_id = project.add_column(sheet_id, "text")
        project.add_rows(sheet_id, [{"text": "one"}], {"text": text_id})
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Search the document"
        )
        observed: list[int] = []

        def semantic_stub(*_args, **kwargs):
            observed.append(kwargs["remaining_embeddings"])
            return {
                "hits": [],
                "new_embeddings": 64,
                "coverage": {"complete": True, "semantic": True},
            }

        monkeypatch.setattr(
            "frisket.server.services.project_qa_tools.semantic_passage_search",
            semantic_stub,
        )
        ordinary = ProjectQATools(project, turn, store)
        ordinary.search_cells("one", sheet_id, mode="semantic")
        ordinary.search_cells("one", sheet_id, mode="semantic")
        assert observed == [64, 0]

        observed.clear()
        research = ProjectQATools(project, turn, store, research=True)
        research.search_cells("one", sheet_id, mode="semantic")
        research.search_cells("one", sheet_id, mode="semantic")
        assert observed == [64, 64]
    finally:
        project.close()

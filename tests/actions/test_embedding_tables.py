"""Reusable table read admission, source ownership, and published snapshot replay."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.executor.embedding_read import AdmittedEmbeddingIndexReader
from frisket.engine.store import Project
from helpers import replace_test_source_cells
from frisket.sdk import (
    ActionParams,
    EmbeddingIndexReader,
    ListColumnSource,
    ListTableReader,
    RowSource,
    TableResult,
    TableRow,
    action,
    create_sheet,
)


class RenamedParams(ActionParams):
    identifier: str
    required: int = 1


class Value(BaseModel):
    key: str


class ListAggregateParams(ActionParams):
    source: ListColumnSource


def aggregate_list_items(
    params: ListAggregateParams, lists: ListTableReader
) -> TableResult[Value]:
    items = lists.read(params.source)
    return TableResult(
        rows=[
            TableRow(
                output=Value(key=", ".join(str(item.value) for item in items)),
                sources=tuple(item.source for item in items),
            )
        ]
    )


class Gateway:
    def embed(self, texts, *, provider, model, modality):
        return build_batch_result(
            [[float(index + 1)] + [0.0] * 383 for index, _ in enumerate(texts)],
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture
def seeded(tmp_path):
    project = Project.create(tmp_path / "tables")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "text")
    project.add_rows(sheet, [{"text": "a"}, {"text": "b"}], {"text": column})
    create = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "idempotency_key": "index",
            "params": {
                "sheet_id": sheet,
                "source_columns": ["text"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
        },
        project_id="p",
    )
    assert create.status == "completed", create.errors
    index_id = create.outputs[0].ref["index_id"]
    refresh = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "idempotency_key": "refresh",
            "params": {"index_id": index_id, "mode": "full"},
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=Gateway()),
    )
    assert refresh.status == "completed", refresh.errors
    yield project, index_id
    project.close()


def bound_for(producer, index_id, **changes):
    registered = RegisteredAction(
        "test.table",
        action(
            name="table",
            title="Table",
            description="Table",
            category=ActionCategory.SOURCES,
            run=create_sheet(producer),
        ),
    )
    request = ActionRequest(
        action_id="test.table",
        scope={"kind": "project"},
        sheet_name="Output",
        params={"identifier": "prefix:" + index_id},
        idempotency_key="run",
    ).model_copy(update=changes)
    return BoundTypedActionRequest.bind(registered, request)


@pytest.mark.parametrize(
    "mode", ["cross_sheet", "source_free_prefix", "unemitted_admission"]
)
def test_lazy_reader_cannot_change_published_parent_sheet(seeded, mode):
    project, first_index = seeded
    sheet = project.add_sheet("Other source")
    column = project.add_column(sheet, "text")
    project.add_rows(sheet, [{"text": "other"}], {"text": column})
    created = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "idempotency_key": "other-index",
            "params": {
                "sheet_id": sheet,
                "source_columns": ["text"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
        },
        project_id="p",
    )
    assert created.status == "completed", created.errors
    second_index = created.outputs[0].ref["index_id"]
    refreshed = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "idempotency_key": "other-refresh",
            "params": {"index_id": second_index, "mode": "full"},
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=Gateway()),
    )
    assert refreshed.status == "completed", refreshed.errors

    def produce(
        params: RenamedParams, reader: EmbeddingIndexReader
    ) -> TableResult[Value]:
        def rows():
            if mode == "source_free_prefix":
                yield TableRow(output=Value(key="unparented"))
            else:
                item = reader.read(first_index)[0]
                yield TableRow(
                    output=Value(key=item.source_key),
                    sources=(item.source,),
                    parent=item.source,
                )
            items = reader.read(second_index)
            if mode != "unemitted_admission":
                item = items[0]
                yield TableRow(
                    output=Value(key=item.source_key),
                    sources=(item.source,),
                    parent=item.source,
                )

        return TableResult(rows=rows())

    def counts():
        return {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sheets",
                "columns",
                "rows",
                "ops",
                "receipts",
                "materialized_row_sources",
            )
        }

    before = counts()
    result = run_typed_create_sheet_action(
        project, "p", bound_for(produce, first_index)
    )
    assert result.status == "failed", result
    assert counts() == before


def read_renamed(
    params: RenamedParams, indexes: EmbeddingIndexReader
) -> TableResult[Value]:
    entries = indexes.read(
        params.identifier.removeprefix("prefix:"), minimum_rows=params.required
    )
    return TableResult(
        rows=[
            TableRow(
                output=Value(key=entry.source_key),
                sources=(entry.source,),
                parent=entry.source,
            )
            for entry in entries
            for _ in range(2)
        ]
    )


def test_renamed_derived_reader_arguments_and_repeated_sources(seeded):
    project, index_id = seeded
    bound = bound_for(read_renamed, index_id, output_names={"key": "Renamed"})
    result = run_typed_create_sheet_action(project, "p", bound)
    assert result.status == "completed", result.errors
    ref = result.outputs[0].ref
    assert ref["row_count"] == 4
    assert ref["reads"][0]["index_id"] == index_id
    assert ref["reads"][0]["minimum_rows"] == 1
    assert "Renamed" in ref["columns"]
    op = json.loads(
        project.db.execute(
            "SELECT spec FROM ops WHERE id=?", (result.op_ids[0],)
        ).fetchone()[0]
    )
    assert op["params"] == {"identifier": "prefix:" + index_id}
    assert op["reads"] == ref["reads"]


def test_source_admission_does_not_rescan_all_tokens_per_output(seeded, monkeypatch):
    project, index_id = seeded
    visits = []
    original = AdmittedEmbeddingIndexReader.read

    class CountedSources(set):
        def __iter__(self):
            for source in super().__iter__():
                visits.append(source)
                yield source

    def read(self, *args, **kwargs):
        entries = original(self, *args, **kwargs)
        self.sources = CountedSources(self.sources)
        return entries

    monkeypatch.setattr(AdmittedEmbeddingIndexReader, "read", read)
    result = run_typed_create_sheet_action(
        project, "p", bound_for(read_renamed, index_id)
    )
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["row_count"] == 4
    # Table-source discovery may walk the bag; each emitted row must use
    # membership lookup rather than scanning every admitted token again.
    assert len(visits) <= 2 * len(set(visits))


def test_reader_refusal_does_not_assume_builtin_params_fields(seeded):
    project, index_id = seeded
    bound = bound_for(
        read_renamed,
        index_id,
        params={"identifier": "prefix:" + index_id, "required": 99},
    )
    result = run_typed_create_sheet_action(project, "p", bound)
    assert result.errors[0].code == "embedding_analysis_insufficient_rows"
    assert result.errors[0].field == "params"
    assert result.errors[0].details == {"ready": 2, "minimum_rows": 99}


@pytest.mark.parametrize("shape", ["fabricated", "copied", "multiple"])
def test_unadmitted_and_unsupported_sources_refuse_without_writes(seeded, shape):
    project, index_id = seeded

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        source = indexes.read(params.identifier.removeprefix("prefix:"))[0].source
        sources = {
            "fabricated": (RowSource(sheet_id=source.sheet_id, row_id=source.row_id),),
            "copied": (replace(source),),
            "multiple": (source, source),
        }[shape]
        return TableResult(
            rows=[
                TableRow(output=Value(key="first"), sources=(source,)),
                TableRow(output=Value(key="second"), sources=sources),
            ]
        )

    before = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    result = run_typed_create_sheet_action(project, "p", bound_for(produce, index_id))
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_params"
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before
    assert (
        project.db.execute("SELECT 1 FROM sheets WHERE name='Output'").fetchone()
        is None
    )


@pytest.mark.parametrize("empty", [False, True])
def test_reads_remain_authoritative_with_source_free_or_empty_output(seeded, empty):
    project, index_id = seeded

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        indexes.read(params.identifier.removeprefix("prefix:"))
        return TableResult(
            rows=[] if empty else [TableRow(output=Value(key="summary"))]
        )

    result = run_typed_create_sheet_action(project, "p", bound_for(produce, index_id))
    assert result.status == "completed", result.errors
    ref = result.outputs[0].ref
    assert ref["reads"][0]["index_id"] == index_id
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()[0]
    )
    assert any(item["ref"] == ref["reads"][0] for item in receipt["inputs"])


def test_explicit_parent_and_all_contributors_support_mixed_rows(seeded):
    project, index_id = seeded
    admitted = []

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        left, right = [
            entry.source
            for entry in indexes.read(params.identifier.removeprefix("prefix:"))
        ]
        admitted.extend((left, right))
        return TableResult(
            rows=[
                TableRow(output=Value(key="detail"), sources=(left,), parent=left),
                TableRow(output=Value(key="singleton"), sources=(left,)),
                TableRow(output=Value(key="total"), sources=(left, right)),
                TableRow(
                    output=Value(key="preferred"), sources=(left, right), parent=right
                ),
                TableRow(output=Value(key="source-free")),
            ]
        )

    bound = bound_for(produce, index_id)
    result = run_typed_create_sheet_action(project, "p", bound)
    assert result.status == "completed", result.errors
    sheet_id = result.outputs[0].sheet_id
    rows = project.db.execute(
        "SELECT id,parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (sheet_id,),
    ).fetchall()
    left, right = admitted
    assert [row["parent_row_id"] for row in rows] == [
        left.row_id,
        None,
        None,
        right.row_id,
        None,
    ]
    assert (
        project.db.execute(
            "SELECT parent_sheet_id FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()[0]
        == left.sheet_id
    )
    membership = project.db.execute(
        "SELECT materialized_row_id,source_row_id,role FROM materialized_row_sources WHERE op_id=?",
        (result.op_ids[0],),
    ).fetchall()
    assert {(row[0], row[1], row[2]) for row in membership} == {
        (rows[1]["id"], left.row_id, "aggregate_source"),
        (rows[2]["id"], left.row_id, "aggregate_source"),
        (rows[2]["id"], right.row_id, "aggregate_source"),
        (rows[3]["id"], left.row_id, "aggregate_source"),
    }
    assert (
        run_typed_create_sheet_action(project, "p", bound).receipt_id
        == result.receipt_id
    )
    project.db.execute(
        "DELETE FROM materialized_row_sources WHERE materialized_row_id=?",
        (rows[1]["id"],),
    )
    project.db.commit()
    replay = run_typed_create_sheet_action(project, "p", bound)
    assert replay.status == "failed" and replay.errors[0].code == "stale_replay"


@pytest.mark.parametrize("parent_kind", ["copy", "not_contributor", "unadmitted"])
def test_parent_must_be_exact_admitted_contributor(seeded, parent_kind):
    project, index_id = seeded

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        left, right = [
            entry.source
            for entry in indexes.read(params.identifier.removeprefix("prefix:"))
        ]
        parent = {
            "copy": replace(left),
            "not_contributor": right,
            "unadmitted": RowSource(left.sheet_id, 999999),
        }[parent_kind]
        return TableResult(
            rows=[TableRow(output=Value(key="bad"), sources=(left,), parent=parent)]
        )

    before = tuple(project.db.iterdump())
    result = run_typed_create_sheet_action(project, "p", bound_for(produce, index_id))
    assert result.status == "failed" and result.errors[0].code == "invalid_params"
    assert tuple(project.db.iterdump()) == before


def test_admitted_roles_are_preserved_even_for_preferred_parent(seeded, monkeypatch):
    project, index_id = seeded
    original_read = AdmittedEmbeddingIndexReader.read

    def read(self, *args, **kwargs):
        entries = original_read(self, *args, **kwargs)
        self.source_roles = {
            entries[0].source: "join_left",
            entries[1].source: "join_right",
        }
        return entries

    monkeypatch.setattr(AdmittedEmbeddingIndexReader, "read", read)

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        left, right = [
            entry.source
            for entry in indexes.read(params.identifier.removeprefix("prefix:"))
        ]
        return TableResult(
            rows=[
                TableRow(output=Value(key="joined"), sources=(right, left), parent=left)
            ]
        )

    result = run_typed_create_sheet_action(project, "p", bound_for(produce, index_id))
    assert result.status == "completed", result.errors
    assert {
        row[0]
        for row in project.db.execute(
            "SELECT role FROM materialized_row_sources WHERE op_id=?",
            (result.op_ids[0],),
        )
    } == {"join_left", "join_right"}


def test_distinct_list_item_tokens_share_one_canonical_row_membership(tmp_path):
    project = Project.create(tmp_path / "list-contributors")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "items", type="json")
        row_id = project.add_rows(
            sheet, [{"items": ["alpha", "beta"]}], {"items": column}
        )[0]
        registered = RegisteredAction(
            "test.list_aggregate",
            action(
                name="list_aggregate",
                title="Aggregate",
                description="Aggregate list items",
                category=ActionCategory.CONVERT,
                run=create_sheet(aggregate_list_items),
            ),
        )
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id=registered.action_id,
                scope={"kind": "project"},
                sheet_name="Aggregate",
                params={
                    "source": {"kind": "column", "sheet_id": sheet, "column_id": column}
                },
                idempotency_key="aggregate",
            ),
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        output_sheet = result.outputs[0].sheet_id
        assert (
            project.db.execute(
                "SELECT parent_row_id FROM rows WHERE sheet_id=?", (output_sheet,)
            ).fetchone()[0]
            is None
        )
        membership = project.db.execute(
            "SELECT source_row_id,role FROM materialized_row_sources WHERE op_id=?",
            (result.op_ids[0],),
        ).fetchall()
        assert [(row[0], row[1]) for row in membership] == [
            (row_id, "aggregate_source")
        ]
        assert (
            run_typed_create_sheet_action(project, "p", bound).receipt_id
            == result.receipt_id
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    "tamper",
    [
        "null_parent",
        "hidden_column",
        "missing_marker",
        "wrong_marker",
        "hidden_sheet",
        "missing_refs",
        "sheet_id_dict",
        "sheet_id_list",
        "op_id_dict",
        "op_id_list",
    ],
)
def test_parented_table_replay_rejects_tampered_publication_without_read(
    seeded, monkeypatch, tamper
):
    project, index_id = seeded
    bound = bound_for(read_renamed, index_id)
    result = run_typed_create_sheet_action(project, "p", bound)
    assert result.status == "completed", result.errors
    sheet_id = result.outputs[0].sheet_id
    if tamper == "null_parent":
        project.db.execute(
            "UPDATE rows SET parent_row_id=NULL WHERE sheet_id=?", (sheet_id,)
        )
    elif tamper == "hidden_column":
        project.db.execute("UPDATE columns SET hidden=1 WHERE sheet_id=?", (sheet_id,))
    elif tamper == "hidden_sheet":
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
    else:
        body = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
        )
        ref = body["outputs"][0]["ref"]
        if tamper == "missing_marker":
            ref.pop("parent_sheet_id")
        elif tamper == "wrong_marker":
            ref["parent_sheet_id"] = 99999
        elif tamper.endswith(("_dict", "_list")):
            field, value_kind = tamper.rsplit("_", 1)
            ref[field] = {} if value_kind == "dict" else []
        else:
            body["outputs"] = [body["outputs"][0]]
            body["evidence"] = []
        project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (json.dumps(body), result.receipt_id),
        )
    project.db.commit()

    def no_read(*args, **kwargs):
        raise AssertionError("replay must not reread vectors")

    monkeypatch.setattr(AdmittedEmbeddingIndexReader, "read", no_read)
    replay = run_typed_create_sheet_action(project, "p", bound)
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"


def test_reader_unknown_or_duplicate_injection_refuses_registration():
    def unknown(params: RenamedParams, other: object) -> TableResult[Value]:
        return TableResult(rows=[])

    def duplicate(
        params: RenamedParams, first: EmbeddingIndexReader, second: EmbeddingIndexReader
    ) -> TableResult[Value]:
        return TableResult(rows=[])

    with pytest.raises(TypeError, match="create_sheet supports only"):
        create_sheet(unknown)
    with pytest.raises(TypeError, match="repeated"):
        create_sheet(duplicate)


def test_source_reference_cannot_escape_its_invocation(seeded):
    project, index_id = seeded
    previous = []

    def produce(
        params: RenamedParams, indexes: EmbeddingIndexReader
    ) -> TableResult[Value]:
        source = indexes.read(params.identifier.removeprefix("prefix:"))[0].source
        previous.append(source)
        return TableResult(
            rows=[TableRow(output=Value(key="value"), sources=(previous[0],))]
        )

    first = run_typed_create_sheet_action(project, "p", bound_for(produce, index_id))
    assert first.status == "completed", first.errors
    second = run_typed_create_sheet_action(
        project,
        "p",
        bound_for(
            produce,
            index_id,
            sheet_name="Second",
            idempotency_key="second",
        ),
    )
    assert second.status == "failed"
    assert second.errors[0].code == "invalid_params"


def test_import_workload_limit_does_not_limit_derived_tables(seeded):
    from frisket.engine.executor import ImportWorkloadLimits

    project, index_id = seeded
    result = run_typed_create_sheet_action(
        project,
        "p",
        bound_for(read_renamed, index_id),
        deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=1)),
    )
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["row_count"] == 4


def test_source_change_does_not_rerun_a_published_table(seeded, monkeypatch):
    project, index_id = seeded
    bound = bound_for(read_renamed, index_id)
    first = run_typed_create_sheet_action(project, "p", bound)
    assert first.status == "completed", first.errors
    column = project.db.execute(
        "SELECT id,sheet_id FROM columns WHERE name='text'"
    ).fetchone()
    assert column is not None
    column_id = int(column["id"])
    replace_test_source_cells(
        project,
        (
            (row_id, column_id, "changed")
            for row_id in project.visible_row_ids(int(column["sheet_id"]))
        ),
    )
    original_read = AdmittedEmbeddingIndexReader.read

    def no_read(*args, **kwargs):
        raise AssertionError("completed table replay must not reread the source")

    monkeypatch.setattr(AdmittedEmbeddingIndexReader, "read", no_read)
    replay = run_typed_create_sheet_action(project, "p", bound)
    assert replay.status == "completed"
    assert replay.receipt_id == first.receipt_id
    monkeypatch.setattr(AdmittedEmbeddingIndexReader, "read", original_read)
    fresh = run_typed_create_sheet_action(
        project,
        "p",
        bound_for(
            read_renamed,
            index_id,
            sheet_name="Fresh",
            idempotency_key="fresh",
        ),
    )
    assert fresh.errors[0].code == "embedding_source_stale"

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from frisket.contracts.action import Receipt
from frisket.ai.embeddings import build_batch_result
from frisket.ai.embeddings.vector_backend import VectorBackend
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
    SingleParentChildSheetPlan,
    SingleParentMaterializedRow,
    write_single_parent_child_sheet,
)
from frisket.engine.store import Project


_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class _MappedGateway:
    dim = 384

    def embed(self, texts, *, provider, model, modality):
        out = [
            list(_VECTORS.get(text, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3)
            for text in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _project(tmp_path: Path) -> tuple[Project, int, int]:
    project = Project.create(tmp_path / "project.frisket", name="p")
    sheet_id = project.add_sheet("s")
    column_id = project.add_column(sheet_id, "headline")
    project.add_rows(
        sheet_id,
        [{"headline": text} for text in _VECTORS],
        {"headline": column_id},
    )
    return project, sheet_id, column_id


def _create_index(project: Project, sheet_id: int) -> str:
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet_id,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "index-create@stage3",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    return str(result.outputs[0].ref["index_id"])


def _refresh(project: Project, index_id: str) -> None:
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "index-refresh@stage3",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=_MappedGateway()),
    )
    assert result.status == "completed", result.errors


def _run_embedding_action(project: Project, action: dict) -> Receipt:
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    assert result.receipt_id is not None
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def test_single_parent_writer_records_rows_undo_and_standard_refs(tmp_path: Path):
    project = Project.create(tmp_path / "helper.frisket", name="helper")
    source_sheet_id = project.add_sheet("Source")
    source_column_id = project.add_column(source_sheet_id, "name")
    project.add_rows(
        source_sheet_id,
        [{"name": "Alice"}, {"name": "Bob"}],
        {"name": source_column_id},
    )
    parent_row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
            (source_sheet_id,),
        ).fetchall()
    ]

    cur = project.db.cursor()
    cur.execute("BEGIN IMMEDIATE")
    write = write_single_parent_child_sheet(
        cur,
        SingleParentChildSheetPlan(
            action_kind="test.single_parent_writer",
            label="test child writer",
            target_sheet_name="Children",
            parent_sheet_id=source_sheet_id,
            op_spec={"kind": "test"},
            columns=[
                MaterializedColumnSpec("name", "text", ai_generated=True),
                MaterializedColumnSpec("score", "number"),
            ],
            rows=[
                SingleParentMaterializedRow(
                    parent_row_id=parent_row_ids[0],
                    values={"name": "Alice", "score": 0.9},
                ),
                SingleParentMaterializedRow(
                    parent_row_id=parent_row_ids[1],
                    values={"name": "Bob", "score": None},
                ),
            ],
        ),
    )
    project.db.commit()

    child = project.db.execute(
        "SELECT * FROM sheets WHERE id=?", (write.sheet_id,)
    ).fetchone()
    assert child["parent_sheet_id"] == source_sheet_id
    assert child["parent_op_id"] == write.op_id
    rows = project.db.execute(
        "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (write.sheet_id,),
    ).fetchall()
    assert [int(row["id"]) for row in rows] == write.row_ids
    assert [int(row["parent_row_id"]) for row in rows] == parent_row_ids
    assert write.materialized_rows_ref["parent_row_ids"] == parent_row_ids
    assert write.lineage_parent_rows_ref["pairs"] == [
        {"child_row_id": write.row_ids[0], "parent_row_id": parent_row_ids[0]},
        {"child_row_id": write.row_ids[1], "parent_row_id": parent_row_ids[1]},
    ]
    op = project.db.execute("SELECT undo_info FROM ops WHERE id=?", (write.op_id,))
    assert json.loads(op.fetchone()["undo_info"]) == write.undo_info
    score_values = project.get_values(write.sheet_id, write.column_ids["score"])
    assert score_values[write.row_ids[0]] == 0.9
    assert score_values[write.row_ids[1]] is None


def test_single_parent_writer_rejects_parentless_rows(tmp_path: Path):
    project = Project.create(tmp_path / "bad-helper.frisket", name="bad-helper")
    source_sheet_id = project.add_sheet("Source")
    cur = project.db.cursor()
    cur.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="parent_row_id"):
        write_single_parent_child_sheet(
            cur,
            SingleParentChildSheetPlan(
                action_kind="test.single_parent_writer",
                label="bad child writer",
                target_sheet_name="Bad Children",
                parent_sheet_id=source_sheet_id,
                op_spec={},
                columns=[MaterializedColumnSpec("name", "text")],
                rows=[
                    SingleParentMaterializedRow(  # type: ignore[arg-type]
                        parent_row_id=None,
                        values={"name": "Alice"},
                    )
                ],
            ),
        )
    project.db.rollback()


def test_embedding_project_and_cluster_publish_standard_refs_with_lineage_evidence(
    tmp_path: Path,
):
    project, sheet_id, _column_id = _project(tmp_path)
    source_row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
    ]
    index_id = _create_index(project, sheet_id)
    _refresh(project, index_id)

    project_receipt = _run_embedding_action(
        project,
        {
            "action_id": "embedding.index_project",
            "scope": {"kind": "project"},
            "sheet_name": "pca",
            "params": {
                "index_id": index_id,
                "method": "pca",
                "dimensions": 2,
            },
            "idempotency_key": "index-project@stage3",
        },
    )
    cluster_receipt = _run_embedding_action(
        project,
        {
            "action_id": "embedding.index_cluster",
            "scope": {"kind": "project"},
            "sheet_name": "clusters",
            "params": {
                "index_id": index_id,
                "method": "kmeans",
                "k": 2,
                "seed": 0,
            },
            "idempotency_key": "index-cluster@stage3",
        },
    )

    for receipt in (project_receipt, cluster_receipt):
        assert receipt.outputs[0].ref["kind"] == "materialized_sheet"
        assert receipt.outputs[0].ref["reads"][0]["index_id"] == index_id
        assert any(item.ref["kind"] == "materialized_rows" for item in receipt.outputs)
        lineage = next(
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "lineage_parent_rows"
        )
        assert [pair["parent_row_id"] for pair in lineage["pairs"]] == source_row_ids
        materialized_rows = next(
            item.ref
            for item in receipt.outputs
            if item.ref["kind"] == "materialized_rows"
        )
        assert materialized_rows["parent_row_ids"] == source_row_ids

    backend = VectorBackend(project)
    backend.ensure_schema()
    vector = backend.db.execute(
        "SELECT vector_id FROM embedding_items WHERE index_id=? AND status='ready' "
        "LIMIT 1",
        (index_id,),
    ).fetchone()["vector_id"]
    backend.db.execute(
        "UPDATE embedding_vectors_f32 SET vec=? WHERE id=?",
        (struct.pack("<3f", 1.0, 2.0, 3.0), vector),
    )
    backend.db.commit()
    backend.close()
    stale = run_action_spec(
        project,
        {
            "action_id": "embedding.index_project",
            "scope": {"kind": "project"},
            "sheet_name": "bad-pca",
            "params": {
                "index_id": index_id,
                "method": "pca",
                "dimensions": 2,
            },
            "idempotency_key": "index-project-stale@stage3",
        },
        project_id="p",
    )
    assert stale.status == "failed"
    assert stale.errors[0].code == "embedding_space_mismatch"

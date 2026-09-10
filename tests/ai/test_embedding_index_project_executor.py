"""embedding.index_project — PCA-2D analysis sheet.

Reads an index's ready vectors, blocks on stale/incomplete/mixed-space,
computes a deterministic numpy-only PCA-2D, and routes one row per ready
vector into a NEW child sheet with embedding space/model/source_policy
provenance + an idempotent receipt.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.ai.embeddings import build_batch_result
from frisket.ai.embeddings.vector_backend import VectorBackend
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from helpers import replace_test_source_cell

_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class MappedGateway:
    def __init__(self, dim=384):
        self.dim = dim

    def embed(self, texts, *, provider, model, modality):
        out = [
            list(_VECTORS.get(t, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3)
            for t in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], {"headline": col})
    create = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "c@seed",
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
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@seed",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=MappedGateway()),
    )
    assert refresh.status == "completed", refresh.errors
    return {"sheet_id": sheet, "column_id": col, "index_id": index_id}


def _project_action(
    index_id: str,
    name: str,
    *,
    key: str = "pj",
    method: str = "pca",
    dimensions: int = 2,
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_project",
        "scope": {"kind": "project"},
        "sheet_name": name,
        "params": {
            "index_id": index_id,
            "method": method,
            "dimensions": dimensions,
        },
        "idempotency_key": f"pj@{key}",
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _project_action(seeded["index_id"], "cat-pca")


def _make_stale_source(project: Project, seeded: dict[str, Any]) -> None:
    # Edit a source cell -> a ready row is now stale.
    cat = next(
        rid
        for rid, v in project.get_values(
            seeded["sheet_id"], seeded["column_id"]
        ).items()
        if v == "cat"
    )
    replace_test_source_cell(
        project,
        row_id=cat,
        column_id=seeded["column_id"],
        value="panther",
    )


def _corrupt_vector_width(project: Project, seeded: dict[str, Any]) -> None:
    # Corrupt one ready vector to the wrong width (a different/mixed space).
    backend = VectorBackend(project)
    backend.ensure_schema()
    vid = backend.db.execute(
        "SELECT vector_id FROM embedding_items WHERE index_id=? AND status='ready' "
        "LIMIT 1",
        (seeded["index_id"],),
    ).fetchone()["vector_id"]
    backend.db.execute(
        "UPDATE embedding_vectors_f32 SET vec=? WHERE id=?",
        (struct.pack("<3f", 1.0, 2.0, 3.0), vid),
    )
    backend.db.commit()
    backend.close()


def _stale_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _project_action(seeded["index_id"], "stale-pca", key="stale")


def _mixed_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _project_action(seeded["index_id"], "mixed-pca", key="mixed")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    child = next(s for s in project.sheets() if s["name"] == "cat-pca")
    assert child["parent_sheet_id"] == seeded["sheet_id"]  # lineage to the source
    cols = {c["name"]: c for c in project.columns(child["id"])}
    assert set(cols) == {"source_key", "dim_0", "dim_1"}
    assert cols["dim_0"]["type"] == "number" and cols["dim_1"]["type"] == "number"
    assert project.row_count(child["id"]) == 3  # one per ready vector
    # the dim_0 column actually holds numeric coordinates
    dim0 = project.get_values(child["id"], cols["dim_0"]["id"])
    assert len(dim0) == 3 and all(isinstance(v, (int, float)) for v in dim0.values())

    # provenance on the output ref
    out = result.outputs[0].ref
    assert out["kind"] == "materialized_sheet"
    assert out["sheet_id"] == child["id"]
    assert out["row_count"] == 3
    assert out["columns"] == {name: col["id"] for name, col in cols.items()}
    (prov,) = out["reads"]
    assert prov["kind"] == "embedding_index_read"
    assert prov["index_id"] == seeded["index_id"]
    assert prov["space_id"] and prov["actual_model_id"]
    assert prov["source_policy_hash"] and prov["dimension"] == 384
    spec = json.loads(
        project.db.execute(
            "SELECT spec FROM ops WHERE id=?", (out["op_id"],)
        ).fetchone()["spec"]
    )
    assert spec["params"] == {
        "index_id": seeded["index_id"],
        "method": "pca",
        "dimensions": 2,
    }


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same key as the primary action, different target sheet name.
    return _project_action(seeded["index_id"], "cat-pca-OTHER")


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _project_action(seeded["index_id"], "cat-pca", key="duplicate-name")


CASES = [
    ExecutorCase(
        kind="embedding.index_project",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_sidecar_vectors",
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "embedding_index_not_found",
                    "embedding_source_stale",
                    "embedding_space_mismatch",
                    "embedding_analysis_insufficient_rows",
                    "duplicate_sheet_name",
                    "idempotency_conflict",
                    "invalid_action_request",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("index_id", "method", "dimensions"),
            output_schema_properties=("source_key", "dim_0", "dim_1"),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "embedding_source_stale",
                _stale_action,
                "embedding_source_stale",
                prepare=_make_stale_source,
            ),
            Gate(
                "embedding_space_mismatch",
                _mixed_action,
                "embedding_space_mismatch",
                prepare=_corrupt_vector_width,
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_params_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sheets": 1, "columns": 3, "rows": 3, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_index_project_replay_rejects_hidden_materialized_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors

        child = next(s for s in env.project.sheets() if s["name"] == "cat-pca")
        row_id = env.project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (child["id"],)
        ).fetchone()["id"]
        env.project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
        env.project.db.commit()

        stale = env.run_primary()
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"

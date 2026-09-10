"""embedding.index_cluster — deterministic k-means cluster sheet.

Sibling of embedding.index_project. Reads an index's ready vectors, blocks on
stale/incomplete/mixed-space identically, computes a DETERMINISTIC numpy-only
k-means (k-means++ init seeded by params.seed), and routes one row per ready
vector into a NEW child sheet [source_key, cluster_id] with embedding
space/model/source_policy provenance + an idempotent receipt. Same
vectors+k+seed -> identical assignments.
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

# Two clearly separated clusters in the first three dims so k=2 is unambiguous.
_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.9, 0.1, 0.0],
    "lion": [0.95, 0.05, 0.0],
    "airplane": [0.0, 0.0, 1.0],
    "rocket": [0.0, 0.1, 0.9],
    "jet": [0.05, 0.0, 0.95],
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


def _cluster_action(
    index_id: str,
    name: str,
    *,
    key: str = "cl",
    method: str = "kmeans",
    k: int = 2,
    seed: int = 0,
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_cluster",
        "scope": {"kind": "project"},
        "sheet_name": name,
        "params": {
            "index_id": index_id,
            "method": method,
            "k": k,
            "seed": seed,
        },
        "idempotency_key": f"cl@{key}",
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(seeded["index_id"], "headlines-clusters")


def _sheet_by_name(project: Project, name: str) -> Any:
    return next((s for s in project.sheets() if s["name"] == name), None)


def _assignment(project: Project, name: str) -> dict[str, Any]:
    """Return {source_key: cluster_id} for a cluster child sheet."""
    child = _sheet_by_name(project, name)
    assert child is not None
    cols = {c["name"]: c for c in project.columns(child["id"])}
    keys = project.get_values(child["id"], cols["source_key"]["id"])
    cids = project.get_values(child["id"], cols["cluster_id"]["id"])
    return {keys[rid]: cids[rid] for rid in keys}


def _make_stale_source(project: Project, seeded: dict[str, Any]) -> None:
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
    return _cluster_action(seeded["index_id"], "stale-cl", key="stale")


def _mixed_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(seeded["index_id"], "mixed-cl", key="mixed")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    child = _sheet_by_name(project, "headlines-clusters")
    assert child is not None
    assert child["parent_sheet_id"] == seeded["sheet_id"]  # lineage to the source
    cols = {c["name"]: c for c in project.columns(child["id"])}
    assert set(cols) == {"source_key", "cluster_id"}
    assert cols["cluster_id"]["type"] == "integer"
    assert project.row_count(child["id"]) == len(_VECTORS)  # one per ready vector
    cids = project.get_values(child["id"], cols["cluster_id"]["id"])
    assert all(type(v) is int for v in cids.values())
    assert set(cids.values()) == {0, 1}  # exactly k=2 labels used

    out = result.outputs[0].ref
    assert out["kind"] == "materialized_sheet"
    assert out["sheet_id"] == child["id"]
    assert out["row_count"] == len(_VECTORS)
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
        "method": "kmeans",
        "k": 2,
        "seed": 0,
    }


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same key as the primary action, different k -> different params hash.
    return _cluster_action(seeded["index_id"], "headlines-clusters-OTHER", k=3)


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(
        seeded["index_id"], "headlines-clusters", key="duplicate-name"
    )


CASES = [
    ExecutorCase(
        kind="embedding.index_cluster",
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
            input_schema_properties=("index_id", "method", "k", "seed"),
            output_schema_properties=("source_key", "cluster_id"),
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
        expect_counts={"sheets": 1, "columns": 2, "rows": 6, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_index_cluster_replay_rejects_materialized_lineage_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing one materialized row at a different parent makes replay's
    lineage check refuse to certify the stored result."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors

        body = json.loads(
            env.project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
            ).fetchone()["body"]
        )
        rows_ref = next(
            item["ref"]
            for section in ("inputs", "outputs", "evidence")
            for item in body.get(section) or []
            if isinstance(item.get("ref"), dict)
            and item["ref"].get("kind") == "materialized_rows"
        )
        row_id = rows_ref["row_ids"][0]
        replacement_parent_id = rows_ref["parent_row_ids"][-1]
        assert replacement_parent_id != rows_ref["parent_row_ids"][0]
        env.project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?",
            (replacement_parent_id, row_id),
        )
        env.project.db.commit()

        stale = env.run_primary()
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"


def test_index_cluster_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        a = env.run(
            _cluster_action(env.seeded["index_id"], "det-a", key="ka", k=2, seed=0)
        )
        assert a.status == "completed", a.errors
        b = env.run(
            _cluster_action(env.seeded["index_id"], "det-b", key="kb", k=2, seed=0)
        )
        assert b.status == "completed", b.errors

        # Same vectors + k + seed -> identical per-source_key labels (order
        # fixed by ORDER BY source_key, init seeded). No label permutation.
        asn_a = _assignment(env.project, "det-a")
        asn_b = _assignment(env.project, "det-b")
        assert asn_a == asn_b
        # And the partition actually separates the two semantic groups.
        # source_key is the source row key; the three animal rows were added
        # first (keys 1..3) and the three craft rows next (keys 4..6).
        ordered = sorted(asn_a, key=lambda s: int(s))
        animals = {asn_a[k] for k in ordered[:3]}
        craft = {asn_a[k] for k in ordered[3:]}
        assert len(animals) == 1 and len(craft) == 1 and animals != craft


def test_index_cluster_k_exceeds_rows_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        before = len(env.project.sheets())
        res = env.run(
            _cluster_action(
                env.seeded["index_id"], "kbig-cl", key="kbig", k=len(_VECTORS) + 1
            )
        )
        assert res.status == "failed"
        assert res.errors[0].code == "embedding_analysis_insufficient_rows"
        assert res.errors[0].details["minimum_rows"] == len(_VECTORS) + 1
        assert res.errors[0].details["ready"] == len(_VECTORS)
        assert len(env.project.sheets()) == before
        assert _sheet_by_name(env.project, "kbig-cl") is None

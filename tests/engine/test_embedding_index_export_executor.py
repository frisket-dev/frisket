"""embedding.index_export — JSONL / Parquet / Arrow artifacts + receipt.

Exports an index's ready source originals + raw vectors. Raw vectors never go
to CSV or the default bundle. The harness owns the shared lifecycle; the
hand-written tests cover format content, vector omission, artifact-missing
replay, stale-source blocking, and the sidecar/ready-vector preconditions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow.feather as feather
import pyarrow.parquet as pq
import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.ai.embeddings import build_batch_result
from frisket.ai.embeddings.freshness import IndexFreshness
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor import embedding_read
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


def _index_create_action(sheet: int, *, key: str) -> dict[str, Any]:
    return {
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
        "idempotency_key": key,
    }


def _refresh(project: Project, index_id: str, *, key: str = "r@seed") -> None:
    refresh = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": key,
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=MappedGateway()),
    )
    assert refresh.status == "completed", refresh.errors


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    sheet = project.add_sheet("animals")
    col = project.add_column(sheet, "headline")
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], {"headline": col})
    create = run_action_spec(
        project, _index_create_action(sheet, key="c@seed"), project_id="p"
    )
    assert create.status == "completed", create.errors
    index_id = create.outputs[0].ref["index_id"]
    _refresh(project, index_id)
    out = tmp_path / "out"
    out.mkdir()
    return {
        "sheet_id": sheet,
        "column_id": col,
        "index_id": index_id,
        "out": out,
    }


def _export_action(
    index_id: str,
    dest: Path,
    *,
    formats: list[str] | None = None,
    include_vectors: bool = True,
    key: str = "e",
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_export",
        "scope": {"kind": "project"},
        "params": {
            "index_id": index_id,
            "destination": {"kind": "local_dir", "path": str(dest)},
            "formats": formats or ["jsonl"],
            "include_vectors": include_vectors,
        },
        "idempotency_key": f"e@{key}",
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(seeded["index_id"], seeded["out"])


def _missing_index_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action("embidx_missing", seeded["out"], key="missing-index")


def _bad_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        seeded["index_id"], seeded["out"] / "does-not-exist", key="bad-dest"
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    jsonl = next(o.ref for o in result.outputs if o.ref["format"] == "jsonl")
    assert Path(jsonl["path"]).is_file()
    assert jsonl["row_count"] == 3
    assert jsonl["sha256"].startswith("sha256:")

    # JSONL content: originals + vectors
    lines = Path(jsonl["path"]).read_text().splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert set(rec) >= {"source_key", "row_id", "source_hash", "values", "vector"}
    assert rec["values"]["headline"] in _VECTORS
    assert len(rec["vector"]) == 384

    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    prov = next(
        e["ref"]
        for e in receipt["evidence"]
        if e["ref"]["kind"] == "embedding_index_export_provenance"
    )
    assert prov["dimension"] == 384
    assert prov["provider_id"] == "fastembed"
    assert prov["distance_metric"] == "cosine"
    assert prov["source_policy_hash"].startswith("sha256:")
    assert prov["vector_backend_id"] and prov["vector_backend_version"]
    assert prov["ready_items"] == 3


CASES = [
    ExecutorCase(
        kind="embedding.index_export",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_sidecar_vectors",
                    "write_export_artifact",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "embedding_index_not_found",
                    "embedding_export_sidecar_missing",
                    "embedding_export_no_ready_vectors",
                    "embedding_export_artifact_missing",
                    "embedding_source_stale",
                    "invalid_export_destination",
                    "idempotency_conflict",
                    "invalid_action_request",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("index_id", "destination", "formats"),
            output_schema_properties=("artifacts", "exported_rows"),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "embedding_index_not_found",
                _missing_index_action,
                "embedding_index_not_found",
            ),
            Gate(
                "invalid_export_destination",
                _bad_destination_action,
                "invalid_export_destination",
            ),
        ),
        expect_counts={"receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_export_writes_parquet_and_arrow_with_vector_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run(
            _export_action(
                env.seeded["index_id"],
                env.seeded["out"],
                formats=["jsonl", "parquet", "arrow"],
                key="multi",
            )
        )
        assert result.status == "completed", result.errors
        by_fmt = {o.ref["format"]: o.ref for o in result.outputs}
        assert set(by_fmt) == {"jsonl", "parquet", "arrow"}
        for ref in by_fmt.values():
            assert Path(ref["path"]).is_file()
            assert ref["row_count"] == 3
            assert ref["sha256"].startswith("sha256:")

        pt = pq.read_table(by_fmt["parquet"]["path"])
        assert pt.num_rows == 3 and "vector" in pt.column_names
        at = feather.read_table(by_fmt["arrow"]["path"])
        assert at.num_rows == 3 and "vector" in at.column_names


def test_export_without_vectors_omits_vector_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run(
            _export_action(
                env.seeded["index_id"],
                env.seeded["out"],
                formats=["jsonl", "parquet"],
                include_vectors=False,
                key="novec",
            )
        )
        assert result.status == "completed", result.errors
        by_fmt = {o.ref["format"]: o.ref for o in result.outputs}
        rec = json.loads(Path(by_fmt["jsonl"]["path"]).read_text().splitlines()[0])
        assert rec["vector"] is None
        assert "vector" not in pq.read_table(by_fmt["parquet"]["path"]).column_names


def test_export_replay_detects_missing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        # delete the artifact -> replay fails typed instead of certifying it
        Path(first.outputs[0].ref["path"]).unlink()
        broken = env.run_primary()
        assert broken.status == "failed"
        assert broken.errors[0].code == "embedding_export_artifact_missing"


def test_export_missing_sidecar_fails(tmp_path: Path) -> None:
    # An index that was created but never refreshed has no sidecar at all.
    project = Project.create(tmp_path / "nosidecar.frisket", name="nosidecar")
    try:
        sheet = project.add_sheet("animals")
        col = project.add_column(sheet, "headline")
        project.add_rows(sheet, [{"headline": t} for t in _VECTORS], {"headline": col})
        create = run_action_spec(
            project, _index_create_action(sheet, key="c@nosidecar"), project_id="p"
        )
        assert create.status == "completed", create.errors
        out = tmp_path / "out"
        out.mkdir()
        result = run_action_spec(
            project,
            _export_action(create.outputs[0].ref["index_id"], out),
            project_id="p",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "embedding_export_sidecar_missing"
    finally:
        project.close()


def test_export_no_ready_vectors_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        # The sidecar exists (seed refreshed index a); a second, never-refreshed
        # index has zero ready vectors.
        create = env.run(_index_create_action(env.seeded["sheet_id"], key="c@b"))
        assert create.status == "completed", create.errors
        result = env.run(
            _export_action(
                create.outputs[0].ref["index_id"], env.seeded["out"], key="b"
            )
        )
        assert result.status == "failed"
        assert result.errors[0].code == "embedding_export_no_ready_vectors"


def test_export_blocks_stale_source_without_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a ready vector whose source content changed must NOT be exported paired
    # with the current values — fail loud, before any artifact or receipt.
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project = env.project
        cat_row = next(
            rid
            for rid, v in project.get_values(
                env.seeded["sheet_id"], env.seeded["column_id"]
            ).items()
            if v == "cat"
        )
        replace_test_source_cell(
            project,
            row_id=cat_row,
            column_id=env.seeded["column_id"],
            value="cat rewritten",
        )
        result = env.run(
            _export_action(
                env.seeded["index_id"],
                env.seeded["out"],
                formats=["jsonl", "parquet"],
                key="stale",
            )
        )
        assert result.status == "failed"
        assert result.errors[0].code == "embedding_source_stale"
        assert str(cat_row) in result.errors[0].details["stale_source_keys"]
        # nothing committed: no artifacts (not even tmp) and no export receipt
        assert list(env.seeded["out"].iterdir()) == []
        assert result.receipt_id is None
        n = project.db.execute(
            "SELECT COUNT(*) AS n FROM receipts "
            "WHERE action_kind='embedding.index_export'"
        ).fetchone()["n"]
        assert n == 0


def test_embedding_read_fails_closed_for_unknown_nonfresh_reason(monkeypatch) -> None:
    freshness = IndexFreshness(ready_keys=set())
    monkeypatch.setattr(embedding_read, "index_freshness", lambda *_args: freshness)
    monkeypatch.setattr(
        embedding_read, "freshness_reason", lambda *_args: "future_nonfresh_reason"
    )

    error = embedding_read.index_read_freshness_error(object(), object(), "cluster")

    assert error is not None
    assert error.code == "embedding_index_incomplete"
    assert error.details["freshness_reason"] == "future_nonfresh_reason"

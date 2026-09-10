from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


class _RemoteGateway:
    def embed(
        self,
        texts: list[str],
        *,
        provider: str,
        model: str,
        modality: str,
    ) -> dict[str, Any]:
        return build_batch_result(
            [[1.0] + [0.0] * 1535 for _ in texts],
            provider_id=provider,
            provider_kind="platform_api",
            requested_model=model,
            actual_model_id=model,
            modality=modality,
            usage={"input_tokens": 19, "input_count": len(texts)},
            credential_source="org_byok",
            provider_cost_usd=0.0007,
            provider_reported_cost_usd=0.0007,
            cost_source="provider_reported",
        )


def _embedding_project(tmp_path: Path) -> tuple[Project, int, str]:
    project = Project.create(tmp_path / "lifecycle.frisket", name="lifecycle")
    sheet_id = project.add_sheet("Stories")
    column_id = project.add_column(sheet_id, "story", type="text")
    project.add_rows(sheet_id, [{"story": "one"}], {"story": column_id})
    create = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet_id,
                "source_columns": ["story"],
                "modality": "text",
                "provider": "openai",
                "model": "text-embedding-3-small",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": True},
            },
            "idempotency_key": "fact-lifecycle-create@1",
        },
        project_id="fact-lifecycle",
    )
    assert create.status == "completed"
    return project, sheet_id, str(create.outputs[0].ref["index_id"])


def _refresh(index_id: str, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": "full"},
        "idempotency_key": key,
    }


def _assert_quarantined_fact(project: Project) -> None:
    run = project.db.execute(
        "SELECT * FROM runs WHERE action_kind='embedding.index_refresh' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run is not None
    assert run["status"] == "failed"
    assert float(run["cost_actual"]) == pytest.approx(0.0007)
    [fact] = RunResultStore(project).model_calls(int(run["id"]))
    assert fact["capability"] == "llm.embed"
    assert fact["credential_source"] == "org_byok"


def test_embedding_provider_fact_survives_vector_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.engine.executor.action_families.embeddings as family

    project, _sheet_id, index_id = _embedding_project(tmp_path)
    try:

        def fail_upsert(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("fault after provider response")

        monkeypatch.setattr(family.VectorBackend, "upsert_item", fail_upsert)
        with pytest.raises(RuntimeError, match="fault after provider response"):
            run_action_spec(
                project,
                _refresh(index_id, key="fact-lifecycle-vector-fault@1"),
                project_id="fact-lifecycle",
                deps=ExecutorDeps(embedding_gateway=_RemoteGateway()),
            )
        _assert_quarantined_fact(project)
    finally:
        project.close()


def test_embedding_receipt_failure_never_leaves_completed_billable_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _sheet_id, index_id = _embedding_project(tmp_path)
    try:

        def fail_receipt(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("fault before receipt finalization")

        monkeypatch.setattr(ReceiptStore, "insert", fail_receipt)
        result = run_action_spec(
            project,
            _refresh(index_id, key="fact-lifecycle-receipt-fault@1"),
            project_id="fact-lifecycle",
            deps=ExecutorDeps(embedding_gateway=_RemoteGateway()),
        )
        assert result.status == "failed"
        _assert_quarantined_fact(project)
        assert result.receipt_id is None
    finally:
        project.close()

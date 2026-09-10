"""Closure fence for remote ``embedding.index_refresh``
egress honors the per-project ``network=off`` policy.

The kind carries no static ``external:*`` tag (a local-provider refresh is
genuinely offline), so the gate lives in the refresh executor at the same
pre-claim choke point as the ``allow_remote`` policy — which stays the primary
consent gate. ``network=off`` blocks a REMOTE-backed refresh before any
provider call, and never blocks a local-backed one.
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import VectorBackend, build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project

PROJECT_ID = "project-embedding-network-fence"


class FakeGateway:
    def __init__(self, dim: int = 1536, kind: str = "platform_api"):
        self.dim = dim
        self.kind = kind
        self.calls: list[tuple] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        vectors = [
            [float((abs(hash(t)) % 97) + 1)] + [0.0] * (self.dim - 1) for t in texts
        ]
        return build_batch_result(
            vectors,
            provider_id=provider or "fastembed",
            provider_kind=self.kind,
            requested_model=model,
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "emb-net.frisket", name="emb-net")
    sheet = p.add_sheet("data")
    cols = {"headline": p.add_column(sheet, "headline")}
    p.add_rows(sheet, [{"headline": f"story {i}"} for i in range(3)], cols)
    p._sheet_id = sheet  # type: ignore[attr-defined]
    yield p
    p.close()


def _create_action(sheet_id, *, provider, allow_remote, key):
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": provider,
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": allow_remote},
        },
        "idempotency_key": key,
    }


def _refresh_action(index_id, *, key):
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": "incremental"},
        "idempotency_key": key,
    }


def _run(project, data, *, gateway=None):
    deps = ExecutorDeps(embedding_gateway=gateway) if gateway is not None else None
    return run_action_spec(project, data, project_id=PROJECT_ID, deps=deps)


def test_network_off_blocks_remote_backed_refresh_before_egress(project) -> None:
    created = _run(
        project,
        _create_action(
            project._sheet_id, provider="openai", allow_remote=True, key="c@1"
        ),
    )
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]

    project.set_network_policy(mode="off")
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id, key="r@1"), gateway=gw)
    assert result.status == "failed"
    assert result.errors[0].code == "network_disabled"
    assert gw.calls == []  # blocked BEFORE any provider call
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_network_off_still_allows_local_backed_refresh(project) -> None:
    # The honest inverse: a local-provider refresh does no egress, so a
    # static kind-level external tag (which would block it) is exactly the
    # wrong fix — the executor gate blocks only remote-backed indexes.
    created = _run(
        project,
        _create_action(
            project._sheet_id, provider="fastembed", allow_remote=False, key="c@2"
        ),
    )
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]

    project.set_network_policy(mode="off")
    gw = FakeGateway(dim=384, kind="local_process")
    result = _run(project, _refresh_action(index_id, key="r@2"), gateway=gw)
    assert result.status == "completed", result.errors
    assert len(gw.calls) == 1


def test_network_on_keeps_remote_refresh_working(project) -> None:
    created = _run(
        project,
        _create_action(
            project._sheet_id, provider="openai", allow_remote=True, key="c@3"
        ),
    )
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id, key="r@3"), gateway=gw)
    assert result.status == "completed", result.errors
    assert len(gw.calls) == 1

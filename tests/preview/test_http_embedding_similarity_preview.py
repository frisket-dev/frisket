"""Read-only show-similar preview over ``embedding_similarity``.

POST /api/projects/{pid}/embeddings/v1/similarity-preview wraps
resolve_embedding_similarity and attaches each hit's current row values. No UI.
Covers: row anchor (with values + distance/score), manual-text remote gate
(blocked vs allowed), stale anchor, hidden rows, missing index, bad space, and
NO provider call for a row anchor (a router that raises on embed still returns
results for a row anchor).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.team.security.secrets import encrypt_secret, key_hint
from helpers import replace_test_source_cell

_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class MappedGateway:
    def __init__(self, dim: int = 384):
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


class _RaisingRouter:
    """A router that fails if any embedding provider call is made — used to prove
    a row anchor never hits a provider."""

    def embed_batch(self, *a, **k):
        raise AssertionError("provider was called for a row anchor")


def _openai_router():
    router = ModelRouter(keys={})

    class _Adapter:
        base_url = "https://example/v1"

        async def embed_with_meta(self, texts, model, client):
            return (
                [[1.0] + [0.0] * 1535 for _ in texts],
                {"actual_model_id": model, "dimension": 1536},
            )

    router._adapters["openai"] = _Adapter()
    return router


def _client(tmp_path, router):
    return TestClient(create_app(tmp_path / "ws", router=router))


def _seed(client, *, provider="fastembed", policy=None, dim=384):
    pid = client.post("/api/projects", json={"name": "sim"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], cols)
    create = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "provider_policy": policy or {"allow_remote": False},
            },
            "idempotency_key": "c@1",
        },
        project_id=pid,
    )
    index_id = create.outputs[0].ref["index_id"]
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@1",
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=MappedGateway(dim)),
    )
    return pid, project, sheet, cols, index_id


def _row(project, sheet, cols, text):
    return next(
        rid
        for rid, v in project.get_values(sheet, cols["headline"]).items()
        if v == text
    )


def _row_query(index_id, row_id, **extra):
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": {"kind": "row", "row_id": row_id},
        **extra,
    }


def _preview(client, pid, query):
    return client.post(
        f"/api/projects/{pid}/embeddings/v1/similarity-preview", json={"query": query}
    )


def _hybrid_preview(client, pid, query):
    return client.post(
        f"/api/projects/{pid}/embeddings/v1/hybrid-preview", json={"query": query}
    )


def _paid_preview_query(kind, index_id, sheet, text):
    if kind == "similarity":
        return _manual_query(index_id, text)
    return {
        "kind": "embedding_hybrid",
        "embedding_index_id": index_id,
        "sheet_id": sheet,
        "text": text,
    }


def _run_paid_preview(client, pid, kind, query):
    if kind == "similarity":
        return _preview(client, pid, query)
    return _hybrid_preview(client, pid, query)


def _install_paid_project_key_embed(monkeypatch, calls, *, dimension=1536):
    async def embed_batch(self, texts, *, model, modality):
        provider, actual_model = model.split("/", 1)
        credential_source = self.credential_source_for(provider)
        calls.append(
            {
                "texts": list(texts),
                "model": model,
                "modality": modality,
                "credential_source": credential_source,
            }
        )
        return build_batch_result(
            [[1.0] + [0.0] * (dimension - 1) for _ in texts],
            provider_id=provider,
            provider_kind="platform_api",
            requested_model=actual_model,
            actual_model_id=actual_model,
            modality=modality,
            dimension=dimension,
            usage={"input_count": len(texts), "input_tokens": 7},
            provider_request_id=f"preview-request-{len(calls)}",
            credential_source=credential_source,
            provider_reported_cost_usd=0.0042,
            provider_cost_usd=0.0042,
            cost_source="provider_reported",
        )

    monkeypatch.setattr(ModelRouter, "embed_batch", embed_batch)


# --------------------------------------------------------------------------


def test_row_anchor_preview_returns_ordered_hits_with_values(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    resp = _preview(client, pid, _row_query(index_id, cat))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "frisket.embedding_similarity_preview.v1"
    assert body["index_id"] == index_id and body["space_id"]
    assert body["distance_metric"] == "cosine"
    kitten = _row(project, sheet, cols, "kitten")
    airplane = _row(project, sheet, cols, "airplane")
    assert [h["row_id"] for h in body["hits"]] == [kitten, airplane]
    top = body["hits"][0]
    assert top["values"]["headline"] == "kitten"  # current row value attached
    assert top["distance"] < body["hits"][1]["distance"]
    assert top["score"] > body["hits"][1]["score"]


def test_no_provider_call_for_row_anchor(tmp_path):
    # the router raises if embed is called; a row anchor must still succeed
    client = _client(tmp_path, _RaisingRouter())
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    resp = _preview(client, pid, _row_query(index_id, cat))
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["hits"]) == 2


def test_hidden_rows_excluded(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    kitten = _row(project, sheet, cols, "kitten")
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (kitten,))
    project.db.commit()
    body = _preview(client, pid, _row_query(index_id, cat)).json()
    assert [h["row_id"] for h in body["hits"]] == [
        _row(project, sheet, cols, "airplane")
    ]


def test_stale_anchor_returns_typed_400(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    replace_test_source_cell(
        project,
        row_id=cat,
        column_id=cols["headline"],
        value="cat rewritten",
    )
    resp = _preview(client, pid, _row_query(index_id, cat))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_source_stale"


def test_missing_index_returns_typed_400(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, *_ = _seed(client)
    resp = _preview(client, pid, _row_query("embidx_missing", 1))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_index_not_found"


def test_bad_space_returns_typed_400(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    resp = _preview(client, pid, _row_query(index_id, cat, space_id="emb_other"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_space_mismatch"


def test_missing_project_returns_404(tmp_path):
    client = _client(tmp_path, ModelRouter(keys={}))
    resp = _preview(client, "nope", _row_query("embidx_x", 1))
    assert resp.status_code == 404


def _manual_query(index_id, text):
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": {"kind": "manual_text_query", "text": text},
    }


def test_manual_text_blocks_remote_egress_without_policy(tmp_path):
    from frisket.ai.embeddings import EmbeddingStore

    client = _client(tmp_path, _openai_router())
    # Refresh under allow_remote=True so the index is FRESH, then revoke remote — so
    # the MANUAL-TEXT egress gate is exercised, not the (separate) freshness gate. A
    # never-refreshed remote index is correctly blocked earlier as incomplete.
    pid, project, sheet, cols, index_id = _seed(
        client, provider="openai", policy={"allow_remote": True}, dim=1536
    )
    EmbeddingStore(project).update_index_policy(
        index_id, provider_policy={"allow_remote": False}
    )
    resp = _preview(client, pid, _manual_query(index_id, "cat"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_remote_confirmation_required"


def test_manual_text_runs_when_policy_allows(tmp_path):
    client = _client(tmp_path, _openai_router())
    pid, project, sheet, cols, index_id = _seed(
        client, provider="openai", policy={"allow_remote": True}, dim=1536
    )
    resp = _preview(client, pid, _manual_query(index_id, "cat"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # the router's embed produced a [1,0,...] query → cat ranks first
    assert body["hits"][0]["row_id"] == _row(project, sheet, cols, "cat")


@pytest.mark.parametrize("kind", ["similarity", "hybrid"])
def test_remote_manual_preview_refuses_exhausted_project_key_before_egress(
    tmp_path, monkeypatch, kind
):
    """allow_remote consents to egress, but it must not bypass the independent
    project-key spend bound on either direct preview route."""
    client = _client(tmp_path, _openai_router())
    pid, project, sheet, _cols, index_id = _seed(
        client, provider="openai", policy={"allow_remote": True}, dim=1536
    )
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=0,
    )
    calls = []
    _install_paid_project_key_embed(monkeypatch, calls)

    response = _run_paid_preview(
        client,
        pid,
        kind,
        _paid_preview_query(kind, index_id, sheet, "cat"),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "provider_spend_cap_exceeded"
    assert detail["provider"] == "openai"
    assert detail["cap_usd"] == 0
    assert detail["spent_usd"] == 0
    assert detail["setting"] == "spend_cap_usd"
    assert calls == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id IS NULL AND provider='openai'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("kind", ["similarity", "hybrid"])
def test_remote_manual_preview_persists_paid_fact_and_project_key_spend(
    tmp_path, monkeypatch, kind
):
    """A successful preview cannot return ranked rows while its paid embedding
    call disappears from the neutral fact ledger and project-key spend."""
    client = _client(tmp_path, _openai_router())
    pid, project, sheet, _cols, index_id = _seed(
        client, provider="openai", policy={"allow_remote": True}, dim=1536
    )
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )
    calls = []
    _install_paid_project_key_embed(monkeypatch, calls)

    response = _run_paid_preview(
        client,
        pid,
        kind,
        _paid_preview_query(kind, index_id, sheet, "cat"),
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0]["credential_source"] == "project_key"
    [fact] = project.db.execute(
        "SELECT * FROM model_calls WHERE run_id IS NULL AND provider='openai'"
    ).fetchall()
    assert fact["capability"] == "llm.embed"
    assert fact["engine"] == "openai/text-embedding-3-small"
    assert fact["credential_source"] == "project_key"
    assert fact["provider_cost_usd"] == pytest.approx(0.0042)
    assert json.loads(fact["units"]) == {"input_count": 1, "input_tokens": 7}
    assert fact["request_id"] == "preview-request-1"
    spend = project.provider_spend_state("openai")
    assert spend is not None
    assert spend.spent_micro == 4_200
    assert spend.unmetered_calls == 0


def test_remote_manual_preview_accounts_for_paid_malformed_provider_result(
    tmp_path, monkeypatch
):
    """A provider contract error happens after egress.  The 400 must not erase
    the real call or make the project key look unspent."""
    client = _client(tmp_path, _openai_router())
    pid, project, sheet, _cols, index_id = _seed(
        client, provider="openai", policy={"allow_remote": True}, dim=1536
    )
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )
    calls = []
    _install_paid_project_key_embed(monkeypatch, calls, dimension=8)

    response = _preview(client, pid, _manual_query(index_id, "cat"))

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "embedding_space_mismatch"
    assert len(calls) == 1
    [fact] = project.db.execute(
        "SELECT * FROM model_calls WHERE run_id IS NULL AND provider='openai'"
    ).fetchall()
    assert fact["provider_cost_usd"] == pytest.approx(0.0042)
    spend = project.provider_spend_state("openai")
    assert spend is not None
    assert spend.spent_micro == 4_200


def test_preview_blocks_incomplete_after_append(tmp_path):
    # Show Similar must not silently search a PARTIAL index (coordinator review).
    client = _client(tmp_path, ModelRouter(keys={}))
    pid, project, sheet, cols, index_id = _seed(client)
    cat = _row(project, sheet, cols, "cat")
    project.add_rows(sheet, [{"headline": "lion"}], cols)  # unembedded
    resp = _preview(client, pid, _row_query(index_id, cat))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_index_incomplete"

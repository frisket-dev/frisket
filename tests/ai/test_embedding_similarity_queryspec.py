"""embedding_similarity retrieval backend (handoff Lane 4 "find similar").

Backend only — no UI, watchlists, clustering, scheduled refresh, or export.
Covers: row-anchor nearest-in-order; results constrained to the index sheet +
visibility; missing/stale anchor typed failures; cross-space mismatch can't run;
manual text query blocks remote egress unless policy allows it; and NO provider
call for a row anchor whose vector already exists.
"""

from __future__ import annotations

import json

import pytest

from frisket.ai.embeddings import (
    VectorBackend,
    build_batch_result,
    resolve_embedding_similarity,
)
from frisket.ai.embeddings.similarity import SimilarityError
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from helpers import replace_test_source_cell


class MappedGateway:
    """Deterministic text->vector embedder so ranking is predictable. Records
    calls so a row-anchor query can be proven not to embed anything."""

    def __init__(self, vectors_by_text: dict[str, list[float]], dim: int = 384):
        self.vectors_by_text = vectors_by_text
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            base = list(self.vectors_by_text.get(t, [0.0, 0.0, 0.0]))
            out.append(base + [0.0] * (self.dim - len(base)))
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


# cat and kitten are close; airplane is orthogonal to both.
_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


def _create_index(project, sheet, *, provider="fastembed", policy=None, key="c"):
    action = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": provider,
            "source_policy": {"kind": "text_cell"},
            "maintenance_policy": {"mode": "manual"},
            "provider_policy": policy
            if policy is not None
            else {"allow_remote": False},
        },
        "idempotency_key": f"sim_create@{key}",
    }
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _refresh(project, index_id, gateway, *, key="r"):
    action = {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": "full"},
        "idempotency_key": f"sim_refresh@{key}",
    }
    result = run_action_spec(
        project,
        action,
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    assert result.status == "completed", result.errors


@pytest.fixture
def env(tmp_path):
    project = Project.create(tmp_path / "sim.frisket", name="sim")
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(
        sheet,
        [{"headline": t} for t in ("cat", "kitten", "airplane")],
        cols,
    )
    index_id = _create_index(project, sheet)
    _refresh(project, index_id, MappedGateway(_VECTORS))
    yield project, sheet, cols, index_id
    project.close()


def _row(project, sheet, cols, text):
    values = project.get_values(sheet, cols["headline"])
    return next(rid for rid, v in values.items() if v == text)


def _row_query(index_id, row_id, **extra):
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": {"kind": "row", "row_id": row_id},
        **extra,
    }


# --------------------------------------------------------------------------


def test_row_anchor_returns_nearest_in_order(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    result = resolve_embedding_similarity(project, _row_query(index_id, cat))
    kitten = _row(project, sheet, cols, "kitten")
    airplane = _row(project, sheet, cols, "airplane")
    # nearest first; the anchor itself is excluded
    assert [h.row_id for h in result.hits] == [kitten, airplane]
    # strictly ascending distance, descending cosine score
    assert result.hits[0].distance < result.hits[1].distance
    assert result.hits[0].score > result.hits[1].score
    assert result.hits[0].distance == pytest.approx(0.2, abs=1e-6)
    assert result.hits[0].score == pytest.approx(0.8, abs=1e-6)
    assert all(h.sheet_id == sheet for h in result.hits)
    assert result.space_id and result.distance_metric == "cosine"


def test_no_provider_call_for_row_anchor(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    gw = MappedGateway(_VECTORS)
    resolve_embedding_similarity(project, _row_query(index_id, cat), gateway=gw)
    assert gw.calls == []  # the stored anchor vector is reused — nothing embedded


def test_results_constrained_to_visible_index_rows(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    kitten = _row(project, sheet, cols, "kitten")
    airplane = _row(project, sheet, cols, "airplane")
    # hide kitten → it drops out even though its vector is nearest
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (kitten,))
    project.db.commit()
    result = resolve_embedding_similarity(project, _row_query(index_id, cat))
    assert [h.row_id for h in result.hits] == [airplane]


def test_limit_and_threshold(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    limited = resolve_embedding_similarity(project, _row_query(index_id, cat, limit=1))
    assert len(limited.hits) == 1  # only the nearest
    # threshold drops the far airplane (distance 1.0)
    thresholded = resolve_embedding_similarity(
        project, _row_query(index_id, cat, threshold=0.5)
    )
    assert [h.row_id for h in thresholded.hits] == [
        _row(project, sheet, cols, "kitten")
    ]


def test_requested_limit_above_legacy_cap_returns_every_match(tmp_path):
    project = Project.create(tmp_path / "large-sim.frisket", name="large-sim")
    sheet = project.add_sheet("documents")
    cols = {"headline": project.add_column(sheet, "headline")}
    texts = [f"document {index:03d}" for index in range(502)]
    project.add_rows(sheet, [{"headline": text} for text in texts], cols)
    vectors = {text: [1.0, 0.0, 0.0] for text in texts}
    index_id = _create_index(project, sheet, key="large-sim")
    _refresh(project, index_id, MappedGateway(vectors), key="large-sim")

    anchor = _row(project, sheet, cols, texts[0])
    result = resolve_embedding_similarity(
        project, _row_query(index_id, anchor, limit=501)
    )

    assert result.limit == 501
    assert len(result.hits) == 501
    project.close()


def test_missing_anchor_is_typed_failure(env):
    project, sheet, cols, index_id = env
    # a row that exists but was never embedded (added after refresh)
    new_row = project.add_rows(sheet, [{"headline": "dog"}], cols)[0]
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, _row_query(index_id, new_row))
    assert exc.value.code == "embedding_anchor_not_found"


def test_stale_anchor_is_typed_failure(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id=index_id,
        space_id="ignored",
        source_key=str(cat),
        source_ref={},
        source_hash="h",
        status="stale",
        vector=[1.0, 0.0, 0.0],
    )
    backend.close()
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, _row_query(index_id, cat))
    assert exc.value.code == "embedding_source_stale"


def test_unknown_index_is_typed_failure(env):
    project, *_ = env
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, _row_query("embidx_missing", 1))
    assert exc.value.code == "embedding_index_not_found"


def test_cross_space_mismatch_cannot_run(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    query = _row_query(index_id, cat, space_id="emb_some_other_space")
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, query)
    assert exc.value.code == "embedding_space_mismatch"


# --------------------------------------------------------------------------
# manual text query + remote egress policy
# --------------------------------------------------------------------------


def _manual_query(index_id, text):
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": {"kind": "manual_text_query", "text": text},
    }


def _preview_action(query, *, key):
    return {
        "action_id": "query.preview",
        "scope": {"kind": "project"},
        "params": {"query": query, "limit": 50, "offset": 0},
        "idempotency_key": key,
    }


@pytest.mark.parametrize("anchor_kind", ["row", "row_cell"])
def test_typed_preview_preserves_stored_vector_similarity(
    env, anchor_kind, monkeypatch
):
    from frisket.engine.store.receipts import ReceiptStore

    project, sheet, cols, index_id = env
    monkeypatch.setattr(
        "frisket.ai.embeddings.similarity.EmbeddingGateway",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored-vector preview must not construct a gateway")
        ),
    )
    anchor = _row(project, sheet, cols, "cat")
    query = _row_query(index_id, anchor)
    query["sheet_id"] = sheet
    query["anchor"]["kind"] = anchor_kind
    if anchor_kind == "row_cell":
        query["anchor"]["column_id"] = cols["headline"]

    result = run_action_spec(
        project,
        _preview_action(query, key=f"typed-stored-{anchor_kind}"),
        project_id="p",
    )

    assert result.status == "completed", result.errors
    assert result.value["query"]["anchor"]["kind"] == anchor_kind
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt is not None
    assert receipt.provider_use[0]["service"] == ("frisket.query_preview.stored_vector")
    assert receipt.provider_use[0]["external_api"] is False


def test_typed_preview_local_manual_replays_without_reembedding(env, monkeypatch):
    from frisket.engine.store.receipts import ReceiptStore

    project, sheet, _cols, index_id = env
    calls = []

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            calls.append(list(texts))
            return [list(_VECTORS[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    query = _manual_query(index_id, "cat")
    query["sheet_id"] = sheet
    action = _preview_action(query, key="typed-manual")
    first = run_action_spec(project, action, project_id="p")
    # Make the live index incomplete. Idempotency replay must happen before a
    # fresh resolution (and therefore before another local embed).
    project.add_rows(sheet, [{"headline": "new unembedded row"}], _cols)
    replay = run_action_spec(project, action, project_id="p")
    fresh = run_action_spec(
        project,
        _preview_action(query, key="typed-manual-after-stale"),
        project_id="p",
    )

    assert first.status == replay.status == "completed"
    assert replay.receipt_id == first.receipt_id
    assert calls == [["cat"]]
    assert fresh.status == "failed"
    assert fresh.errors[0].code == "embedding_index_incomplete"
    receipt = ReceiptStore(project).parsed_by_id(first.receipt_id)
    assert receipt is not None
    assert receipt.provider_use[0]["service"] == (
        "frisket.query_preview.local_embedding"
    )
    assert receipt.provider_use[0]["provider"] == "fastembed"


def test_typed_preview_receipt_uses_actual_gateway_identity(env, monkeypatch):
    from frisket.engine.store.receipts import ReceiptStore

    project, sheet, _cols, index_id = env

    class ActualGateway:
        def embed(self, texts, *, provider, model, modality):
            del provider, model
            vectors = [list(_VECTORS[text]) + [0.0] * 381 for text in texts]
            return build_batch_result(
                vectors,
                provider_id="actual-local-provider",
                provider_kind="local_process",
                actual_model_id="actual-local-model",
                modality=modality,
            )

    monkeypatch.setattr(
        "frisket.ai.embeddings.similarity.EmbeddingGateway", ActualGateway
    )
    query = {**_manual_query(index_id, "cat"), "sheet_id": sheet}
    result = run_action_spec(
        project,
        _preview_action(query, key="typed-actual-provider"),
        project_id="p",
    )

    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt is not None
    assert receipt.provider_use[0]["provider"] == "actual-local-provider"
    assert receipt.provider_use[0]["model"] == "actual-local-model"


def test_typed_preview_rejects_mid_embed_source_edit_with_failed_receipt(
    env, monkeypatch
):
    project, sheet, cols, index_id = env
    edited_row = _row(project, sheet, cols, "kitten")
    mutated = False

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            nonlocal mutated
            if not mutated:
                mutated = True
                project.apply_edits(
                    [
                        {
                            "row_id": edited_row,
                            "column_id": cols["headline"],
                            "value": "kitten edited during embed",
                        }
                    ]
                )
            return [list(_VECTORS[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    query = {**_manual_query(index_id, "cat"), "sheet_id": sheet}
    result = run_action_spec(
        project,
        _preview_action(query, key="typed-mid-embed-source-edit"),
        project_id="p",
    )

    assert result.status == "failed"
    assert result.errors[0].code == "embedding_source_stale"
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE action_kind='query.preview'"
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"


def test_typed_preview_rejects_mid_embed_provider_metadata_edit(env, monkeypatch):
    project, sheet, _cols, index_id = env
    mutated = False

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            nonlocal mutated
            if not mutated:
                mutated = True
                project.db.execute(
                    "UPDATE embedding_spaces SET provider_id=?, actual_model_id=? "
                    "WHERE id=(SELECT space_id FROM embedding_indexes WHERE id=?)",
                    ("mutated-provider", "mutated-model", index_id),
                )
                project.db.commit()
            return [list(_VECTORS[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    query = {**_manual_query(index_id, "cat"), "sheet_id": sheet}
    result = run_action_spec(
        project,
        _preview_action(query, key="typed-mid-embed-provider-edit"),
        project_id="p",
    )

    assert result.status == "failed"
    assert result.errors[0].code == "stale_input"
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE action_kind='query.preview'"
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"


def test_typed_preview_rejects_same_second_highest_vector_replacement(env, monkeypatch):
    project, sheet, cols, index_id = env
    reader = VectorBackend(project)
    item = reader.db.execute(
        "SELECT * FROM embedding_items WHERE index_id=? "
        "ORDER BY vector_id DESC LIMIT 1",
        (index_id,),
    ).fetchone()
    assert item is not None
    item_state = dict(item)
    row_id = int(item_state["source_key"])
    reader.close()
    mutated = False

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            nonlocal mutated
            if not mutated:
                mutated = True
                writer = VectorBackend(project)
                writer.ensure_schema()
                writer.upsert_item(
                    index_id=index_id,
                    space_id=item_state["space_id"],
                    source_key=str(row_id),
                    source_ref=json.loads(item_state["source_ref_json"]),
                    source_hash=item_state["source_hash"],
                    status="ready",
                    vector=[0.0] * 384,
                )
                replacement = writer.get_item(index_id, str(row_id))
                assert replacement is not None
                # Deleting and reinserting the highest rowid reuses it. Restore the
                # second-resolution timestamps too: every item metadata field now
                # matches the captured state while the supported upsert changed bytes.
                assert replacement["vector_id"] == item_state["vector_id"]
                writer.db.execute(
                    "UPDATE embedding_items SET source_ref_json=?, embedded_at=?, "
                    "updated_at=? WHERE index_id=? AND source_key=?",
                    (
                        item_state["source_ref_json"],
                        item_state["embedded_at"],
                        item_state["updated_at"],
                        index_id,
                        str(row_id),
                    ),
                )
                writer.db.commit()
                writer.close()
            return [list(_VECTORS[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    query = {**_manual_query(index_id, "cat"), "sheet_id": sheet}
    result = run_action_spec(
        project,
        _preview_action(query, key="typed-mid-embed-sidecar-edit"),
        project_id="p",
    )

    assert result.status == "failed"
    assert result.errors[0].code == "stale_input"
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE action_kind='query.preview'"
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"


@pytest.mark.parametrize("query_kind", ["embedding_similarity", "embedding_hybrid"])
def test_typed_preview_refuses_remote_query_text_before_gateway(
    env, monkeypatch, query_kind
):
    project, sheet, _cols, index_id = env
    project.db.execute(
        "UPDATE embedding_spaces SET provider_id='openai', "
        "provider_kind='platform_api' WHERE id=(SELECT space_id FROM "
        "embedding_indexes WHERE id=?)",
        (index_id,),
    )
    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        ('{"allow_remote":true}', index_id),
    )
    project.db.commit()
    model_calls_before = project.db.execute(
        "SELECT COUNT(*) FROM model_calls"
    ).fetchone()[0]
    spend_before = tuple(
        project.db.execute(
            "SELECT COALESCE(SUM(spent_micro), 0), "
            "COALESCE(SUM(unmetered_calls), 0) FROM project_provider_keys"
        ).fetchone()
    )

    def forbidden_gateway(*_args, **_kwargs):
        raise AssertionError("remote refusal must precede gateway construction")

    monkeypatch.setattr(
        "frisket.ai.embeddings.similarity.EmbeddingGateway", forbidden_gateway
    )
    query = (
        {**_manual_query(index_id, "cat"), "sheet_id": sheet}
        if query_kind == "embedding_similarity"
        else {
            "kind": "embedding_hybrid",
            "embedding_index_id": index_id,
            "sheet_id": sheet,
            "text": "cat",
        }
    )
    result = run_action_spec(
        project,
        _preview_action(query, key=f"typed-remote-{query_kind}"),
        project_id="p",
    )

    assert result.status == "failed"
    assert result.errors[0].code == "embedding_remote_preview_unsupported"
    assert result.errors[0].field == (
        "anchor.text" if query_kind == "embedding_similarity" else "text"
    )
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE action_kind='query.preview'"
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"
    assert (
        project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        == model_calls_before
    )
    spend = project.db.execute(
        "SELECT COALESCE(SUM(spent_micro), 0), "
        "COALESCE(SUM(unmetered_calls), 0) FROM project_provider_keys"
    ).fetchone()
    assert tuple(spend) == spend_before


def test_manual_text_blocks_remote_egress_without_policy(tmp_path):
    project = Project.create(tmp_path / "remote.frisket", name="remote")
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], cols)
    gw = MappedGateway(_VECTORS, dim=1536)
    # No refresh needed: the remote egress gate fires before any embedding /
    # ranking (and refresh of an allow_remote=False index would itself block).
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": False}, key="remote"
    )
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project, _manual_query(index_id, "cat"), gateway=gw
        )
    assert exc.value.code == "embedding_remote_confirmation_required"
    assert gw.calls == []  # never embedded — no egress
    project.close()


def test_manual_text_runs_when_policy_allows(tmp_path):
    project = Project.create(tmp_path / "remote2.frisket", name="remote2")
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], cols)
    gw = MappedGateway(_VECTORS, dim=1536)
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": True}, key="ok"
    )
    _refresh(project, index_id, gw)
    gw.calls.clear()
    result = resolve_embedding_similarity(
        project, _manual_query(index_id, "cat"), gateway=gw
    )
    assert gw.calls == [["cat"]]  # embedded the query once
    kitten = _row(project, sheet, cols, "kitten")
    # "cat" query vector ranks cat first (exact), then kitten, then airplane
    assert result.hits[0].row_id == _row(project, sheet, cols, "cat")
    assert result.hits[1].row_id == kitten


def test_hard_not_scans_past_100000_matching_vectors(env):
    project, sheet, cols, index_id = env
    airplane = _row(project, sheet, cols, "airplane")
    cat = _row(project, sheet, cols, "cat")
    kitten = _row(project, sheet, cols, "kitten")

    backend = VectorBackend(project)
    backend.ensure_schema()
    index = backend.get_item(index_id, str(airplane))
    assert index is not None
    # Share the real airplane vector across 100,000 synthetic sidecar entries. Their
    # fixed-width keys sort ahead of the real row key, placing that real excluded row
    # immediately beyond the legacy scan ceiling without allocating 100,000 vectors.
    backend.db.executemany(
        "INSERT INTO embedding_items ("
        "index_id, space_id, source_key, source_ref_json, source_hash, status, "
        "vector_table, vector_id, embedded_at) VALUES (?, ?, ?, '{}', 'synthetic', "
        "'ready', ?, ?, datetime('now'))",
        (
            (
                index_id,
                index["space_id"],
                f"{position:06d}",
                index["vector_table"],
                index["vector_id"],
            )
            for position in range(100_000)
        ),
    )
    backend.db.commit()
    backend.close()

    result = resolve_embedding_similarity(
        project,
        {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "anchor": {
                "kind": "manual_text_query",
                "text": "cat",
                "exclude": [{"text": "airplane"}],
            },
            "limit": 10,
        },
        gateway=MappedGateway(_VECTORS),
    )
    hit_ids = {hit.row_id for hit in result.hits}

    assert airplane not in hit_ids
    assert hit_ids == {cat, kitten}


# --------------------------------------------------------------------------
# Stale by CONTENT CHANGE (sidecar status='ready' is not enough)
# --------------------------------------------------------------------------


def _set_cell(project, sheet, cols, row_id, value):
    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=cols["headline"],
        value=value,
    )


def test_row_anchor_stale_on_content_change(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    # the sidecar item is still status='ready', but the source cell changed
    _set_cell(project, sheet, cols, cat, "cat rewritten")
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, _row_query(index_id, cat))
    assert exc.value.code == "embedding_source_stale"


def test_stale_candidate_is_excluded_not_returned(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    kitten = _row(project, sheet, cols, "kitten")
    airplane = _row(project, sheet, cols, "airplane")
    # kitten's nearest vector is stale (its cell changed since embedding) — it
    # must not be returned as a valid nearest match.
    _set_cell(project, sheet, cols, kitten, "kitten rewritten")
    result = resolve_embedding_similarity(project, _row_query(index_id, cat))
    assert [h.row_id for h in result.hits] == [airplane]


# --------------------------------------------------------------------------
# Vector shape validation
# --------------------------------------------------------------------------


def _remote_index(tmp_path, name):
    project = Project.create(tmp_path / f"{name}.frisket", name=name)
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], cols)
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": True}, key=name
    )
    return project, sheet, cols, index_id


class _TwoVectorGateway:
    def embed(self, texts, *, provider, model, modality):
        vecs = [[1.0] + [0.0] * 1535, [0.0] * 1536]  # two for one input
        return build_batch_result(
            vecs,
            provider_id="openai",
            provider_kind="platform_api",
            actual_model_id=model,
            modality=modality,
        )


class _WrongWidthGateway:
    def embed(self, texts, *, provider, model, modality):
        return build_batch_result(
            [[1.0, 0.0, 0.0, 0.0, 0.0]],  # width 5, not 1536
            provider_id="openai",
            provider_kind="platform_api",
            actual_model_id=model,
            modality=modality,
        )


def test_manual_text_wrong_vector_count_is_provider_error(tmp_path):
    project, sheet, cols, index_id = _remote_index(tmp_path, "twovec")
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project, _manual_query(index_id, "cat"), gateway=_TwoVectorGateway()
        )
    assert exc.value.code == "embedding_provider_error"
    project.close()


def test_manual_text_wrong_dimension_is_space_mismatch(tmp_path):
    project, sheet, cols, index_id = _remote_index(tmp_path, "wrongdim")
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project, _manual_query(index_id, "cat"), gateway=_WrongWidthGateway()
        )
    assert exc.value.code == "embedding_space_mismatch"
    project.close()


def test_row_anchor_wrong_stored_dimension_is_space_mismatch(env):
    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    backend = VectorBackend(project)
    backend.ensure_schema()
    stored_hash = backend.get_item(index_id, str(cat))["source_hash"]
    # a ready item whose source hash still matches but whose vector is the wrong
    # width (e.g. a provider upgrade changed dimensions)
    backend.upsert_item(
        index_id=index_id,
        space_id="s",
        source_key=str(cat),
        source_ref={},
        source_hash=stored_hash,
        status="ready",
        vector=[1.0, 0.0, 0.0],  # 3-d, not the 384-d space
    )
    backend.close()
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(project, _row_query(index_id, cat))
    assert exc.value.code == "embedding_space_mismatch"


# --------------------------------------------------------------------------
# Visible-result starvation (>64 hidden higher-ranked rows)
# --------------------------------------------------------------------------


def test_no_starvation_with_many_hidden_higher_ranked_rows(tmp_path):
    project = Project.create(tmp_path / "starve.frisket", name="starve")
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    # cat (anchor) + 70 near (identical to cat) + 2 far visible rows
    near = [f"near{i}" for i in range(70)]
    vectors = {"cat": [1.0, 0.0, 0.0], "far0": [0.0, 1.0, 0.0], "far1": [0.0, 1.0, 0.0]}
    for n in near:
        vectors[n] = [1.0, 0.0, 0.0]
    project.add_rows(
        sheet, [{"headline": t} for t in ["cat", *near, "far0", "far1"]], cols
    )
    index_id = _create_index(project, sheet, key="starve")
    _refresh(project, index_id, MappedGateway(vectors), key="starve")
    # hide the 70 nearer rows; only the 2 far rows remain visible
    near_ids = [_row(project, sheet, cols, n) for n in near]
    placeholders = ",".join("?" * len(near_ids))
    project.db.execute(
        f"UPDATE rows SET hidden=1 WHERE id IN ({placeholders})", near_ids
    )
    project.db.commit()
    cat = _row(project, sheet, cols, "cat")
    result = resolve_embedding_similarity(project, _row_query(index_id, cat, limit=2))
    # not starved by the 70 hidden near rows — the 2 visible far rows are returned
    assert {h.row_id for h in result.hits} == {
        _row(project, sheet, cols, "far0"),
        _row(project, sheet, cols, "far1"),
    }
    project.close()


# --- coordinator review: product-path freshness gate + pagination (query_preview) ---


def test_query_preview_blocks_incomplete_after_append(env):
    # The shared query-preview path must NOT silently resolve a partial index: an
    # appended, unembedded row -> typed embedding_index_incomplete (not a stale search).
    from frisket.preview.query import QueryPreviewError, resolve_query_preview

    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    project.add_rows(sheet, [{"headline": "lion"}], cols)  # unembedded
    with pytest.raises(QueryPreviewError) as excinfo:
        resolve_query_preview(project, _row_query(index_id, cat, sheet_id=sheet))
    assert excinfo.value.code == "embedding_index_incomplete"


def test_query_preview_limit_zero_is_empty_window_total_preserved(env):
    # limit=0 is an EMPTY window, not "return all"; total stays the full hit count.
    from frisket.preview.query import resolve_query_preview

    project, sheet, cols, index_id = env
    cat = _row(project, sheet, cols, "cat")
    full = resolve_query_preview(project, _row_query(index_id, cat, sheet_id=sheet))
    assert full.total > 0 and full.row_ids
    zero = resolve_query_preview(
        project, _row_query(index_id, cat, sheet_id=sheet), limit=0
    )
    assert zero.row_ids == [] and zero.row_count == 0 and zero.scores == {}
    assert zero.total == full.total  # total preserved

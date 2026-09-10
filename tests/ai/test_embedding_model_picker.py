from __future__ import annotations

import pytest

from frisket.ai.embeddings import EmbeddingGateway, EmbeddingStore
from frisket.ai.embeddings.capabilities import embedding_capabilities
from frisket.engine.executor import run_action_spec
from frisket.semantic import local_embedder
from frisket.engine.store import Project

_BGE_SMALL = "BAAI/bge-small-en-v1.5"
_BGE_LARGE = "BAAI/bge-large-en-v1.5"


def test_catalog_offers_multiple_local_models_with_factual_metadata():
    caps = embedding_capabilities(local_available=True)
    local = [c for c in caps if c["provider_id"] == "fastembed"]
    assert len(local) >= 2, "the picker needs a real choice of local models"
    for c in local:
        # factual only: a dimension + a size; NO use_case editorial field
        assert c["dimensions"] and c["dimensions"][0] > 0
        assert isinstance(c.get("size_gb"), (int, float))
        assert "use_case" not in c
    # exactly one sensible default is flagged
    assert sum(1 for c in local if c.get("recommended")) == 1
    ids = {c["model_id"] for c in local}
    assert _BGE_SMALL in ids and _BGE_LARGE in ids


def _create(project, sheet, model, key):
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "model": model,
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": key,
        },
        project_id="p",
    )


def test_create_with_chosen_model_sets_its_dimension(tmp_path):
    # The chosen model's dimension flows into the space at create (metadata only,
    # no download): bge-large is 1024-d, distinct from the 384-d default.
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "headline")
    store = EmbeddingStore(project)

    res = _create(project, sheet, _BGE_LARGE, "c@large")
    assert res.status == "completed", res.errors
    index = store.get_index(res.outputs[0].ref["index_id"])
    space = store.get_space(index["space_id"])
    assert space["dimension"] == 1024
    assert "bge-large" in space["actual_model_id"]


@pytest.mark.skipif(
    local_embedder() is None,
    reason="bundled local FastEmbed runtime unavailable or disabled",
)
@pytest.mark.network
@pytest.mark.real
def test_chosen_model_is_actually_honored_at_embed():
    # The gateway must use the REQUESTED model, not a hardcoded one: bge-small and
    # the multilingual default produce DIFFERENT vectors for the same text.
    gw = EmbeddingGateway()
    chosen = gw.embed(
        ["hello world"], provider="fastembed", model=_BGE_SMALL, modality="text"
    )
    default = gw.embed(
        ["hello world"], provider="fastembed", model=None, modality="text"
    )
    assert "bge-small" in chosen["actual_model_id"]
    assert chosen["dimension"] == 384
    assert chosen["vectors"][0] != default["vectors"][0]  # honored, not hardcoded


# ---- Slice 2: anti-fabrication cross-check + size guard + column-type disable ----


@pytest.mark.skipif(
    local_embedder() is None,
    reason="needs fastembed installed to cross-check against its registry",
)
def test_catalog_numbers_are_sourced_from_fastembed_not_invented():
    # Every curated local model's dimension + size MUST match fastembed's own
    # registry — so a number can never be fabricated or silently drift.
    from fastembed import TextEmbedding

    from frisket.semantic import _FASTEMBED_ALIASES

    src = {m["model"]: m for m in TextEmbedding.list_supported_models()}
    for cap in embedding_capabilities(local_available=True):
        if cap["provider_id"] != "fastembed" or "text" not in cap["modalities"]:
            continue
        mid = _FASTEMBED_ALIASES.get(cap["model_id"], cap["model_id"])
        ref = src.get(mid)
        assert ref is not None, f"{cap['model_id']} is not a real fastembed model"
        assert cap["dimensions"][0] == ref["dim"]
        assert abs(cap["size_gb"] - float(ref["size_in_GB"])) < 0.05


def test_size_guard_blocks_oversized_model(tmp_path, monkeypatch):
    # The picked model's on-disk size must be under the cap BEFORE any download.
    monkeypatch.setenv("FRISKET_EMBEDDING_MAX_MODEL_GB", "0.1")
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "headline")

    res = _create(project, sheet, _BGE_LARGE, "c@big")  # bge-large is 1.2 GB > 0.1
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_model_too_large"
    assert res.errors[0].details["size_gb"] == 1.2


def test_modality_incompatible_models_are_disabled_not_hidden():
    # For an image column, the text models are shown but DISABLED with a reason
    # (the UI greys them out) rather than silently filtered away.
    from frisket.server.embedding_catalog import embedding_provider_catalog_payload

    payload = embedding_provider_catalog_payload(
        source_column_type="image", local_available=True
    )
    by_model = {p["model_id"]: p for p in payload["providers"]}
    text_model = by_model["BAAI/bge-small-en-v1.5"]
    assert text_model["modality_compatible"] is False
    assert text_model["disabled_reason"]  # a human reason, not just hidden


# ---- Slice 2b: custom fastembed ids + unsupported-id error + token-limit guard ----

# A real fastembed model id that is NOT in _KNOWN_ENGINES (so it must be resolved
# from fastembed.TextEmbedding.list_supported_models()), 768-d.
_BGE_BASE = "BAAI/bge-base-en-v1.5"
_E5_LARGE = "intfloat/multilingual-e5-large"  # ~2.24 GB, not curated


@pytest.mark.skipif(
    local_embedder() is None,
    reason="needs fastembed installed to resolve a custom model id from its registry",
)
def test_create_with_custom_fastembed_model_id(tmp_path):
    # A fastembed-supported id NOT in the curated list must still create, with the
    # space dimension taken from the fastembed registry (bge-base is 768-d).
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "headline")
    store = EmbeddingStore(project)

    res = _create(project, sheet, _BGE_BASE, "c@custom")
    assert res.status == "completed", res.errors
    index = store.get_index(res.outputs[0].ref["index_id"])
    space = store.get_space(index["space_id"])
    assert space["dimension"] == 768
    assert "bge-base" in space["actual_model_id"]


def test_unsupported_model_id_rejected(tmp_path):
    # A model id that is neither curated nor fastembed-supported must fail with a
    # typed error and write NO space/index rows.
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "headline")

    res = _create(project, sheet, "totally/not-a-real-model", "c@bad")
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_model_unsupported"
    n_idx = project.db.execute(
        "SELECT COUNT(*) AS n FROM embedding_indexes"
    ).fetchone()["n"]
    n_sp = project.db.execute("SELECT COUNT(*) AS n FROM embedding_spaces").fetchone()[
        "n"
    ]
    assert n_idx == 0 and n_sp == 0


@pytest.mark.skipif(
    local_embedder() is None,
    reason="needs fastembed installed to resolve a large custom model's size",
)
def test_size_guard_applies_to_custom_fastembed_id(tmp_path, monkeypatch):
    # The size guard must apply to a synthesized custom-id capability too: a large
    # custom fastembed model (~2.24 GB) is blocked when the cap is set low.
    monkeypatch.setenv("FRISKET_EMBEDDING_MAX_MODEL_GB", "0.5")
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "headline")

    res = _create(project, sheet, _E5_LARGE, "c@e5big")
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_model_too_large"


def test_token_limit_guard_surfaces_truncated_inputs(tmp_path):
    # An input far over the model's max_input_tokens is silently truncated by the
    # provider; the refresh must SURFACE the count of likely-truncated items
    # (truncated_items) in the result/receipt so the user can see it.
    from frisket.ai.embeddings import build_batch_result
    from frisket.engine.executor import ExecutorDeps

    class _FakeGateway:
        def embed(self, texts, *, provider, model, modality):
            return build_batch_result(
                [[0.1] * 384 for _ in texts],
                provider_id=provider or "fastembed",
                provider_kind="local_process",
                requested_model=model,
                actual_model_id=model or "fake",
                modality=modality,
            )

    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    # One short row + one row FAR over a 512-token model (100k chars ≈ 25k tokens).
    project.add_rows(
        sheet, [{"headline": "short"}, {"headline": "x" * 100_000}], {"headline": col}
    )

    # default multilingual model: 384-d, max_input_tokens 512
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
            "idempotency_key": "tok@create",
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
            "idempotency_key": "tok@refresh",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=_FakeGateway()),
    )
    assert refresh.status == "completed", refresh.errors
    assert refresh.outputs[0].ref["truncated_items"] >= 1

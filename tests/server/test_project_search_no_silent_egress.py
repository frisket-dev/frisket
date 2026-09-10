"""Project search never egresses the corpus to a remote embeddings API.

Closure-sweep audit L6: ``ProjectSearchService.search`` used to pass a
literal ``allow_remote=True`` into ``resolve_embedder``. With no local model
installed that resolved the router's remote embedding API and shipped every
indexed text cell in the project to a third party — through a plain GET
``/search`` that has no cost gate, no estimate, and no consent dialog
anywhere in front of it.

The seam is now ``allow_remote=False``, so a project with no local embedder
degrades to the lexical fallback instead of billing and egressing.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

import frisket.semantic as semantic_module
import frisket.search as search_module
from frisket.server.services.project_search import ProjectSearchService
from frisket.server.workspace import Workspace


class _EgressTripwire:
    """A router that HAS a remote embedding backend and screams if used."""

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []

    def has_embedding_backend(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        raise AssertionError(
            "project search embedded the corpus through the remote API — "
            "that egress has no cost gate and no consent dialog (L6)"
        )


def _project_with_text(tmp_path: Path):
    workspace = Workspace(tmp_path / "ws")
    info = workspace.create("search egress")
    project = workspace.get(info["id"])
    sheet_id = project.add_sheet("notes")
    columns = {"body": project.add_column(sheet_id, "body", type="text")}
    project.add_rows(
        sheet_id,
        [
            {"body": "the mayor approved the stadium bond"},
            {"body": "reporters questioned the stadium bond"},
        ],
        columns,
    )
    return workspace, info["id"]


def test_semantic_search_without_a_local_embedder_does_not_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ops escape hatch local_embedder() honors: no in-process model, which
    # is exactly the condition under which the old code fell through to the
    # router's remote API.
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "2")
    model_cache = tmp_path / "fastembed-cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(model_cache))
    monkeypatch.setattr(semantic_module, "_local_models", {})
    monkeypatch.setattr(search_module, "_rerank_model", None)
    real_import = builtins.__import__

    def no_fastembed_import(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise AssertionError("disabled semantic search imported FastEmbed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastembed_import)

    from frisket.ai.embeddings import EmbeddingBackendUnavailable, EmbeddingGateway

    assert semantic_module.local_embedder() is None
    assert semantic_module.local_image_embedder() is None
    assert semantic_module.resolve_embedder() is None
    assert search_module.local_reranker() is None
    with pytest.raises(EmbeddingBackendUnavailable):
        EmbeddingGateway().embed(
            ["generic text"],
            provider="fastembed",
            model="BAAI/bge-small-en-v1.5",
            modality="text",
        )
    with pytest.raises(EmbeddingBackendUnavailable):
        EmbeddingGateway().embed(
            ["/tmp/image.png"],
            provider="fastembed",
            model="Qdrant/clip-ViT-B-32-vision",
            modality="image",
        )

    workspace, project_id = _project_with_text(tmp_path)
    tripwire = _EgressTripwire()
    monkeypatch.setattr(
        Workspace, "router_for", lambda self, project: tripwire, raising=True
    )

    hits = ProjectSearchService(workspace).search(
        project_id, q="stadium bond", limit=10, mode="semantic", rerank="auto"
    )

    # The request is still SERVED — degraded to lexical, not refused ...
    assert len(hits) == 2 and all(hit["semantic"] is False for hit in hits)
    assert all("rerank_score" not in hit for hit in hits)
    # ... and not one cell left the machine.
    assert tripwire.embed_calls == []
    # Capability-disable must not initialize FastEmbed or create its model cache.
    assert semantic_module._local_models == {}
    assert search_module._rerank_model is None
    assert not model_cache.exists()

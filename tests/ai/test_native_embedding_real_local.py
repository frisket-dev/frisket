"""REAL local embeddings end-to-end (no fake gateway).

The native embedding stack (Stages 1-5) is otherwise covered ONLY with injected fake
gateways. This burns a real fastembed vector through index_create -> index_refresh ->
embedding_similarity and asserts the ranking is semantically sensible — the live-demo
proof a capability claim needs, not just a green suite. The bundled runtime is
still gated by the network marker because a cold cache downloads model weights.
It also skips under FRISKET_DISABLE_LOCAL_EMBED=1 (for example, the eval oracle).
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import EmbeddingGateway
from frisket.ai.embeddings.similarity import resolve_embedding_similarity
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.semantic import local_embedder
from frisket.engine.store import Project

pytestmark = [
    pytest.mark.network,
    pytest.mark.real,
    pytest.mark.skipif(
        local_embedder() is None,
        reason="bundled local FastEmbed runtime unavailable or disabled",
    ),
]

_ROWS = [
    "the cat sat on the mat",  # anchor
    "a feline rested on the rug",  # near-synonym -> should rank first
    "quarterly tax filing deadline",  # unrelated -> should rank last
]


def _row(project, sheet, col, text):
    return next(r for r, v in project.get_values(sheet, col).items() if v == text)


def test_real_local_text_embedding_end_to_end(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(sheet, [{"headline": t} for t in _ROWS], {"headline": col})

    created = run_action_spec(
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
            "idempotency_key": "c@1",
        },
        project_id="p",
    )
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]

    # REAL gateway — in-process fastembed produces actual vectors into the sidecar.
    refreshed = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@1",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=EmbeddingGateway()),
    )
    assert refreshed.status == "completed", refreshed.errors

    cat = _row(project, sheet, col, _ROWS[0])
    feline = _row(project, sheet, col, _ROWS[1])
    tax = _row(project, sheet, col, _ROWS[2])

    result = resolve_embedding_similarity(
        project,
        {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "anchor": {"kind": "row", "row_id": cat},
            "limit": 5,
        },
        gateway=None,  # row anchor -> reads the stored vector, no provider call
    )
    ranked = [h.row_id for h in result.hits]
    assert feline in ranked and tax in ranked
    # the near-synonym is genuinely closer than the unrelated row
    assert ranked.index(feline) < ranked.index(tax)
    by_row = {h.row_id: h for h in result.hits}
    assert by_row[feline].distance < by_row[tax].distance
    # a real cosine score in (0, 1), not a stub
    assert by_row[feline].score is not None and 0.0 < by_row[feline].score <= 1.0

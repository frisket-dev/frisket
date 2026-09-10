"""the first REAL media embedding path (IMAGE).

Image embeddings run IN-PROCESS via fastembed's light ONNX ``ImageEmbedding``
(``Qdrant/clip-ViT-B-32-vision``, 512-d, ~0.34GB, onnxruntime, NO torch) — the
same shape as the local text embedder. This burns REAL CLIP-ONNX vectors through
index_create -> index_refresh -> embedding_similarity and asserts an honest
image-to-image ranking: a near-red square ranks ahead of a blue square when the
anchor is a red square.

Skipped when no local image embedder is available (FastEmbed image / PIL absent
or FRISKET_DISABLE_LOCAL_EMBED=1). The proof carries the network marker because
a cold cache downloads the ONNX model once.

The DISABLED-HONESTY assertions (audio/video/file still block at create) run
either way — they need no model.
"""

from __future__ import annotations

import io

import pytest

from frisket.ai.embeddings import EmbeddingGateway
from frisket.ai.embeddings.similarity import resolve_embedding_similarity
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.semantic import local_image_embedder
from frisket.engine.store import Project

_needs_image = pytest.mark.skipif(
    local_image_embedder() is None,
    reason="needs fastembed ImageEmbedding + PIL for REAL local image vectors",
)


def _png(rgb: tuple[int, int, int]) -> bytes:
    """A tiny solid-color PNG (real bytes a real CLIP-ONNX model can read)."""
    from PIL import Image

    img = Image.new("RGB", (32, 32), rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _add_image_rows(project, sheet, col):
    """red, near-red, blue solid squares as image blobs. Returns their row ids."""
    swatches = {
        "red": (220, 20, 20),
        "near_red": (200, 40, 40),
        "blue": (20, 20, 220),
    }
    row_by_name: dict[str, int] = {}
    for name, rgb in swatches.items():
        blob = project.add_blob(_png(rgb), filename=f"{name}.png", mime="image/png")
        envelope = {"blob": blob, "mime": "image/png", "filename": f"{name}.png"}
        rid = project.add_rows(sheet, [{"pic": envelope}], {"pic": col})[0]
        row_by_name[name] = rid
    return row_by_name


def _create_image_index(project, sheet):
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["pic"],
                "modality": "image",
                "provider": "fastembed",
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "img-create@1",
        },
        project_id="p",
    )


@pytest.mark.network
@pytest.mark.real
@_needs_image
def test_real_image_embedding_end_to_end(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "pic", type="image")
    rows = _add_image_rows(project, sheet, col)

    created = _create_image_index(project, sheet)
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]
    # the minted space declares the real CLIP-ONNX width
    assert created.outputs[0].ref["dimension"] == 512

    # REAL gateway — in-process fastembed ImageEmbedding produces actual vectors.
    refreshed = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "img-refresh@1",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=EmbeddingGateway()),
    )
    assert refreshed.status == "completed", refreshed.errors
    assert refreshed.outputs[0].ref["refreshed"] == 3
    assert refreshed.outputs[0].ref["ready_items"] == 3

    # the stored space is exactly 512-d
    space_dim = project.db.execute("SELECT dimension FROM embedding_spaces").fetchone()[
        0
    ]
    assert int(space_dim) == 512

    # ROW-anchor similarity from the red image: near-red ranks ahead of blue.
    # No re-embed — the resolver reads the stored vector for a row anchor.
    result = resolve_embedding_similarity(
        project,
        {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "anchor": {"kind": "row", "row_id": rows["red"]},
            "limit": 5,
        },
        gateway=None,
    )
    ranked = [h.row_id for h in result.hits]
    assert rows["near_red"] in ranked and rows["blue"] in ranked
    assert ranked.index(rows["near_red"]) < ranked.index(rows["blue"])
    by_row = {h.row_id: h for h in result.hits}
    assert by_row[rows["near_red"]].distance < by_row[rows["blue"]].distance
    # a real cosine score in (0, 1], not a stub
    score = by_row[rows["near_red"]].score
    assert score is not None and 0.0 < score <= 1.0


def _create_media_index(project, sheet, modality, model):
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["m"],
                "modality": modality,
                "provider": "frisket-models",
                "model": model,
            },
            "idempotency_key": f"{modality}@1",
        },
        project_id="p",
    )


def test_audio_video_file_index_create_still_blocks_disabled():
    """Disabled-honesty preserved: audio/video/file engines are NOT built, so a
    media index over them STILL blocks at create with embedding_backend_unavailable
    (this assertion needs no model and runs regardless of the image engine)."""
    import tempfile

    cases = {
        "audio": "audio-embedding",
        "video": "video-embedding",
        "file": "file-page-embedding",
    }
    for modality, model in cases.items():
        with tempfile.TemporaryDirectory() as d:
            project = Project.create(f"{d}/p.frisket", name="p")
            sheet = project.add_sheet("s")
            project.add_column(sheet, "m", type=modality)
            res = _create_media_index(project, sheet, modality, model)
            assert res.status == "failed", (modality, res)
            assert res.errors[0].code == "embedding_backend_unavailable", modality
            # nothing minted: no un-refreshable index/space left behind
            assert (
                project.db.execute("SELECT COUNT(*) FROM embedding_indexes").fetchone()[
                    0
                ]
                == 0
            ), modality
            assert (
                project.db.execute("SELECT COUNT(*) FROM embedding_spaces").fetchone()[
                    0
                ]
                == 0
            ), modality

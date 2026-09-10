"""multimodal honesty + the blob-backed source-payload seam.

No image engine is installed in this env (fastembed/open_clip/torch/PIL absent), so
REAL image vectors are BLOCKED. This slice ships the HONEST path: a media index whose
engine is unavailable fails AT CREATE with a typed disabled-with-reason (so no
un-refreshable image index is minted), media source payloads hash the BLOB CONTENT
(not the stringified envelope), and the gateway has a typed image seam.
"""

from __future__ import annotations


from frisket.ai.embeddings import EmbeddingBackendUnavailable, EmbeddingGateway
from frisket.ai.embeddings.source_payload import build_source_payloads, source_hash
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from helpers import replace_test_source_cell

_CLIP = "open_clip/ViT-B-32/laion2b_s34b_b79k"


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
                "provider": "frisket-models",
                "model": _CLIP,
            },
            "idempotency_key": "img@1",
        },
        project_id="p",
    )


def test_image_index_create_disabled_with_reason_when_engine_absent(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    project.add_column(sheet, "pic", type="image")

    res = _create_image_index(project, sheet)
    assert res.status == "failed"
    err = res.errors[0]
    assert err.code == "embedding_backend_unavailable"
    # a real reason, surfaced for the picker / user
    assert (err.details or {}).get("disabled_reason")
    # nothing minted: no un-refreshable image index/space left behind
    assert (
        project.db.execute("SELECT COUNT(*) FROM embedding_indexes").fetchone()[0] == 0
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM embedding_spaces").fetchone()[0] == 0
    )


def test_build_source_payloads_image_column_uses_blob_hash_not_repr(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "pic", type="image")
    # a realistic 64-hex content digest (the format Project.add_blob stores under; a
    # malformed/short digest is now rejected to a None path by the traversal guard).
    blob_a, blob_b = "a" * 64, "b" * 64
    envelope = {"blob": blob_a, "mime": "image/png", "filename": "x.png"}
    rid = project.add_rows(sheet, [{"pic": envelope}], {"pic": col})[0]

    index = {"sheet_id": sheet, "source_policy_hash": "sha256:policy"}
    out = build_source_payloads(project, index, ["pic"], [rid])
    assert len(out) == 1
    entry = out[0]
    media = entry["source_ref"].get("media")
    assert media and media[0]["blob_hash"] == blob_a
    assert media[0]["materializable"] is True
    assert "path" not in media[0]  # durable refs never persist a host temp path
    # the hash is over the blob CONTENT id, not the stringified envelope dict
    assert entry["source_hash"] != source_hash(
        "sha256:policy", [["pic", str(envelope)]]
    )

    # changing the blob hash changes the source_hash (staleness can be detected)
    replace_test_source_cell(
        project,
        row_id=rid,
        column_id=col,
        value={"blob": blob_b, "mime": "image/png"},
    )
    out2 = build_source_payloads(project, index, ["pic"], [rid])
    assert out2[0]["source_hash"] != entry["source_hash"]


def test_gateway_image_modality_routes_to_sidecar_typed_unavailable(tmp_path):
    # media embeddings are served by the frisket-models sidecar, not in-process; the
    # gateway fails LOUD (never fakes) until that sidecar route + client land.
    gw = EmbeddingGateway()
    try:
        gw.embed(
            ["/tmp/x.png"], provider="frisket-models", model=_CLIP, modality="image"
        )
        raise AssertionError("expected EmbeddingBackendUnavailable")
    except EmbeddingBackendUnavailable as exc:
        msg = str(exc).lower()
        assert "image" in msg
        # names the real home (the sidecar), not a blanket reject
        assert "sidecar" in msg


def test_image_blob_traversal_digest_is_not_materializable(tmp_path):
    # Review (LOW security): a media cell whose blob "digest" is a path-traversal string
    # must NOT be materializable — otherwise the image embedder could open a file
    # outside the project blob store. A malformed digest leaves no host path in the
    # durable source ref and refresh surfaces an honest error.
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "pic", type="image")
    envelope = {"blob": "../../../../etc/passwd", "mime": "image/png"}
    rid = project.add_rows(sheet, [{"pic": envelope}], {"pic": col})[0]
    out = build_source_payloads(
        project, {"sheet_id": sheet, "source_policy_hash": "p"}, ["pic"], [rid]
    )
    media = out[0]["source_ref"].get("media")
    assert media and media[0]["blob_hash"] == "../../../../etc/passwd"
    assert media[0]["materializable"] is False
    assert "path" not in media[0]

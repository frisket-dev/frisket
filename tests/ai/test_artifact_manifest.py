"""Pins for the pinned-artifact manifest machinery."""

from __future__ import annotations

import hashlib
import re

from frisket.ai.models import artifact_manifest as am
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile
from frisket.semantic import PROVIDERLESS_CLASSIFY_MODEL

_SPDX_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_single_file_composite_digest_is_the_file_digest():
    f = PinnedFile("model.gguf", "https://example/model.gguf", _sha(b"x"), 1)
    art = PinnedArtifact(
        ref="hf:o/r@rev/model.gguf",
        kind="hf_file",
        display_name="X",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://example/license",
        source_url="https://example/o/r@rev",
        files=(f,),
    )
    assert art.composite_digest == f"sha256:{_sha(b'x')}"
    assert art.total_size == 1


def test_multi_file_composite_digest_is_order_independent():
    a = PinnedFile("model.bin", "https://e/model.bin", _sha(b"a"), 3)
    b = PinnedFile("source.spm", "https://e/source.spm", _sha(b"b"), 5)
    art1 = PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://e/cc-by",
        source_url="https://e/opus-mt-en-es-ct2@rev",
        files=(a, b),
    )
    art2 = PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://e/cc-by",
        source_url="https://e/opus-mt-en-es-ct2@rev",
        files=(b, a),  # reversed order
    )
    assert art1.composite_digest == art2.composite_digest
    # deterministic recompute
    expected = hashlib.sha256(
        "\n".join(
            sorted([f"{_sha(b'a')}  model.bin", f"{_sha(b'b')}  source.spm"])
        ).encode("utf-8")
    ).hexdigest()
    assert art1.composite_digest == f"sha256:{expected}"
    assert art1.total_size == 8


def test_shipped_manifest_entries_are_well_formed():
    for entry in am.all_pinned():
        assert entry.ref
        assert entry.kind in ("ct2_pair", "hf_file", "hf_snapshot", "spacy_model")
        assert _SPDX_RE.match(entry.license), entry.license
        assert entry.license_url.startswith("https://")
        assert entry.source_url.startswith("https://")
        if entry.kind == "hf_snapshot":
            assert entry.integrity == "hf_revision_pinned"
            assert entry.files == ()
            assert entry.total_size is None
            assert entry.composite_digest.startswith("hf-revision:")
            snap = entry.hf_snapshot
            assert snap is not None
            assert snap.repo_id
            assert snap.revision
            assert snap.files, "an hf_snapshot entry must pin at least one file"
            continue
        assert entry.integrity == "checksum"
        assert entry.hf_snapshot is None
        assert entry.files, "an entry must pin at least one file"
        assert entry.total_size == sum(f.size for f in entry.files)
        for f in entry.files:
            assert re.fullmatch(r"[0-9a-f]{64}", f.sha256), f.sha256
            assert f.size > 0
            assert f.source.startswith("https://")


def test_opus_mt_source_url_shape():
    url = am.opus_mt_source_url("en", "es", "abc123", "model.bin")
    assert url == (
        "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/abc123/model.bin"
    )


def test_opus_mt_pins_agree_with_the_org_constant():
    # The constant, generator, and pinned URLs must name ONE publisher org
    # (frisket-models) so a newly converted pair is uploaded to and pulled from
    # the same place — never a 404 pin. Every frisket-hosted CT2
    # pair file must resolve under the org constant; the generator must mint
    # URLs under the same org.
    org_base = f"https://huggingface.co/{am.FRISKET_HF_ORG}/"
    assert am.FRISKET_HF_ORG == "frisket-models"
    assert am.opus_mt_source_url("en", "es", "r", "model.bin").startswith(org_base)
    for entry in am.all_pinned():
        if entry.kind != "ct2_pair":
            continue  # upstream hf_file artifacts (e.g. tencent/...) are not ours
        assert entry.source_url.startswith(org_base), entry.source_url
        for f in entry.files:
            assert f.source.startswith(org_base), f.source


def test_lookup_missing_returns_none():
    assert am.lookup("opus-mt:xx-yy") is None


def test_spacy_model_wheel_is_checksum_pinned():
    entry = am.spacy_model_artifact()
    assert entry is not None
    assert entry.ref == am.SPACY_MODEL_REF == "spacy:en_core_web_sm@3.8.0"
    assert entry.kind == "spacy_model"
    assert entry.integrity == "checksum"
    assert entry.license == "MIT"
    assert entry.total_size == 12_806_118
    assert entry.files[0].sha256 == (
        "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
    )


def test_whisper_base_artifact_is_pinned_hf_snapshot():
    """faster-whisper's default 'base' size is pinned the same way as the
    two Parakeet entries (task: complete the local-model provisioning
    story) -- one accessor, one ref constant, gated through the shared
    hf_snapshot manifest machinery."""
    entry = am.whisper_base_artifact()
    assert entry is not None
    assert entry.ref == am.WHISPER_BASE_REF
    assert entry.kind == "hf_snapshot"
    assert entry.integrity == "hf_revision_pinned"
    assert entry.license == "MIT"
    assert am.lookup(am.WHISPER_BASE_REF) is entry
    snap = entry.hf_snapshot
    assert snap is not None
    assert snap.repo_id == am.WHISPER_BASE_HF_REPO == "Systran/faster-whisper-base"
    assert snap.revision and len(snap.revision) == 40  # a full git SHA, not a tag
    assert set(snap.files) == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }
    assert entry.approx_size_bytes and entry.approx_size_bytes > 100_000_000


def test_providerless_classifier_is_pinned_hf_snapshot():
    entry = am.providerless_classify_artifact()
    assert entry is not None
    assert PROVIDERLESS_CLASSIFY_MODEL == "BAAI/bge-small-en-v1.5"
    assert entry.ref == am.PROVIDERLESS_CLASSIFY_REF
    assert entry.kind == "hf_snapshot"
    assert entry.integrity == "hf_revision_pinned"
    assert entry.license == "Apache-2.0"
    assert am.lookup(am.PROVIDERLESS_CLASSIFY_REF) is entry

    snap = entry.hf_snapshot
    assert snap is not None
    assert snap.repo_id == "qdrant/bge-small-en-v1.5-onnx-q"
    assert snap.revision == "52398278842ec682c6f32300af41344b1c0b0bb2"
    assert snap.files == (
        "config.json",
        "model_optimized.onnx",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    )


def test_hf_snapshot_integrity_and_digest_shape():
    snap = am.HfSnapshotSource(
        repo_id="owner/repo", revision="abc123", files=("a.onnx", "b.onnx")
    )
    art = PinnedArtifact(
        ref="hf-snapshot:owner/repo@abc123",
        kind="hf_snapshot",
        display_name="X",
        manifest_version="2026.07.2",
        license="MIT",
        license_url="https://opensource.org/licenses/MIT",
        source_url="https://huggingface.co/owner/repo/tree/abc123",
        files=(),
        hf_snapshot=snap,
    )
    assert art.integrity == "hf_revision_pinned"
    assert art.total_size is None
    assert art.composite_digest == "hf-revision:owner/repo@abc123"

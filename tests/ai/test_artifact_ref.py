"""Grammar pins for scheme-prefixed artifact references."""

from __future__ import annotations

import pytest

from frisket.engine.jobs.artifact_ref import (
    MAX_ARTIFACT_REF_LENGTH,
    InvalidModelRefError,
    normalize_artifact_ref,
)
from frisket.engine.jobs.model_pull import normalize_model_ref


@pytest.mark.parametrize(
    "raw",
    ["llama3:8b", "namespace/name:tag", "smollm:135m", "qwen3:8b", "mymodel"],
)
def test_qualified_local_model_keeps_one_canonical_identity(raw):
    expected = normalize_model_ref(raw)
    ref = normalize_artifact_ref(f"ollama/@desktop/{raw}")
    assert ref.canonical == f"ollama/@desktop/{expected}"
    assert ref.scheme == "ollama"
    assert ref.endpoint_id == "desktop"
    assert ref.ollama_ref == expected


@pytest.mark.parametrize("raw", ["llama3:8b", "ollama:llama3:8b", "ollama/llama3:8b"])
def test_unqualified_or_colon_local_model_spelling_is_rejected(raw):
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref(raw)


@pytest.mark.parametrize(
    "raw,pair",
    [
        ("opus-mt:en-es", "en-es"),
        ("opus-mt:de-en", "de-en"),
        ("opus-mt:en-zh", "en-zh"),
        ("opus-mt:zh-hans-en", "zh-hans-en"),
    ],
)
def test_opus_mt_pairs_normalize(raw, pair):
    ref = normalize_artifact_ref(raw)
    assert ref.scheme == "opus-mt"
    assert ref.pair == pair
    assert ref.canonical == f"opus-mt:{pair}"


@pytest.mark.parametrize(
    "raw",
    ["opus-mt:en", "opus-mt:", "opus-mt:EN-ES", "opus-mt:en_es", "opus-mt:123-456789"],
)
def test_opus_mt_malformed_rejected(raw):
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref(raw)


def test_hf_file_normalizes():
    ref = normalize_artifact_ref(
        "hf:frisket-models/hy-mt2-1.8b-gguf@a1b2c3d/hy-mt2-1.8b.Q4_K_M.gguf"
    )
    assert ref.scheme == "hf"
    assert ref.hf_repo == "frisket-models/hy-mt2-1.8b-gguf"
    assert ref.hf_revision == "a1b2c3d"
    assert ref.hf_path == "hy-mt2-1.8b.Q4_K_M.gguf"
    assert ref.canonical == (
        "hf:frisket-models/hy-mt2-1.8b-gguf@a1b2c3d/hy-mt2-1.8b.Q4_K_M.gguf"
    )


def test_hf_missing_revision_rejected():
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref("hf:owner/repo/file.gguf")


def test_hf_path_traversal_rejected():
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref("hf:owner/repo@rev/../etc/passwd")


def test_hf_absolute_path_rejected():
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref("hf:owner/repo@rev//abs.gguf")


def test_hf_extension_not_in_allowlist_rejected():
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref("hf:owner/repo@rev/evil.sh")


def test_hf_nested_path_allowed():
    ref = normalize_artifact_ref("hf:nvidia/diar@v1/onnx/model.onnx")
    assert ref.hf_path == "onnx/model.onnx"


def test_hf_snapshot_pinned_refs_roundtrip():
    # The manifest is the allowlist: every pinned hf-snapshot ref must parse
    # back to itself (scheme kept in canonical, repo/revision split out).
    from frisket.ai.models.artifact_manifest import (
        PARAKEET_MODEL_REF,
        PARAKEET_VAD_REF,
        WHISPER_BASE_REF,
    )

    for pinned_ref in (PARAKEET_MODEL_REF, PARAKEET_VAD_REF, WHISPER_BASE_REF):
        ref = normalize_artifact_ref(pinned_ref)
        assert ref.scheme == "hf-snapshot"
        assert ref.canonical == pinned_ref
        assert f"hf-snapshot:{ref.hf_repo}@{ref.hf_revision}" == pinned_ref
        assert ref.hf_path is None


def test_hf_snapshot_unpinned_repo_rejected():
    # Rule-16 boundary: a request must not be able to make the server
    # snapshot-download an arbitrary repo -- only manifest-pinned refs parse.
    with pytest.raises(InvalidModelRefError, match="only pinned artifacts"):
        normalize_artifact_ref("hf-snapshot:owner/repo@0123abc")


def test_hf_snapshot_pinned_repo_wrong_revision_rejected():
    # Same repo as a pinned entry but a different revision is still unpinned.
    from frisket.ai.models.artifact_manifest import PARAKEET_MODEL_HF_REPO

    with pytest.raises(InvalidModelRefError, match="only pinned artifacts"):
        normalize_artifact_ref(f"hf-snapshot:{PARAKEET_MODEL_HF_REPO}@deadbeef")


@pytest.mark.parametrize(
    "raw",
    [
        "hf-snapshot:owner/repo",  # no revision
        "hf-snapshot:not-a-repo@rev",  # missing owner/name split
        "hf-snapshot:owner/repo@rev/extra.onnx",  # a file path is hf:'s shape
        "hf-snapshot:owner/repo@",  # empty revision
        "hf-snapshot:",
    ],
)
def test_hf_snapshot_malformed_rejected(raw):
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref(raw)


def test_pinned_spacy_model_roundtrips():
    from frisket.ai.models.artifact_manifest import SPACY_MODEL_REF

    ref = normalize_artifact_ref(SPACY_MODEL_REF)
    assert ref.scheme == "spacy"
    assert ref.canonical == SPACY_MODEL_REF
    assert ref.spacy_model == "en_core_web_sm"
    assert ref.spacy_version == "3.8.0"


@pytest.mark.parametrize(
    "raw",
    [
        "spacy:en_core_web_sm",
        "spacy:en_core_web_sm@latest",
        "spacy:../model@3.8.0",
        "spacy:other_model@3.8.0",
    ],
)
def test_unpinned_or_malformed_spacy_model_rejected(raw):
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref(raw)


def test_empty_ref_rejected():
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref("   ")


def test_length_cap_enforced():
    too_long = "hf:owner/repo@rev/" + ("a" * MAX_ARTIFACT_REF_LENGTH) + ".gguf"
    with pytest.raises(InvalidModelRefError):
        normalize_artifact_ref(too_long)

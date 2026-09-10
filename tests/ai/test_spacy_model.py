from __future__ import annotations

import hashlib
import zipfile

from frisket.ai.models import spacy_model
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile


def _fake_pin(data: bytes) -> PinnedArtifact:
    return PinnedArtifact(
        ref="spacy:en_core_web_sm@3.8.0",
        kind="spacy_model",
        display_name="spaCy test model",
        manifest_version="test",
        license="MIT",
        license_url="https://example.test/license",
        source_url="https://example.test/release",
        files=(
            PinnedFile(
                spacy_model.WHEEL_FILENAME,
                "https://example.test/model.whl",
                hashlib.sha256(data).hexdigest(),
                len(data),
            ),
        ),
    )


def test_model_state_distinguishes_absent_mismatch_and_present(monkeypatch, tmp_path):
    monkeypatch.setattr(spacy_model.importlib.util, "find_spec", lambda name: None)
    path = spacy_model.wheel_path(cache_root=tmp_path)

    assert spacy_model.model_state(cache_root=tmp_path).status == "not_downloaded"

    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong")
    assert spacy_model.model_state(cache_root=tmp_path).status == "hash_mismatch"

    expected = b"pinned wheel"
    monkeypatch.setattr(spacy_model, "_pinned", lambda: _fake_pin(expected))
    path.write_bytes(expected)
    state = spacy_model.model_state(cache_root=tmp_path)
    assert state.status == "present"
    assert state.source == "managed_cache"


def test_model_state_accepts_manual_package_when_cache_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(spacy_model.importlib.util, "find_spec", lambda name: object())
    state = spacy_model.model_state(cache_root=tmp_path)
    assert state.status == "present"
    assert state.source == "python_package"


def test_verified_wheel_extracts_to_loadable_model_path(monkeypatch, tmp_path):
    path = spacy_model.wheel_path(cache_root=tmp_path)
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as archive:
        prefix = "en_core_web_sm/en_core_web_sm-3.8.0"
        archive.writestr(f"{prefix}/config.cfg", "[nlp]\nlang = 'en'\n")
        archive.writestr(f"{prefix}/meta.json", '{"lang":"en"}')
        archive.writestr("en_core_web_sm/__init__.py", "")
        archive.writestr("en_core_web_sm-3.8.0.dist-info/METADATA", "ignored")
    payload = path.read_bytes()
    monkeypatch.setattr(spacy_model, "_pinned", lambda: _fake_pin(payload))

    model_path = spacy_model.ensure_loadable_model_path(cache_root=tmp_path)

    assert model_path == (
        tmp_path
        / "spacy"
        / "en_core_web_sm"
        / "3.8.0"
        / "runtime"
        / "en_core_web_sm"
        / "en_core_web_sm-3.8.0"
    )
    assert (model_path / "config.cfg").is_file()
    assert (model_path / "meta.json").is_file()
    assert not (model_path.parents[1] / "en_core_web_sm-3.8.0.dist-info").exists()


def test_hash_mismatch_never_extracts(monkeypatch, tmp_path):
    path = spacy_model.wheel_path(cache_root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered")
    monkeypatch.setattr(spacy_model.importlib.util, "find_spec", lambda name: object())

    try:
        spacy_model.ensure_loadable_model_path(cache_root=tmp_path)
    except spacy_model.SpacyModelUnavailable as exc:
        assert "SHA256" in str(exc)
    else:  # pragma: no cover - a tampered artifact must never become loadable
        raise AssertionError("tampered model unexpectedly extracted")

    assert not (path.parent / "runtime").exists()

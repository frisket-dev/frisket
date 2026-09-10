"""Pins for the frisket-owned model cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.ai.models import model_cache


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("FRISKET_MODEL_CACHE_DIR", str(tmp_path / "custom"))
    assert model_cache.default_cache_root() == tmp_path / "custom"


def test_xdg_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("FRISKET_MODEL_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert model_cache.default_cache_root() == tmp_path / "xdg" / "frisket" / "models"


def test_home_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("FRISKET_MODEL_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert (
        model_cache.default_cache_root()
        == tmp_path / "home" / ".cache" / "frisket" / "models"
    )


def test_opus_mt_install_dir(tmp_path):
    ref = normalize_artifact_ref("opus-mt:en-es")
    assert (
        model_cache.artifact_install_dir(ref, root=tmp_path)
        == tmp_path / "opus-mt" / "en-es"
    )


def test_hf_install_path(tmp_path):
    ref = normalize_artifact_ref("hf:owner/repo@rev1/sub/model.gguf")
    assert (
        model_cache.hf_install_path(ref, root=tmp_path)
        == tmp_path / "hf" / "owner" / "repo" / "rev1" / "sub" / "model.gguf"
    )


def test_spacy_install_dir(tmp_path):
    ref = normalize_artifact_ref("spacy:en_core_web_sm@3.8.0")
    assert model_cache.artifact_install_dir(ref, root=tmp_path) == (
        tmp_path / "spacy" / "en_core_web_sm" / "3.8.0"
    )


def test_is_installed_presence_and_size(tmp_path):
    ref = normalize_artifact_ref("opus-mt:en-es")
    install = model_cache.artifact_install_dir(ref, root=tmp_path)
    install.mkdir(parents=True)
    (install / "model.bin").write_bytes(b"abc")
    assert not model_cache.is_installed(
        ref, [("model.bin", 3), ("x.spm", 1)], root=tmp_path
    )
    (install / "x.spm").write_bytes(b"y")
    assert model_cache.is_installed(
        ref, [("model.bin", 3), ("x.spm", 1)], root=tmp_path
    )
    # size mismatch fails closed
    assert not model_cache.is_installed(ref, [("model.bin", 999)], root=tmp_path)


def test_promote_is_atomic_and_discard_leaves_nothing(tmp_path):
    ref = normalize_artifact_ref("opus-mt:en-es")
    staging = model_cache.tmp_dir(7, root=tmp_path)
    staging.mkdir(parents=True)
    (staging / "model.bin").write_bytes(b"data")
    dest = model_cache.promote(7, ref, root=tmp_path)
    assert (dest / "model.bin").read_bytes() == b"data"
    assert not staging.exists()  # moved, not copied

    # a crashed pull's staging is discardable and leaves the install untouched
    staging2 = model_cache.tmp_dir(8, root=tmp_path)
    staging2.mkdir(parents=True)
    (staging2 / "partial.bin").write_bytes(b"...")
    model_cache.discard_tmp(8, root=tmp_path)
    assert not staging2.exists()
    assert (dest / "model.bin").exists()


def test_uninstall_removes_bytes(tmp_path):
    ref = normalize_artifact_ref("opus-mt:en-es")
    install = model_cache.artifact_install_dir(ref, root=tmp_path)
    install.mkdir(parents=True)
    (install / "model.bin").write_bytes(b"x")
    assert model_cache.uninstall(ref, root=tmp_path) is True
    assert not install.exists()
    assert model_cache.uninstall(ref, root=tmp_path) is False


# --- per-artifact scoping and crash-safe promotion -------------------------


def _stage_and_promote(pull_id, ref, files: dict[str, bytes], root):
    staging = model_cache.tmp_dir(pull_id, root=root)
    for relpath, data in files.items():
        target = staging / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return model_cache.promote(pull_id, ref, root=root)


def test_hf_sibling_files_coexist_and_uninstall_is_scoped(tmp_path):
    ref_a = normalize_artifact_ref("hf:owner/repo@rev1/a.gguf")
    ref_b = normalize_artifact_ref("hf:owner/repo@rev1/sub/b.json")
    _stage_and_promote(1, ref_a, {"a.gguf": b"AAA"}, tmp_path)
    _stage_and_promote(2, ref_b, {"sub/b.json": b"BBB"}, tmp_path)

    path_a = model_cache.hf_install_path(ref_a, root=tmp_path)
    path_b = model_cache.hf_install_path(ref_b, root=tmp_path)
    assert path_a.read_bytes() == b"AAA"
    assert path_b.read_bytes() == b"BBB"

    # Uninstalling a: b (a sibling sharing owner/repo/rev) MUST survive.
    assert model_cache.uninstall(ref_a, root=tmp_path) is True
    assert not path_a.exists()
    assert path_b.read_bytes() == b"BBB"


def test_hf_reinstall_atomically_replaces_the_single_file(tmp_path):
    ref = normalize_artifact_ref("hf:owner/repo@rev1/m.gguf")
    _stage_and_promote(1, ref, {"m.gguf": b"v1"}, tmp_path)
    _stage_and_promote(2, ref, {"m.gguf": b"v2-newer"}, tmp_path)
    assert model_cache.hf_install_path(ref, root=tmp_path).read_bytes() == b"v2-newer"


def test_opus_mt_crash_mid_swap_preserves_the_prior_install(tmp_path, monkeypatch):
    ref = normalize_artifact_ref("opus-mt:en-es")
    _stage_and_promote(1, ref, {"model.bin": b"OLD", "source.spm": b"osp"}, tmp_path)

    # Stage a new version, then make the FINAL swap (staged_final -> dest) crash.
    staging = model_cache.tmp_dir(2, root=tmp_path)
    (staging).mkdir(parents=True, exist_ok=True)
    (staging / "model.bin").write_bytes(b"NEW")
    (staging / "source.spm").write_bytes(b"nsp")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        # calls: 1=staging->staged_final, 2=dest->old, 3=staged_final->dest
        if calls["n"] == 3:
            raise OSError("simulated crash during final swap")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        model_cache.promote(2, ref, root=tmp_path)
    monkeypatch.undo()

    # The prior install's bytes must NOT be destroyed -- recoverable at dest or
    # a .old sibling.
    install_dir = model_cache.artifact_install_dir(ref, root=tmp_path)
    candidates = [install_dir, *install_dir.parent.glob(f"{install_dir.name}.old-*")]
    survivors = [c / "model.bin" for c in candidates if (c / "model.bin").is_file()]
    assert survivors, "prior install was destroyed during a mid-swap crash"
    assert any(p.read_bytes() == b"OLD" for p in survivors)


def test_concurrent_hf_sibling_promotes_do_not_collide(tmp_path):
    from tests.deterministic_time import controlled_time

    refs = [normalize_artifact_ref(f"hf:owner/repo@rev1/f{i}.gguf") for i in range(6)]
    datas = {ref.hf_path: f"data-{i}".encode() for i, ref in enumerate(refs)}

    def worker(i, ref):
        _stage_and_promote(100 + i, ref, {ref.hf_path: datas[ref.hf_path]}, tmp_path)

    with controlled_time() as t:
        threads = [
            t.background(lambda i=i, r=r: worker(i, r)) for i, r in enumerate(refs)
        ]
        for thread in threads:
            thread.join()
    for ref in refs:
        assert (
            model_cache.hf_install_path(ref, root=tmp_path).read_bytes()
            == datas[ref.hf_path]
        )

"""Hy-MT2 GGUF runtime, recipe hook, and catalog pins.

The llama.cpp runtime is behind the `translate-gguf` extra and not installed in
the test env, so the in-process call is stubbed; the dispatch, provisioning,
availability semantics, and receipt provenance are the behaviour under test."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from frisket.ops.integrations import hy_mt2
from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.ops.base import OpContext
from frisket.ops.integrations.translation_engine import TranslationEngine


def test_target_language_name_accepts_codes_and_names():
    assert hy_mt2.target_language_name("es") == "Spanish"
    assert hy_mt2.target_language_name("Spanish") == "Spanish"
    assert hy_mt2.target_language_name("Klingon") == "Klingon"  # passthrough
    with pytest.raises(ValueError):
        hy_mt2.target_language_name("")


def _pin_small_gguf(monkeypatch, data: bytes):
    """Repoint the pinned Hy-MT2 entry at a tiny in-test artifact so the
    provisioning path can run without the real 1.1GB download."""
    from frisket.ai.models import artifact_manifest
    from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

    ref = artifact_manifest.HY_MT2_REF
    art = __import__(
        "frisket.engine.jobs.artifact_ref", fromlist=["normalize_artifact_ref"]
    ).normalize_artifact_ref(ref)
    url = "https://frisket.test/hymt2/model.gguf"
    entry = PinnedArtifact(
        ref=ref,
        kind="hf_file",
        display_name="Hy-MT2 1.8B (Q4_K_M)",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF@rev",
        files=(
            PinnedFile(art.hf_path, url, hashlib.sha256(data).hexdigest(), len(data)),
        ),
    )
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {ref: entry})
    return url, entry


def test_ensure_installed_provisions_worker_locally(tmp_path, monkeypatch):
    data = b"fake-gguf-bytes-0123456789"
    url, _ = _pin_small_gguf(monkeypatch, data)

    def handler(request):
        return (
            httpx.Response(200, content=data)
            if str(request.url) == url
            else httpx.Response(404)
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert not hy_mt2.is_installed(cache_root=tmp_path)
    hy_mt2.ensure_installed(cache_root=tmp_path, client=client)
    assert hy_mt2.is_installed(cache_root=tmp_path)


def test_ensure_installed_unpinned_raises(tmp_path, monkeypatch):
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {})
    with pytest.raises(hy_mt2.HyMt2NotInstalled):
        hy_mt2.ensure_installed(cache_root=tmp_path)


def test_cached_gguf_load_does_not_rehash_installed_bytes(tmp_path, monkeypatch):
    import sys
    import types

    from frisket.engine.jobs import artifact_pull

    data = b"fake-gguf-bytes-0123456789"
    _pin_small_gguf(monkeypatch, data)
    path = hy_mt2._model_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)

    def fail_rehash(*args, **kwargs):
        raise AssertionError("installed model bytes were re-hashed during load")

    monkeypatch.setattr(artifact_pull, "_installed_composite_digest", fail_rehash)

    constructed: list[str] = []

    class _FakeLlama:
        def __init__(self, model_path=None, **kwargs):
            constructed.append(model_path)

    monkeypatch.setitem(
        sys.modules, "llama_cpp", types.SimpleNamespace(Llama=_FakeLlama)
    )
    with hy_mt2._CACHE_LOCK:
        hy_mt2._MODEL_CACHE.clear()

    model_1 = hy_mt2._load_model(path)
    model_2 = hy_mt2._load_model(path)
    assert model_1 is model_2
    assert constructed == [str(path)]

    with hy_mt2._CACHE_LOCK:
        hy_mt2._MODEL_CACHE.clear()


def test_ensure_installed_rejects_checksum_mismatch(tmp_path, monkeypatch):
    from frisket.engine.jobs import artifact_pull

    data = b"fake-gguf-bytes-0123456789"
    url, _ = _pin_small_gguf(monkeypatch, data)
    corrupt = b"X" * len(data)

    def handler(request):
        if str(request.url) == url:
            return httpx.Response(200, content=corrupt)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(artifact_pull.ArtifactProvisionError) as exc:
        hy_mt2.ensure_installed(cache_root=tmp_path, client=client)
    assert exc.value.code == "checksum_mismatch"
    assert not hy_mt2.is_installed(cache_root=tmp_path)


@pytest.mark.asyncio
async def test_recipe_hy_mt2_maps_checksum_mismatch_to_model_tampered(monkeypatch):
    # F3: an on-use provisioning checksum failure is TAMPERING (terminal), not
    # "not installed."
    from frisket.engine.jobs import artifact_pull

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)

    def miss(target, texts, cache_root=None):
        raise hy_mt2.HyMt2NotInstalled("miss")

    def bad_checksum(cache_root=None, client=None, should_cancel=None):
        raise artifact_pull.ArtifactProvisionError(
            "checksum mismatch", code="checksum_mismatch"
        )

    monkeypatch.setattr(hy_mt2, "translate_texts", miss)
    monkeypatch.setattr(hy_mt2, "ensure_installed", bad_checksum)

    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as exc:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {"engine": "hy_mt2", "target_language": "French", "output_name": "fr"},
            ctx,
        )
    assert exc.value.code == "model_tampered"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_recipe_hy_mt2_provision_cancel_is_retryable(monkeypatch):
    # F6: provisioning cancel surfaces as a retryable `cancelled` row.
    from frisket.engine.jobs import artifact_pull

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    seen = {}

    def miss(target, texts, cache_root=None):
        raise hy_mt2.HyMt2NotInstalled("miss")

    def cancelled(cache_root=None, client=None, should_cancel=None):
        seen["should_cancel"] = should_cancel
        raise artifact_pull.ProvisionCancelled("cancelled")

    monkeypatch.setattr(hy_mt2, "translate_texts", miss)
    monkeypatch.setattr(hy_mt2, "ensure_installed", cancelled)

    ctx = OpContext(project=None, http=None, extras={"cancelled": lambda: True})
    with pytest.raises(TranslateEngineError) as exc:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {"engine": "hy_mt2", "target_language": "French", "output_name": "fr"},
            ctx,
        )
    assert exc.value.code == "cancelled"
    assert exc.value.retryable is True
    assert callable(seen["should_cancel"])


@pytest.mark.asyncio
async def test_recipe_hy_mt2_runtime_absent_reports_remediation(monkeypatch):
    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: False)
    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as ei:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {"engine": "hy_mt2", "target_language": "Spanish", "output_name": "es"},
            ctx,
        )
    assert ei.value.code == "unavailable"
    assert "frisket[translate-gguf]" in ei.value.message


@pytest.mark.asyncio
async def test_recipe_hy_mt2_translates_and_no_detected_column(monkeypatch):
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    monkeypatch.setattr(
        hy_mt2, "translate_texts", lambda target, texts, cache_root=None: ["Hola"]
    )
    # a real pinned entry so provenance stashes
    ctx = OpContext(project=None, http=None)
    out = await TranslationEngine().execute(
        {"statement": "Hello"},
        {"engine": "hy_mt2", "target_language": "Spanish", "output_name": "es"},
        ctx,
    )
    assert out == {"translation": "Hola"}  # local/free -> plain dict, NO cost meta
    assert "detected_language" not in out  # detects=false -> no detected column
    # provenance stashed from the (real) pinned Hy-MT2 entry
    prov = ctx.extras.get("artifact_provenance")
    assert prov and prov[0]["ref"] == artifact_manifest.HY_MT2_REF
    assert prov[0]["license"] == "Apache-2.0"


@pytest.mark.asyncio
async def test_recipe_hy_mt2_provisions_on_miss_then_translates(monkeypatch):
    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    state = {"installed": False, "ensured": False}

    def fake_translate(target, texts, cache_root=None):
        if not state["installed"]:
            raise hy_mt2.HyMt2NotInstalled("miss")
        return ["Bonjour"]

    def fake_ensure(cache_root=None, client=None, should_cancel=None):
        state["ensured"] = True
        state["installed"] = True

    monkeypatch.setattr(hy_mt2, "translate_texts", fake_translate)
    monkeypatch.setattr(hy_mt2, "ensure_installed", fake_ensure)
    ctx = OpContext(project=None, http=None)
    out = await TranslationEngine().execute(
        {"statement": "Hello"},
        {"engine": "hy_mt2", "target_language": "French", "output_name": "fr"},
        ctx,
    )
    assert out == {"translation": "Bonjour"}
    assert state["ensured"] is True


def test_catalog_hy_mt2_available_tracks_runtime(monkeypatch):
    from frisket.server import action_catalog_hints

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    monkeypatch.setattr(hy_mt2, "is_installed", lambda cache_root=None: False)
    engines = action_catalog_hints._recipe_engines("map.translate", {})
    hy = next(e for e in engines if e["id"] == "hy_mt2")
    assert hy["available"] is True  # runtime present -> selectable
    assert hy.get("error") is None
    assert "experimental" in hy["label"].lower()
    # the downloadable model artifact is surfaced (size, not-installed)
    assert hy["downloadable_model"]["installed"] is False
    assert hy["downloadable_model"]["size"] == 1133080448
    assert hy["downloadable_model"]["license"] == "Apache-2.0"

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: False)
    engines2 = action_catalog_hints._recipe_engines("map.translate", {})
    hy2 = next(e for e in engines2 if e["id"] == "hy_mt2")
    assert hy2["available"] is False
    assert "frisket[translate-gguf]" in hy2["error"]


@pytest.mark.asyncio
async def test_artifact_evidence_covers_executed_hy_mt2(monkeypatch):
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    monkeypatch.setattr(hy_mt2, "translate_texts", lambda *args: ["Hola"])
    ctx = OpContext(project=None)
    await TranslationEngine().execute(
        {"text": "Hello"}, {"engine": "hy_mt2", "target_language": "Spanish"}, ctx
    )
    [ev] = ctx.extras["artifact_provenance"]
    assert ev["ref"] == artifact_manifest.HY_MT2_REF
    assert ev["license"] == "Apache-2.0"

"""Opus-MT local translate runtime, recipe hook, and catalog pins.

The CT2 runtime (ctranslate2 + sentencepiece) is behind the ``translate`` extra
and not installed in the test env, so the in-process call is stubbed; the
dispatch, the explicit-source gate, the not-installed surface, and the receipt
provenance are the behaviour under test."""

from __future__ import annotations

import pytest

from frisket.ops.integrations import opus_mt
from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.ops.base import OpContext
from frisket.ops.integrations.translation_engine import TranslationEngine


def test_resolve_pair_codes_accepts_codes_and_names():
    assert opus_mt.resolve_pair_codes("en", "es") == ("en", "es")
    assert opus_mt.resolve_pair_codes("English", "Spanish") == ("en", "es")
    with pytest.raises(ValueError):
        opus_mt.resolve_pair_codes("en", "klingon")


def test_installed_pairs_scans_cache(tmp_path):
    base = tmp_path / "opus-mt"
    good = base / "en-es"
    good.mkdir(parents=True)
    for name in ("model.bin", "source.spm", "target.spm"):
        (good / name).write_bytes(b"x")
    # a dir missing target.spm is NOT counted installed
    partial = base / "de-en"
    partial.mkdir(parents=True)
    (partial / "model.bin").write_bytes(b"x")
    assert opus_mt.installed_pairs(cache_root=tmp_path) == ["en-es"]
    assert opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)
    assert not opus_mt.is_pair_installed("de", "en", cache_root=tmp_path)


@pytest.mark.asyncio
async def test_recipe_opus_mt_translates_in_process_and_records_provenance(
    monkeypatch,
):
    from frisket.ai.models import artifact_manifest
    from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

    entry = PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://example/cc-by",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2@rev",
        files=(PinnedFile("model.bin", "https://e/model.bin", "a" * 64, 3),),
    )
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

    def fake_translate(src, tgt, texts, *, cache_root=None):
        assert (src, tgt) == ("en", "es")
        return ["Hola Mundo."]

    monkeypatch.setattr(opus_mt, "translate_texts", fake_translate)

    ctx = OpContext(project=None, http=None)
    out = await TranslationEngine().execute(
        {"statement": "Hello World."},
        {
            "engine": "opus_mt",
            "target_language": "Spanish",
            "language": ["en"],
            "output_name": "es",
        },
        ctx,
    )
    # The translation contract (supersede this test's original always-on pin):
    # detected-language is OPT-IN via save_detected_language and rides the
    # derived {output}_detected_language name; a default run emits only the
    # translation column.
    assert out == {"translation": "Hola Mundo."}
    prov = ctx.extras["artifact_provenance"]
    assert prov[0]["ref"] == "opus-mt:en-es"
    assert prov[0]["license"] == "CC-BY-4.0"
    assert prov[0]["composite_digest"] == entry.composite_digest


@pytest.mark.asyncio
async def test_recipe_opus_mt_empty_source_rejected(monkeypatch):
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as excinfo:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {"engine": "opus_mt", "target_language": "Spanish", "language": []},
            ctx,
        )
    assert excinfo.value.code == "invalid_source"


@pytest.mark.asyncio
async def test_recipe_opus_mt_uninstalled_pair_does_not_silently_fall_back(
    monkeypatch,
):
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

    def not_installed(src, tgt, texts, *, cache_root=None):
        raise opus_mt.OpusPairNotInstalled("no pair")

    def cannot_provision(src, tgt, *, should_cancel=None):
        raise LookupError("pair is not in the provisionable manifest")

    monkeypatch.setattr(opus_mt, "translate_texts", not_installed)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", cannot_provision)
    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as excinfo:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "opus_mt",
                "target_language": "Spanish",
                "language": ["en"],
            },
            ctx,
        )
    assert excinfo.value.code == "pair_unavailable"


@pytest.mark.asyncio
async def test_recipe_opus_mt_runtime_absent_reports_remediation(monkeypatch):
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: False)
    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as excinfo:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "opus_mt",
                "target_language": "Spanish",
                "language": ["en"],
            },
            ctx,
        )
    assert excinfo.value.code == "unavailable"
    assert "frisket[standard]" in excinfo.value.message


def test_translate_catalog_reports_installed_and_downloadable_pairs(monkeypatch):
    from frisket.ai.models import artifact_manifest
    from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile
    from frisket.server import action_catalog_hints

    entry = PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://example/cc-by",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2@rev",
        files=(PinnedFile("model.bin", "https://e/model.bin", "a" * 64, 298),),
    )
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})
    monkeypatch.setattr(opus_mt, "installed_pairs", lambda cache_root=None: [])
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

    engines = action_catalog_hints._recipe_engines("map.translate", {})
    opus = next(e for e in engines if e["id"] == "opus_mt")
    # A present runtime makes the engine selectable even with no
    # pair installed; the missing pair is a run-gating state, not engine
    # unavailability, so the inline pair picker can render.
    assert opus["available"] is True
    assert opus["downloadable_pairs"] == [
        {
            "pair": "en-es",
            "display_name": "English → Spanish",
            "size": 298,
            "license": "CC-BY-4.0",
        }
    ]


def test_translator_cache_reloads_when_weights_are_replaced(tmp_path, monkeypatch):
    """The CT2 translator cache keys by (directory, ``model.bin`` mtime), so a
    re-pull that atomically replaces the weights forces a fresh load instead of
    serving the OLD translator while provenance claims the new bytes."""
    import sys
    import types

    directory = tmp_path / "opus-mt" / "en-es"
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(b"v1")

    constructed: list[str] = []

    class _FakeTranslator:
        def __init__(self, path, device=None, compute_type=None):
            constructed.append(path)

    fake_ct2 = types.SimpleNamespace(Translator=_FakeTranslator)
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct2)
    opus_mt._clear_translator_cache()

    t1 = opus_mt._load_translator(directory)
    t2 = opus_mt._load_translator(directory)
    assert t1 is t2  # same mtime -> cached, one construction
    assert len(constructed) == 1

    # Simulate a fresh pull replacing the weights (new mtime).
    import os

    (directory / "model.bin").write_bytes(b"v2-newer")
    new_ns = (directory / "model.bin").stat().st_mtime_ns + 1
    os.utime(directory / "model.bin", ns=(new_ns, new_ns))

    t3 = opus_mt._load_translator(directory)
    assert t3 is not t1  # mtime changed -> reloaded
    assert len(constructed) == 2
    opus_mt._clear_translator_cache()


# --- Lazy pull on first use after a runtime miss ----------------------------


def _pinned_pair_entry(files: dict[str, bytes]):
    import hashlib as _hl

    from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

    pinned = tuple(
        PinnedFile(
            name,
            f"https://frisket.test/en-es/{name}",
            _hl.sha256(d).hexdigest(),
            len(d),
        )
        for name, d in files.items()
    )
    return PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://e/cc-by",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2@rev",
        files=pinned,
    )


def test_ensure_pair_installed_provisions_worker_locally(tmp_path, monkeypatch):
    import httpx

    from frisket.ai.models import artifact_manifest

    files = {"model.bin": b"wa", "source.spm": b"sa", "target.spm": b"ta"}
    entry = _pinned_pair_entry(files)
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})

    bodies = {f"https://frisket.test/en-es/{n}": d for n, d in files.items()}

    def handler(request):
        url = str(request.url)
        return (
            httpx.Response(200, content=bodies[url])
            if url in bodies
            else httpx.Response(404)
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert not opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)
    opus_mt.ensure_pair_installed("en", "es", cache_root=tmp_path, client=client)
    assert opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)


def test_ensure_pair_installed_unpinned_pair_raises(tmp_path, monkeypatch):
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {})
    with pytest.raises(opus_mt.OpusPairNotInstalled):
        opus_mt.ensure_pair_installed("en", "es", cache_root=tmp_path)


def test_ensure_pair_installed_no_double_pull_under_concurrency(tmp_path, monkeypatch):
    import threading

    import httpx

    from frisket.ai.models import artifact_manifest
    from tests.deterministic_time import controlled_time

    files = {"model.bin": b"wa", "source.spm": b"sa", "target.spm": b"ta"}
    entry = _pinned_pair_entry(files)
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})

    downloads = {"n": 0}
    dl_lock = threading.Lock()
    bodies = {f"https://frisket.test/en-es/{n}": d for n, d in files.items()}

    def handler(request):
        url = str(request.url)
        if url in bodies:
            with dl_lock:
                downloads["n"] += 1
            return httpx.Response(200, content=bodies[url])
        return httpx.Response(404)

    # a shared client (MockTransport is thread-safe for our purposes)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    # reset the per-pair provision lock registry so this test is isolated
    opus_mt._PROVISION_LOCKS.clear()

    def worker():
        opus_mt.ensure_pair_installed("en", "es", cache_root=tmp_path, client=client)

    with controlled_time() as t:
        threads = [t.background(worker) for _ in range(8)]
        for thread in threads:
            thread.join()

    assert opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)
    # exactly one provision -> 3 file downloads (not 8 * 3)
    assert downloads["n"] == len(files)


@pytest.mark.asyncio
async def test_recipe_opus_mt_provisions_on_miss_then_translates(monkeypatch):
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    state = {"installed": False, "ensured": False}

    def fake_translate(src, tgt, texts, *, cache_root=None):
        if not state["installed"]:
            raise opus_mt.OpusPairNotInstalled("miss")
        return ["Hola"]

    def fake_ensure(src, tgt, *, cache_root=None, client=None, should_cancel=None):
        state["ensured"] = True
        state["installed"] = True

    monkeypatch.setattr(opus_mt, "translate_texts", fake_translate)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", fake_ensure)

    ctx = OpContext(project=None, http=None)
    out = await TranslationEngine().execute(
        {"statement": "Hello"},
        {
            "engine": "opus_mt",
            "target_language": "Spanish",
            "language": ["en"],
            "output_name": "es",
        },
        ctx,
    )
    assert out["translation"] == "Hola"
    assert state["ensured"] is True


def test_cached_pair_load_does_not_rehash_installed_bytes(tmp_path, monkeypatch):
    import sys
    import types

    from frisket.engine.jobs import artifact_pull
    from frisket.ai.models import artifact_manifest

    files = {"model.bin": b"wa", "source.spm": b"sa", "target.spm": b"ta"}
    entry = _pinned_pair_entry(files)
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})

    directory = opus_mt._pair_dir("en", "es", tmp_path)
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)

    def fail_rehash(*args, **kwargs):
        raise AssertionError("installed model bytes were re-hashed during load")

    monkeypatch.setattr(artifact_pull, "_installed_composite_digest", fail_rehash)

    constructed: list[str] = []

    class _FakeTranslator:
        def __init__(self, path, device=None, compute_type=None):
            constructed.append(path)

        def translate_batch(self, batch, beam_size=None):
            assert batch == [["hello", "</s>"]]
            return [types.SimpleNamespace(hypotheses=[["hola", "</s>"]])]

    class _FakeSentencePiece:
        def __init__(self, model_file=None):
            self.model_file = model_file

        def encode(self, text, out_type=None):
            assert text == "Hello"
            assert out_type is str
            return ["hello"]

        def decode(self, pieces):
            assert pieces == ["hola"]
            return "Hola"

    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        types.SimpleNamespace(Translator=_FakeTranslator),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentencepiece",
        types.SimpleNamespace(SentencePieceProcessor=_FakeSentencePiece),
    )
    opus_mt._clear_translator_cache()

    assert opus_mt.translate_texts("en", "es", ["Hello"], cache_root=tmp_path) == [
        "Hola"
    ]
    assert opus_mt.translate_texts("en", "es", ["Hello"], cache_root=tmp_path) == [
        "Hola"
    ]
    assert len(constructed) == 1
    opus_mt._clear_translator_cache()


def test_ensure_pair_installed_rejects_checksum_mismatch(tmp_path, monkeypatch):
    import httpx

    from frisket.engine.jobs import artifact_pull
    from frisket.ai.models import artifact_manifest

    files = {"model.bin": b"wa", "source.spm": b"sa", "target.spm": b"ta"}
    entry = _pinned_pair_entry(files)
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})
    bodies = {f"https://frisket.test/en-es/{n}": d for n, d in files.items()}
    bodies["https://frisket.test/en-es/model.bin"] = b"XX"

    def handler(request):
        url = str(request.url)
        return (
            httpx.Response(200, content=bodies[url])
            if url in bodies
            else httpx.Response(404)
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(artifact_pull.ArtifactProvisionError) as exc:
        opus_mt.ensure_pair_installed("en", "es", cache_root=tmp_path, client=client)
    assert exc.value.code == "checksum_mismatch"
    assert not opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)


@pytest.mark.asyncio
async def test_recipe_maps_checksum_mismatch_to_pair_tampered(monkeypatch):
    # F3: an on-use provisioning CHECKSUM failure is TAMPERING (terminal), not
    # "not installed" -- never direct the user to reinstall the bytes that just
    # failed verification. A transport miss stays retryable pair_unreachable.
    from frisket.engine.jobs import artifact_pull

    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

    def miss(src, tgt, texts, *, cache_root=None):
        raise opus_mt.OpusPairNotInstalled("miss")

    def bad_checksum(src, tgt, *, cache_root=None, client=None, should_cancel=None):
        raise artifact_pull.ArtifactProvisionError(
            "checksum mismatch for model.bin", code="checksum_mismatch"
        )

    monkeypatch.setattr(opus_mt, "translate_texts", miss)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", bad_checksum)

    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as exc:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "opus_mt",
                "target_language": "Spanish",
                "language": ["en"],
                "output_name": "es",
            },
            ctx,
        )
    assert exc.value.code == "pair_tampered"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_recipe_maps_transport_failure_to_pair_unreachable(monkeypatch):
    from frisket.engine.jobs import artifact_pull

    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

    def miss(src, tgt, texts, *, cache_root=None):
        raise opus_mt.OpusPairNotInstalled("miss")

    def unreachable(src, tgt, *, cache_root=None, client=None, should_cancel=None):
        raise artifact_pull.ArtifactProvisionError(
            "could not reach the artifact source (ConnectError)", code="transport"
        )

    monkeypatch.setattr(opus_mt, "translate_texts", miss)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", unreachable)

    ctx = OpContext(project=None, http=None)
    with pytest.raises(TranslateEngineError) as exc:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "opus_mt",
                "target_language": "Spanish",
                "language": ["en"],
                "output_name": "es",
            },
            ctx,
        )
    assert exc.value.code == "pair_unreachable"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_recipe_provision_cancel_is_a_retryable_cancelled_row(monkeypatch):
    # F6: the cooperative cancel threaded into provisioning surfaces as a
    # retryable `cancelled` row so backfill re-provisions + re-runs it.
    from frisket.engine.jobs import artifact_pull

    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    seen = {}

    def miss(src, tgt, texts, *, cache_root=None):
        raise opus_mt.OpusPairNotInstalled("miss")

    def cancelled(src, tgt, *, cache_root=None, client=None, should_cancel=None):
        seen["should_cancel"] = should_cancel
        raise artifact_pull.ProvisionCancelled("cancelled mid-download")

    monkeypatch.setattr(opus_mt, "translate_texts", miss)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", cancelled)

    ctx = OpContext(project=None, http=None, extras={"cancelled": lambda: True})
    with pytest.raises(TranslateEngineError) as exc:
        await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "opus_mt",
                "target_language": "Spanish",
                "language": ["en"],
                "output_name": "es",
            },
            ctx,
        )
    assert exc.value.code == "cancelled"
    assert exc.value.retryable is True
    # The cancel callable was threaded through to provisioning (F6).
    assert callable(seen["should_cancel"])


def test_provision_pinned_honors_cancel_signal(tmp_path, monkeypatch):
    # F6 at the source: provision_pinned polls should_cancel between chunks and
    # raises ProvisionCancelled (discarding the partial), instead of streaming a
    # ~1.1GB artifact to completion while the run is trying to cancel.
    import httpx

    from frisket.engine.jobs import artifact_pull
    from frisket.ai.models import artifact_manifest

    files = {"model.bin": b"wa", "source.spm": b"sa", "target.spm": b"ta"}
    entry = _pinned_pair_entry(files)
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})
    bodies = {f"https://frisket.test/en-es/{n}": d for n, d in files.items()}

    def handler(request):
        url = str(request.url)
        return (
            httpx.Response(200, content=bodies[url])
            if url in bodies
            else httpx.Response(404)
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from frisket.engine.jobs.artifact_ref import normalize_artifact_ref

    art = normalize_artifact_ref("opus-mt:en-es")
    with pytest.raises(artifact_pull.ProvisionCancelled):
        artifact_pull.provision_pinned(
            art, entry, cache_root=tmp_path, client=client, should_cancel=lambda: True
        )
    # Nothing was promoted -> the pair is not installed after a cancel.
    assert not opus_mt.is_pair_installed("en", "es", cache_root=tmp_path)


# --- opus_mt receipts record pinned artifact provenance --------------------


def _pin_manifest(monkeypatch):
    from frisket.ai.models import artifact_manifest
    from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

    entry = PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://example/cc-by",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2@rev",
        files=(PinnedFile("model.bin", "https://e/model.bin", "a" * 64, 3),),
    )
    monkeypatch.setattr(artifact_manifest, "_MANIFEST", {"opus-mt:en-es": entry})
    return entry


@pytest.mark.asyncio
async def test_artifact_evidence_carries_the_executed_pinned_tuple(monkeypatch):
    entry = _pin_manifest(monkeypatch)
    rs = {"engine": "opus_mt", "target_language": "Spanish", "language": ["en"]}
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    monkeypatch.setattr(opus_mt, "translate_texts", lambda *args: ["Hola"])
    ctx = OpContext(project=None)
    await TranslationEngine().execute({"text": "Hello"}, rs, ctx)
    [ev] = ctx.extras["artifact_provenance"]
    assert ev["ref"] == "opus-mt:en-es"
    assert ev["license"] == "CC-BY-4.0"
    assert ev["composite_digest"] == entry.composite_digest
    assert ev["source_url"] == entry.source_url


@pytest.mark.asyncio
async def test_artifact_evidence_is_absent_without_execution_or_pin(monkeypatch):
    _pin_manifest(monkeypatch)
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    monkeypatch.setattr(opus_mt, "translate_texts", lambda *args: ["Hallo"])
    for text, target in (("", "Spanish"), ("Hello", "German")):
        ctx = OpContext(project=None)
        await TranslationEngine().execute(
            {"text": text},
            {"engine": "opus_mt", "target_language": target, "language": ["en"]},
            ctx,
        )
        assert not ctx.extras.get("artifact_provenance")


@pytest.mark.asyncio
async def test_admitted_translation_carries_artifact_evidence_by_value(monkeypatch):
    from types import SimpleNamespace
    from frisket.actions.types import Row
    from frisket.actions.translate_types import TranslationOptions
    from frisket.engine.executor import translate_read
    from frisket.engine.executor.translate_read import AdmittedTranslator
    from frisket.execution.resolve_for_action import authored_options

    _pin_manifest(monkeypatch)
    options = TranslationOptions(target_language="Spanish", language=["en"])
    route = SimpleNamespace(
        engine="opus_mt",
        target_snapshot={"capability": "translate", "transport": "local"},
        options=authored_options(options.normalize("opus_mt"), "translate"),
    )
    monkeypatch.setattr(
        translate_read,
        "routed_admission_in_scope",
        lambda extras: SimpleNamespace(route=route),
    )
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    monkeypatch.setattr(opus_mt, "translate_texts", lambda *args: ["Hola"])
    ctx = OpContext(project=None)
    owner = AdmittedTranslator(ctx, engine="opus_mt", options=options.model_dump())
    row = Row({"text": "Hello"})
    bound = owner.bind_row(row, sheet_id=1, row_id=1, sources={}, ctx=ctx)
    await bound.translate(row, "Hello", options=options)
    [call] = owner.calls_by_row[1]
    assert call["artifacts"][0]["ref"] == "opus-mt:en-es"
    ctx.extras["artifact_provenance"][0]["license"] = "mutated"
    assert call["artifacts"][0]["license"] == "CC-BY-4.0"

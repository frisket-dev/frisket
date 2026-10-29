"""Optional translation keeps package setup, weights and inference separate."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest

from frisket.engine._workers import opus_mt_session, opus_mt_worker
from frisket.ops.integrations import opus_mt
from frisket.ops.integrations.translation_engine import TranslationEngine
from frisket.runtime import translation_install


def test_runtime_probe_does_not_import_native_libraries(monkeypatch):
    seen = []
    monkeypatch.setattr(
        translation_install, "find_spec", lambda name: seen.append(name) or None
    )
    for name in ("ctranslate2", "sentencepiece"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(translation_install, "is_installed", lambda: True)
    assert opus_mt.runtime_available()
    assert seen == ["ctranslate2"]
    assert "ctranslate2" not in sys.modules
    assert "sentencepiece" not in sys.modules


def test_install_profile_has_own_hashed_requirements(monkeypatch):
    captured = []
    monkeypatch.setattr(
        translation_install.model_install,
        "_install_profile",
        lambda profile, **kwargs: captured.append(profile),
    )
    translation_install.install_translation(
        should_cancel=lambda: False, progress=lambda _: None
    )
    [profile] = captured
    assert profile.root.name == "translation-v1"
    assert profile.requirements_file == "translation-requirements.txt"
    assert profile.require_hashes
    assert profile.extra is None


@pytest.mark.asyncio
async def test_invocation_reuses_managed_worker_and_closes(monkeypatch):
    instances = []

    class Session:
        def __init__(self, **kwargs):
            self.calls, self.closed = [], False
            instances.append(self)

        async def translate(self, src, tgt, texts):
            self.calls.append(texts)
            return ["Hola"]

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(translation_install, "host_present", lambda: False)
    monkeypatch.setattr(translation_install, "is_installed", lambda: True)
    monkeypatch.setattr(opus_mt_session, "OpusMtSession", Session)
    engine = TranslationEngine(invocation_owned=True)
    assert await engine._opus_translate("en", "es", ["Hello"], None) == ["Hola"]
    assert await engine._opus_translate("en", "es", ["World"], None) == ["Hola"]
    assert len(instances) == 1
    assert instances[0].calls == [["Hello"], ["World"]]
    await engine.aclose()
    assert instances[0].closed


@pytest.mark.asyncio
async def test_direct_engine_call_closes_temporary_session_on_error(monkeypatch):
    closed = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def translate(self, *args):
            raise opus_mt.OpusPairNotInstalled("missing")

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(translation_install, "host_present", lambda: False)
    monkeypatch.setattr(translation_install, "is_installed", lambda: True)
    monkeypatch.setattr(opus_mt_session, "OpusMtSession", Session)
    with pytest.raises(opus_mt.OpusPairNotInstalled):
        await TranslationEngine()._opus_translate("en", "es", ["Hello"], None)
    assert closed == [True]


@pytest.mark.asyncio
async def test_session_reuses_process_and_grants_only_pair(monkeypatch, tmp_path):
    from frisket.ai.models import model_cache

    directory = tmp_path / "opus-mt" / "en-es"
    directory.mkdir(parents=True)
    for name in opus_mt._CT2_REQUIRED_FILES:
        (directory / name).write_bytes(b"model")
    monkeypatch.setattr(model_cache, "default_cache_root", lambda: tmp_path)
    opened, requests, closed = [], [], []

    class Handle:
        async def exchange_frame(self, payload, **kwargs):
            request = json.loads(payload)
            requests.append(request)
            return json.dumps(
                {"type": "ready"}
                if request["type"] == "init"
                else {"type": "result", "texts": ["Hola"]}
            ).encode()

        async def close(self, payload, **kwargs):
            closed.append(json.loads(payload))

        async def abort(self):
            closed.append("abort")

    async def open_process(argv, **kwargs):
        opened.append(kwargs)
        return Handle()

    monkeypatch.setattr(
        opus_mt_session, "worker_argv", lambda *args, **kwargs: ["owned-worker"]
    )
    monkeypatch.setattr(opus_mt_session, "runtime_for_worker", lambda: None)
    monkeypatch.setattr(opus_mt_session.shim, "open_sandboxed_process", open_process)
    session = opus_mt_session.OpusMtSession()
    for _ in range(2):
        assert await session.translate("en", "es", ["Hello"]) == ["Hola"]
    assert len(opened) == 1
    assert requests[0] == {"type": "init", "directory": str(directory)}
    assert opened[0]["policy"].confine.read[0] == str(directory)
    assert not opened[0]["policy"].allow_network
    await session.aclose()
    assert closed == [{"type": "close"}]


@pytest.mark.asyncio
async def test_session_cancel_does_not_start_or_download(monkeypatch):
    session = opus_mt_session.OpusMtSession(cancelled=lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await session.translate("en", "es", ["Hello"])
    assert session._handle is None
    assert session._closed


def test_worker_uses_framing_and_keeps_model_across_rows(monkeypatch, tmp_path):
    from frisket.engine._workers import opus_mt_inference

    directory = tmp_path / "en-es"
    directory.mkdir()
    for name in opus_mt._CT2_REQUIRED_FILES:
        (directory / name).write_bytes(b"model")
    stream = io.BytesIO()
    for request in (
        {"type": "init", "directory": str(directory)},
        {"type": "translate", "texts": ["Hello"]},
        {"type": "translate", "texts": ["World"]},
        {"type": "close"},
    ):
        opus_mt_worker.CODEC.write_frame(stream, request)
    stream.seek(0)
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    loaded, models = [], []

    def load(path, **kwargs):
        model = object()
        loaded.append((path, kwargs))
        return model

    def translate(path, texts, translator):
        models.append(translator)
        return ["Hola"]

    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(Translator=load))
    monkeypatch.setattr(opus_mt_inference, "translate_directory", translate)
    assert opus_mt_worker.framed_main() == 0
    assert len(loaded) == 1
    assert models[0] is models[1]
    output.seek(0)
    assert opus_mt_worker.CODEC.read_frame(output) == {"type": "ready"}
    assert opus_mt_worker.CODEC.read_frame(output) == {
        "type": "result",
        "texts": ["Hola"],
    }
    assert opus_mt_worker.CODEC.read_frame(output) == {
        "type": "result",
        "texts": ["Hola"],
    }


@pytest.mark.asyncio
async def test_managed_first_use_download_preserves_provenance(monkeypatch):
    from frisket.ai.models import artifact_manifest
    from frisket.ops.base import OpContext

    instances, ensured = [], []

    class Session:
        def __init__(self, **kwargs):
            self.calls = 0
            instances.append(self)

        async def translate(self, src, tgt, texts):
            self.calls += 1
            if not ensured:
                raise opus_mt.OpusPairNotInstalled("missing")
            return ["Hola"]

        async def aclose(self):
            pass

    def ensure(src, tgt, *, should_cancel):
        ensured.append((src, tgt))

    monkeypatch.setattr(translation_install, "host_present", lambda: False)
    monkeypatch.setattr(translation_install, "is_installed", lambda: True)
    monkeypatch.setattr(opus_mt_session, "OpusMtSession", Session)
    monkeypatch.setattr(opus_mt, "ensure_pair_installed", ensure)
    engine = TranslationEngine(invocation_owned=True)
    ctx = OpContext(project=None)
    result = await engine.execute(
        {"text": "Hello"},
        {"engine": "opus_mt", "target_language": "Spanish", "language": ["en"]},
        ctx,
    )
    assert result == {"translation": "Hola"}
    assert ensured == [("en", "es")]
    assert len(instances) == 1
    assert instances[0].calls == 2
    assert ctx.extras["artifact_provenance"][0]["ref"] == "opus-mt:en-es"
    assert (
        ctx.extras["artifact_provenance"][0]["composite_digest"]
        == artifact_manifest.lookup("opus-mt:en-es").composite_digest
    )
    await engine.aclose()


@pytest.mark.asyncio
async def test_invalid_worker_result_aborts_session(monkeypatch, tmp_path):
    from frisket.ai.models import model_cache
    from frisket.ops.integrations.translate_common import TranslateEngineError

    monkeypatch.setattr(model_cache, "default_cache_root", lambda: tmp_path)
    monkeypatch.setattr(opus_mt, "is_pair_installed", lambda *args: True)
    aborted = []

    class Handle:
        async def exchange_frame(self, *args, **kwargs):
            return b'{"type":"result","texts":[true]}'

        async def abort(self):
            aborted.append(True)

    session = opus_mt_session.OpusMtSession()
    session._handle = Handle()
    session._pair = ("en", "es")
    with pytest.raises(TranslateEngineError, match="stopped unexpectedly"):
        await session.translate("en", "es", ["Hello"])
    assert aborted == [True]
    assert session._closed


@pytest.mark.asyncio
async def test_admitted_translator_owns_engine_cleanup(monkeypatch):
    from frisket.engine.executor.translate_read import AdmittedTranslator
    from frisket.ops.base import OpContext

    owner = AdmittedTranslator(
        OpContext(project=None),
        engine="opus_mt",
        options={"target_language": "Spanish", "language": ["en"]},
    )
    closed = []

    async def close():
        closed.append(True)

    monkeypatch.setattr(owner._translation_engine, "aclose", close)
    await owner.aclose()
    assert owner._closed
    assert closed == [True]


@pytest.mark.asyncio
async def test_oversized_row_does_not_discard_usable_session(monkeypatch, tmp_path):
    from frisket.ai.models import model_cache
    from frisket.ops.integrations.translate_common import TranslateEngineError

    monkeypatch.setattr(model_cache, "default_cache_root", lambda: tmp_path)
    monkeypatch.setattr(opus_mt, "is_pair_installed", lambda *args: True)
    monkeypatch.setattr(opus_mt_session, "MAX_FRAME_BYTES", 64)
    sent = []

    class Handle:
        async def exchange_frame(self, payload, **kwargs):
            sent.append(json.loads(payload))
            return b'{"type":"result","texts":["Hola"]}'

    session = opus_mt_session.OpusMtSession()
    session._handle = Handle()
    session._pair = ("en", "es")
    with pytest.raises(TranslateEngineError) as error:
        await session.translate("en", "es", ["x" * 128])
    assert error.value.code == "invalid_input"
    assert not session._closed
    assert await session.translate("en", "es", ["Hello"]) == ["Hola"]
    assert sent == [{"type": "translate", "texts": ["Hello"]}]

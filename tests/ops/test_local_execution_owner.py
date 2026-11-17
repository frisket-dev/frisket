"""Local backend batches cross the same admission/outcome owner as remote work."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from frisket.execution.dispatch_control import STANDALONE
from frisket.execution.purchases import PurchaseAdmission
from frisket.ops.base import OpContext


class Policy:
    def __init__(self, *, refuse=False):
        self.events = []
        self.refuse = refuse

    def admit_purchase(self, request):
        self.events.append(("admit", request))
        assert request.provider == "local"
        assert request.credential_source == "none"
        if self.refuse:
            raise RuntimeError("local execution refused")
        return PurchaseAdmission("local")

    def record_purchase(self, request, admission, observation):
        assert admission.disposition == "local"
        assert observation.provider_cost_micro == 0
        self.events.append(("record", observation))

    def fail_purchase(self, request, admission, error):
        self.events.append(("fail", error))


def context(policy):
    return OpContext(
        extras={
            "standalone_dispatch": STANDALONE,
            "service_operation_policy": policy,
        }
    )


@pytest.mark.parametrize("kind", ["text", "image", "rerank"])
@pytest.mark.parametrize("refuse", [False, True])
def test_fastembed_actual_batches_obey_owner_and_empty_batches_do_not_dispatch(
    monkeypatch, kind, refuse
):
    from frisket.ai.embeddings import backends

    policy = Policy(refuse=refuse)

    class Model:
        def __init__(self, *_args, **_kwargs):
            pass

        def embed(self, inputs):
            policy.events.append(("backend", inputs))
            return [SimpleNamespace(tolist=lambda: [1.0, 0.0]) for _ in inputs]

        def rerank(self, query, documents):
            policy.events.append(("backend", documents))
            return [0.5 for _ in documents]

    fake = ModuleType("fastembed")
    fake.TextEmbedding = fake.ImageEmbedding = Model
    cross_encoder = ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = Model
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)
    monkeypatch.setattr(backends, "_local_models", {})
    monkeypatch.setattr(backends, "_local_image_models", {})
    monkeypatch.setattr(backends, "_local_rerank_model", None)
    monkeypatch.setattr(backends, "LOCAL_PURCHASE_POLICY", policy)
    if kind == "rerank":
        score = backends.local_reranker(env={})

        def invoke(values):
            return score("query", values)
    else:
        factory = (
            backends.local_embedder if kind == "text" else backends.local_image_embedder
        )
        invoke, _ = factory(env={})
    assert invoke([]) == []
    assert policy.events == []
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            invoke(["one", "two"])
        assert [event for event, _ in policy.events] == ["admit"]
    else:
        assert len(invoke(["one", "two"])) == 2
        assert [event for event, _ in policy.events] == ["admit", "backend", "record"]
        assert policy.events[0][1].quantity == {"input_count": 2}
        assert policy.events[-1][1].usage == {"input_count": 2}


@pytest.mark.parametrize("refuse", [False, True])
def test_embedding_runtime_owns_fresh_encoder_request_on_its_loop(monkeypatch, refuse):
    from frisket.ai.embeddings import runtime as module
    from frisket.retrieval.encoding import (
        EmbeddingGemma2Space,
        EncodingPurpose,
        TextEncodingInput,
    )

    policy = Policy(refuse=refuse)
    monkeypatch.setattr(module, "LOCAL_PURCHASE_POLICY", policy)

    class Session:
        async def encode(self, inputs, *, purpose, space):
            policy.events.append(("backend", len(inputs)))
            return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]

        async def aclose(self):
            pass

    runtime = module.EmbeddingRuntime(lambda **_kwargs: Session())
    try:
        assert (
            runtime.encode(
                [], purpose=EncodingPurpose.DOCUMENT, space=EmbeddingGemma2Space(128)
            )
            == []
        )
        assert policy.events == []
        if refuse:
            with pytest.raises(RuntimeError, match="local execution refused"):
                runtime.encode(
                    [TextEncodingInput("fresh")],
                    purpose=EncodingPurpose.DOCUMENT,
                    space=EmbeddingGemma2Space(128),
                )
            assert [kind for kind, _ in policy.events] == ["admit"]
        else:
            assert (
                len(
                    runtime.encode(
                        [TextEncodingInput("fresh")],
                        purpose=EncodingPurpose.DOCUMENT,
                        space=EmbeddingGemma2Space(128),
                    )
                )
                == 1
            )
            assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
    finally:
        runtime.close()


def test_representation_cache_hits_do_not_reenter_the_local_execution_owner(
    monkeypatch,
):
    from frisket.ai.embeddings import runtime as module
    from frisket.ai.embeddings.facade import EmbeddingFacade
    from frisket.retrieval.encoding import (
        EmbeddingGemma2Space,
        EncodingPurpose,
        TextEncodingInput,
    )

    policy = Policy()
    monkeypatch.setattr(module, "LOCAL_PURCHASE_POLICY", policy)

    class Cache:
        def __init__(self):
            self.rows = {}

        def get_representations(self, space_id, input_hashes):
            return {
                key: self.rows[(space_id, key)]
                for key in input_hashes
                if (space_id, key) in self.rows
            }

        def put_representations(self, values):
            for value in values:
                self.rows[(value.space_id, value.input_hash)] = value

    class Session:
        async def encode(self, inputs, *, purpose, space):
            policy.events.append(("backend", len(inputs)))
            return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]

        async def aclose(self):
            pass

    runtime = module.EmbeddingRuntime(lambda **_kwargs: Session())
    facade = EmbeddingFacade(Cache())
    values = [TextEncodingInput("same fresh input")] * 2
    try:
        first = facade.encode(
            values,
            encoder=runtime.encode,
            purpose=EncodingPurpose.DOCUMENT,
            space=EmbeddingGemma2Space(128),
        )
        second = facade.encode(
            values,
            encoder=runtime.encode,
            purpose=EncodingPurpose.DOCUMENT,
            space=EmbeddingGemma2Space(128),
        )
        assert first == second and len(second) == 2
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
        assert policy.events[-1][1].usage == {"input_count": 1}
    finally:
        runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,method",
    [
        ("rapidocr", "_ocr_rapidocr"),
        ("tesseract", "_ocr_tesseract"),
        ("indra-ocr", "_ocr_indra"),
    ],
)
@pytest.mark.parametrize("refuse", [False, True])
async def test_local_ocr_batch_cannot_bypass_admission(
    monkeypatch, tmp_path, engine, method, refuse
):
    from frisket.ops.ocr_engines import OcrEngines

    policy = Policy(refuse=refuse)
    backend = OcrEngines()

    async def invoke(*_args, **_kwargs):
        policy.events.append(("backend", engine))
        return [{"text": "one", "blocks": []}, {"text": "two", "blocks": []}]

    monkeypatch.setattr(backend, method, invoke)
    call = backend.run_engine_on_pages(
        engine,
        [tmp_path / "one.png", tmp_path / "two.png"],
        context(policy),
        usage={},
        scratch=tmp_path,
    )
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            await call
        assert [kind for kind, _ in policy.events] == ["admit"]
    else:
        assert len(await call) == 2
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
        assert policy.events[-1][1].usage == {"pages": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True])
async def test_indra_pdf_entry_uses_its_context_owner_once(
    monkeypatch, tmp_path, refuse
):
    from frisket.ops.ocr_engines import OcrEngines, local

    policy = Policy(refuse=refuse)

    async def invoke(*_args, **_kwargs):
        policy.events.append(("backend", "pdf"))
        return [{"text": "recognized", "blocks": []}]

    monkeypatch.setattr(local, "ocr_indra", invoke)
    call = OcrEngines()._ocr_indra_pdf(
        tmp_path / "scan.pdf",
        tmp_path,
        dpi=200,
        page_count=1,
        report_progress=lambda *_: None,
        ctx=context(policy),
    )
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            await call
        assert [kind for kind, _ in policy.events] == ["admit"]
    else:
        assert len(await call) == 1
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
async def test_spacy_host_and_private_worker_share_the_owner(
    monkeypatch, private, refuse
):
    from frisket.ops import spacy_ner
    from frisket.ops.ner_local import LocalNerExtractor
    from frisket.engine.executor.ner_read import (
        AdmittedNerExtractor,
        _BoundNerExtractor,
    )

    policy = Policy(refuse=refuse)
    ctx = context(policy)
    if private:
        owner = AdmittedNerExtractor(ctx, engine="spacy")

        async def extract(*_args):
            policy.events.append(("backend", "private"))
            return []

        owner._spacy_session = SimpleNamespace(extract=extract)
        extractor = _BoundNerExtractor(owner, ctx)
    else:
        monkeypatch.setattr(spacy_ner, "spacy_available", lambda: (True, None))

        def extract(*_args):
            policy.events.append(("backend", "host"))
            return [[]]

        monkeypatch.setattr(spacy_ner, "run_spacy_ner_default", extract)
        extractor = LocalNerExtractor("spacy", ctx)
    call = extractor.extract("Alice", labels=["person"], threshold=0.5)
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            await call
        assert [kind for kind, _ in policy.events] == ["admit"]
    else:
        assert await call == []
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["faster_whisper", "parakeet-tdt"])
@pytest.mark.parametrize("refuse", [False, True])
async def test_local_transcription_adapter_cannot_bypass_owner(
    monkeypatch, tmp_path, engine, refuse
):
    from frisket.sdk.ops import transcribe_engines as module

    policy = Policy(refuse=refuse)
    adapter = (
        module.FasterWhisperAdapter
        if engine == "faster_whisper"
        else module.ParakeetAdapter
    )

    async def transcribe(*_args, **_kwargs):
        policy.events.append(("backend", engine))
        return {"text": "spoken words", "segments": [], "duration": 1.0}

    monkeypatch.setattr(adapter, "transcribe", transcribe)
    call = module.run_transcription_engine(
        engine, str(tmp_path / "audio.wav"), {}, context(policy), transport="local"
    )
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            await call
        assert [kind for kind, _ in policy.events] == ["admit"]
    else:
        assert (await call).output["text"] == "spoken words"
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,private", [("opus_mt", False), ("opus_mt", True), ("hy_mt2", False)]
)
@pytest.mark.parametrize("refuse", [False, True])
async def test_local_translation_batches_keep_context_policy(
    monkeypatch, engine, private, refuse
):
    from frisket.ops.integrations import opus_mt, hy_mt2
    from frisket.ops.integrations.translation_engine import TranslationEngine
    from frisket.runtime import translation_install

    policy = Policy(refuse=refuse)
    monkeypatch.setattr(translation_install, "host_present", lambda: not private)
    monkeypatch.setattr(translation_install, "is_installed", lambda: private)

    def translate(*_args):
        policy.events.append(("backend", engine))
        return ["translated one", "translated two"]

    monkeypatch.setattr(opus_mt, "translate_texts", translate)
    monkeypatch.setattr(hy_mt2, "translate_texts", translate)
    if private:
        from frisket.engine._workers import opus_mt_session

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def translate(self, *args):
                return translate(*args)

            async def aclose(self):
                pass

        monkeypatch.setattr(opus_mt_session, "OpusMtSession", Session)
    backend = TranslationEngine(invocation_owned=True)
    call = (
        backend._opus_translate("en", "es", ["one", "two"], None, ctx=context(policy))
        if engine == "opus_mt"
        else backend._hy_mt2_translate("Spanish", ["one", "two"], ctx=context(policy))
    )
    if refuse:
        with pytest.raises(RuntimeError, match="local execution refused"):
            await call
        assert [kind for kind, _ in policy.events] == ["admit"]
    else:
        assert len(await call) == 2
        assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
        assert policy.events[-1][1].usage == {"input_count": 2}
    await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["gliclass", "jeff"])
@pytest.mark.parametrize("outcome", ["ok", "denied", "malformed"])
async def test_native_classifier_owns_each_actual_request_before_output_validation(
    engine, outcome
):
    from frisket.actions.classify_types import ClassifyField
    from frisket.actions.types import Row, RowError
    from frisket.contracts.classification import LOCAL_CLASSIFIERS
    from frisket.engine.executor.classify_read import AdmittedClassifier

    policy = Policy(refuse=outcome == "denied")

    class Session:
        async def classify(self, text, labels, **_kwargs):
            policy.events.append(("backend", engine))
            return {
                "label": "not submitted" if outcome == "malformed" else labels[0],
                "score": 0.7,
                "model_revision": LOCAL_CLASSIFIERS[engine].revision,
            }

        async def aclose(self):
            pass

    owner = AdmittedClassifier(
        engine=engine, session_factory=lambda *_args, **_kwargs: Session()
    )
    row = Row({"text": "input"})
    bound = owner.bind_row(row, ctx=context(policy))
    fields = [ClassifyField(name="topic", labels=["one", "two"])]
    try:
        if outcome == "denied":
            with pytest.raises(RuntimeError, match="local execution refused"):
                await bound.classify(row, "input", fields)
            assert [kind for kind, _ in policy.events] == ["admit"]
        elif outcome == "malformed":
            with pytest.raises(RowError):
                await bound.classify(row, "input", fields)
            assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
            assert policy.events[-1][1].result["label"] == "not submitted"
        else:
            result = await bound.classify(row, "input", fields)
            assert result["topic"].value == "one"
            assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
    finally:
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["gliclass", "jeff"])
async def test_native_classifier_requires_its_bound_execution_context(engine):
    from frisket.actions.classify_types import ClassifyField
    from frisket.actions.types import Row
    from frisket.engine.executor.classify_read import AdmittedClassifier

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unbound native classifier must not create a session")

    owner = AdmittedClassifier(engine=engine, session_factory=forbidden)
    with pytest.raises(RuntimeError, match="requires its execution context"):
        await owner.classify(
            Row({}), "input", [ClassifyField(name="topic", labels=["one", "two"])]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["markitdown", "trafilatura_html"])
@pytest.mark.parametrize("refuse", [False, True])
async def test_native_document_engine_cannot_bypass_owner(
    monkeypatch, tmp_path, engine, refuse
):
    from frisket.actions.types import ColumnRef, Row
    from frisket.engine.executor import document_convert as module
    from frisket.engine.store import Project

    policy = Policy(refuse=refuse)
    project = Project.create(tmp_path / "documents.frisket")
    try:
        html = "<html><body><h1>Actual document</h1></body></html>"
        sheet = project.add_sheet("documents")
        column = project.add_column(sheet, "doc", type="text")
        row_id = project.add_rows(sheet, [{"doc": html}], {"doc": column})[0]
        row = Row({"doc": html})
        ctx = OpContext(
            project=project,
            extras={
                "preview": True,
                "cancelled": lambda: False,
                "service_operation_policy": policy,
            },
        )
        owner = module.AdmittedDocumentConverter(ctx, engine=engine)

        def convert(*_args):
            policy.events.append(("backend", engine))
            return "# Actual document"

        async def convert_async(*args):
            return convert(*args)

        method = (
            "_convert_markitdown"
            if engine == "markitdown"
            else "_convert_trafilatura_html"
        )
        monkeypatch.setattr(
            module._BoundDocumentConverter,
            method,
            convert_async if engine == "markitdown" else convert,
        )
        bound = owner.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            sources={
                "doc": {"column_id": column, "column_type": "text", "value": html}
            },
            ctx=ctx,
        )
        if refuse:
            with pytest.raises(RuntimeError, match="local execution refused"):
                await bound.convert(row, ColumnRef("doc"))
            assert [kind for kind, _ in policy.events] == ["admit"]
        else:
            result = await bound.convert(row, ColumnRef("doc"))
            assert result.markdown == "# Actual document"
            assert [kind for kind, _ in policy.events] == ["admit", "backend", "record"]
        await owner.aclose()
    finally:
        project.close()

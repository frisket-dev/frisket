from types import SimpleNamespace

import httpx
import pytest

from frisket.actions.translate_types import TranslationOptions, translation_text
from frisket.actions.types import Row, RowError
from frisket.engine.executor import translate_read
from frisket.engine.executor.translate_read import AdmittedTranslator, TranslationEngine
from frisket.execution.credential_use import CredentialUseContext
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.integrations.translate_common import TranslateEngineError


@pytest.mark.parametrize("engine", ["opus_mt", "hy_mt2"])
def test_local_translation_receipt_keeps_actual_pinned_artifact_without_rereading(
    tmp_path, monkeypatch, engine
):
    from frisket.ai.llm import ModelRouter
    from frisket.ai.models import artifact_manifest
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.execution.provider import (
        ExecutionCompositionContext,
        open_execution_composition,
    )
    from frisket.ops.integrations import hy_mt2, opus_mt

    implementation = opus_mt if engine == "opus_mt" else hy_mt2
    pinned = SimpleNamespace(
        ref=f"{engine}:actual",
        manifest_version="used-v1",
        license="Apache-2.0",
        license_url="https://example.test/license",
        composite_digest="sha256:actual",
        source_url="https://example.test/weights",
    )
    translated = False
    captured = False

    def translate(*args):
        nonlocal translated
        translated = True
        return ["Hola"]

    def lookup(*args):
        nonlocal captured
        if translated:
            assert not captured, "receipt must retain captured artifact facts"
            captured = True
        return pinned

    monkeypatch.setattr(implementation, "runtime_available", lambda: True)
    monkeypatch.setattr(implementation, "translate_texts", translate)
    monkeypatch.setattr(artifact_manifest, "lookup", lookup)
    monkeypatch.setattr(artifact_manifest, "hy_mt2_artifact", lookup)
    project = Project.create(tmp_path / f"{engine}.frisket")
    try:
        sheet = project.add_sheet("Text")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": "Hello"}], {"text": column})
        router = ModelRouter(keys={}, use_env_keys=False, cache=None, cache_mode="off")
        deps = ExecutorDeps(
            execution_composition=open_execution_composition(
                project, router, ExecutionCompositionContext.direct()
            )
        )
        request = {
            "action_id": "map.translate",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["text"],
                "engine": engine,
                "target_language": "Spanish",
                **({"language": ["en"]} if engine == "opus_mt" else {}),
            },
            "idempotency_key": "actual-local-artifact",
        }
        result = run_action_spec(
            project, request, project_id="translate", router=router, deps=deps
        )
        assert result.status == "completed", result.errors
        assert translated and captured
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        artifacts = [
            item
            for item in receipt.evidence
            if item.ref.get("kind") == "map_translate_artifact"
        ]
        assert len(artifacts) == 1
        assert artifacts[0].retention == "pinned"
        assert artifacts[0].ref == {
            "kind": "map_translate_artifact",
            **vars(pinned),
            "run_id": result.run_id,
            "op_id": project.db.execute(
                "SELECT op_id FROM runs WHERE id=?", (result.run_id,)
            ).fetchone()[0],
        }
        replay = run_action_spec(
            project, request, project_id="translate", router=router, deps=deps
        )
        assert replay.status == "completed" and replay.receipt_id == result.receipt_id
    finally:
        project.close()


@pytest.mark.parametrize(
    "engine,values",
    [
        ("opus_mt", {}),
        ("hy_mt2", {"language": ["en"]}),
        ("hy_mt2", {"save_detected_language": True}),
        ("deepl", {"language": ["en", "fr"]}),
        ("invalid", {}),
    ],
)
def test_translation_options_reject_unsupported_intent(engine, values):
    with pytest.raises(ValueError):
        TranslationOptions(**values).normalize(engine)


def test_translation_text_is_unlabelled_and_retains_false_and_zero():
    assert (
        translation_text(
            {"a": " Hola ", "b": 0, "c": False, "image": {"blob": "x"}, "empty": " "}
        )
        == " Hola \n\n0\n\nFalse"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["deepl", "google_translate", "opus_mt", "hy_mt2"])
async def test_empty_text_skips_every_provider(engine):
    data, accounting = await TranslationEngine().execute(
        {"empty": " "}, {"engine": engine}, OpContext(project=None)
    )
    assert data == {"translation": ""}
    assert accounting == {"cost": 0.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["deepl", "google_translate"])
@pytest.mark.parametrize("translated", ["Hello", ""])
async def test_hosted_actual_response_and_empty_failure_retain_accounting(
    monkeypatch, engine, translated
):
    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "test-key")
    requests = []

    def respond(request):
        requests.append(request)
        payload = (
            {
                "translations": [
                    {
                        "text": translated,
                        "detected_source_language": "ES",
                        "billed_characters": 4,
                    }
                ]
            }
            if engine == "deepl"
            else {
                "data": {
                    "translations": [
                        {"translatedText": translated, "detectedSourceLanguage": "es"}
                    ]
                }
            }
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        ctx = OpContext(
            project=None, http=http, credential_use_context=CredentialUseContext.open()
        )
        spec = {
            "engine": engine,
            "target_language": "English",
            "save_detected_language": True,
        }
        if translated:
            data, accounting = await TranslationEngine().execute(
                {"text": "Hola"}, spec, ctx
            )
            assert data == {"translation": "Hello", "detected_language": "es"}
        else:
            with pytest.raises(
                TranslateEngineError, match="empty translation"
            ) as caught:
                await TranslationEngine().execute({"text": "Hola"}, spec, ctx)
            assert caught.value.code == "empty_output"
            accounting = caught.value.accounting
    assert len(requests) == 1
    assert accounting["model_calls"][0]["engine"] == engine
    assert accounting["model_calls"][0]["units"]["characters"] == 4


@pytest.mark.asyncio
async def test_bound_translator_checks_admitted_options_route_and_single_call(
    monkeypatch,
):
    options = TranslationOptions(language=["es"])
    ctx = OpContext(project=None)
    route = SimpleNamespace(
        engine="opus_mt",
        target_snapshot={"capability": "translate", "transport": "local"},
        options={"language": ["es"]},
    )
    monkeypatch.setattr(
        translate_read,
        "routed_admission_in_scope",
        lambda extras: SimpleNamespace(route=route),
    )
    called = []

    async def execute(self, values, spec, context):
        called.append(values)
        return {"translation": "Hello"}

    monkeypatch.setattr(TranslationEngine, "execute", execute)
    owner = AdmittedTranslator(ctx, engine="opus_mt", options=options.model_dump())
    row = Row({"a": "Hola"})
    bound = owner.bind_row(row, sheet_id=1, row_id=1, sources={}, ctx=ctx)
    with pytest.raises(RowError, match="options differ"):
        await bound.translate(row, "Hola", options=TranslationOptions(language=["fr"]))
    route.engine = "hy_mt2"
    with pytest.raises(RecipeInvocationHalt):
        await bound.translate(row, "Hola", options=options)
    assert not called
    route.engine = "opus_mt"
    assert (
        await bound.translate(row, "derived Hola", options=options)
    ).translation == "Hello"
    assert called == [{"text": "derived Hola"}]
    assert owner.accounting_by_row[1] == {"cost": 0.0, "model_calls": []}
    with pytest.raises(RowError, match="one call"):
        await bound.translate(row, "Hola", options=options)
    await owner.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await bound.translate(row, "Hola", options=options)


@pytest.mark.asyncio
async def test_bound_translator_preserves_failed_paid_call(monkeypatch):
    options = TranslationOptions()
    ctx = OpContext(project=None)
    route = SimpleNamespace(
        engine="deepl",
        target_snapshot={"capability": "translate", "transport": "deepl.v2"},
        options={"language": []},
    )
    monkeypatch.setattr(
        translate_read,
        "routed_admission_in_scope",
        lambda extras: SimpleNamespace(route=route),
    )
    accounting = {"cost": None, "model_calls": [{"request_id": "charged"}]}

    async def fail(self, values, spec, context):
        error = TranslateEngineError(code="empty_output", message="empty translation")
        error.accounting = accounting
        raise error

    monkeypatch.setattr(TranslationEngine, "execute", fail)
    owner = AdmittedTranslator(ctx, engine="deepl", options=options.model_dump())
    row = Row({"a": "Hola"})
    bound = owner.bind_row(row, sheet_id=1, row_id=1, sources={}, ctx=ctx)
    with pytest.raises(TranslateEngineError):
        await bound.translate(row, "Hola", options=options)
    assert owner.accounting_by_row[1] == accounting


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["opus_mt", "hy_mt2"])
@pytest.mark.parametrize("failure", ["checksum_mismatch", "cancelled", None])
async def test_local_provisioning_integrity_cancellation_and_artifact_facts(
    monkeypatch, engine, failure
):
    from frisket.ai.models import artifact_manifest
    from frisket.engine.jobs import artifact_pull
    from frisket.ops.integrations import hy_mt2, opus_mt

    implementation = opus_mt if engine == "opus_mt" else hy_mt2
    missing = (
        opus_mt.OpusPairNotInstalled
        if engine == "opus_mt"
        else hy_mt2.HyMt2NotInstalled
    )
    installed = False
    signals = []
    pinned = SimpleNamespace(
        ref="test-model",
        manifest_version="test-v1",
        license="Apache-2.0",
        license_url="https://example.test/license",
        composite_digest="sha256:pinned",
        source_url="https://example.test/model",
    )
    monkeypatch.setattr(artifact_manifest, "lookup", lambda ref: pinned)
    monkeypatch.setattr(artifact_manifest, "hy_mt2_artifact", lambda: pinned)
    monkeypatch.setattr(implementation, "runtime_available", lambda: True)

    def translate(*args):
        if not installed:
            raise missing("not installed")
        return ["Hola"]

    def install(*args, should_cancel=None):
        nonlocal installed
        signals.append(should_cancel)
        if failure == "checksum_mismatch":
            raise artifact_pull.ArtifactProvisionError(
                "checksum mismatch", code=failure
            )
        if failure == "cancelled":
            raise artifact_pull.ProvisionCancelled("cancelled")
        installed = True

    monkeypatch.setattr(implementation, "translate_texts", translate)
    monkeypatch.setattr(
        implementation,
        "ensure_pair_installed" if engine == "opus_mt" else "ensure_installed",
        install,
    )

    def signal():
        return False

    ctx = OpContext(project=None, extras={"cancelled": signal})
    spec = {
        "engine": engine,
        "target_language": "Spanish",
        "language": ["en"] if engine == "opus_mt" else [],
        "save_detected_language": engine == "opus_mt",
    }
    if failure:
        with pytest.raises(TranslateEngineError) as caught:
            await TranslationEngine().execute({"text": "Hello"}, spec, ctx)
        expected = (
            "cancelled"
            if failure == "cancelled"
            else "pair_tampered"
            if engine == "opus_mt"
            else "model_tampered"
        )
        assert caught.value.code == expected
        assert caught.value.retryable is (failure == "cancelled")
    else:
        result = await TranslationEngine().execute({"text": "Hello"}, spec, ctx)
        assert result == {
            "translation": "Hola",
            **({"detected_language": "en"} if engine == "opus_mt" else {}),
        }
        assert ctx.extras["artifact_provenance"][0]["license_url"] == pinned.license_url
        assert (
            ctx.extras["artifact_provenance"][0]["composite_digest"]
            == pinned.composite_digest
        )
    assert signals == [signal]

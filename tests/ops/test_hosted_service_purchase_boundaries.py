"""Direct adapters admit the prepared purchase and retain paid facts before shaping output."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import wave

import httpx
import pytest
from PIL import Image

from frisket.credentials import ResolvedCredential
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.purchases import PurchaseAdmission, PurchaseRecordingError
from frisket.ops.base import OpContext


class Purchases:
    def __init__(self, *, refuse=False):
        self.events = []
        self.refuse = refuse

    def admit_purchase(self, request):
        self.events.append(("admit", vars(request)))
        if self.refuse:
            raise ValueError("actual request cannot be funded")
        return PurchaseAdmission("metered", f"operation-{len(self.events)}")

    def record_purchase(self, request, admission, observation):
        self.events.append(
            (
                "finish",
                {
                    "operation_id": admission.operation_id,
                    **vars(observation),
                },
            )
        )

    def fail_purchase(self, request, admission, error):
        self.events.append(
            (
                "fail",
                {
                    "operation_id": admission.operation_id,
                    "error": error,
                },
            )
        )


def context(policy, **kwargs):
    return OpContext(
        extras={
            "service_operation_policy": policy,
            "preview": True,
            "cancelled": lambda: False,
        },
        credential_use_context=CredentialUseContext.open(),
        **kwargs,
    )


def credential(monkeypatch):
    from frisket import credentials

    selected = ResolvedCredential("fixture-secret", "local")
    monkeypatch.setattr(
        credentials, "resolve_credential_for_use", lambda *args, **kwargs: selected
    )
    return selected


@pytest.mark.asyncio
async def test_datalab_admits_each_page_and_keeps_first_purchase_when_second_fails(
    tmp_path, monkeypatch
):
    from frisket.ops.ocr_engines_remote import ocr_datalab
    from frisket.ops.integrations import datalab
    from frisket.ops.integrations.hosted_error import HostedEngineError

    selected = credential(monkeypatch)
    policy = Purchases()
    pages = [tmp_path / "one.png", tmp_path / "two.png"]
    for page in pages:
        Image.new("RGB", (4, 4)).save(page)
    calls = []

    async def provider(http, key, payload, name, mime, **kwargs):
        assert policy.events[-1][0] == "admit"
        assert key == selected.value
        calls.append(name)
        kwargs["on_accepted"]({"cost": None, "model_calls": [{"id": name}]})
        if len(calls) == 2:
            raise HostedEngineError(
                "transport", "unknown second page", provider_job_accepted=True
            )
        return [{"text": "first retained page", "blocks": []}], 0.01

    monkeypatch.setattr(datalab, "datalab_ocr", provider)
    with pytest.raises(HostedEngineError):
        await ocr_datalab(pages, context(policy), {"calls": 0, "cost": 0.0})
    assert [kind for kind, _ in policy.events] == ["admit", "finish", "admit", "fail"]
    assert policy.events[0][1]["credential_source"] == selected.source
    assert policy.events[1][1]["provider_cost_micro"] == 10_000
    assert policy.events[1][1]["result"][0]["text"] == "first retained page"
    assert policy.events[3][1]["error"].provider_job_accepted
    assert policy.events[3][1]["error"].accounting["model_calls"][0]["id"] == "two.png"


@pytest.mark.asyncio
async def test_datalab_conversion_estimates_measured_document_and_records_known_cost(
    tmp_path, monkeypatch
):
    from frisket.engine.executor.document_convert import _BoundDocumentConverter
    from frisket.ops.integrations import datalab

    credential(monkeypatch)
    policy = Purchases()
    page = tmp_path / "one.png"
    Image.new("RGB", (4, 4)).save(page)

    async def provider(http, key, payload, name, mime, **kwargs):
        assert policy.events[-1][0] == "admit"
        kwargs["on_accepted"]({"cost": None, "model_calls": [{"id": "document"}]})
        return {"markdown": "saved", "page_count": 1}, 0.012

    monkeypatch.setattr(datalab, "datalab_convert", provider)
    reader = _BoundDocumentConverter(None, None, 1, 1, {}, context(policy))
    result, meta = await reader._convert_datalab(page)
    assert result == "saved"
    assert policy.events[0][1]["prepared"]["pages"] == 1
    assert policy.events[0][1]["estimated_cost_usd"] > 0
    assert policy.events[1][1]["provider_cost_micro"] == 12_000


@pytest.mark.asyncio
async def test_opendocrouter_shared_hook_uses_resolved_key_and_measured_pages(
    tmp_path, monkeypatch
):
    from frisket.ops import opendocrouter

    selected = credential(monkeypatch)
    monkeypatch.setattr(
        opendocrouter, "resolve_credential_for_use", lambda *a, **kw: selected
    )
    policy = Purchases()
    page = tmp_path / "one.png"
    Image.new("RGB", (4, 4)).save(page)

    async def provider(*args, **kwargs):
        assert policy.events[-1][0] == "admit"
        assert kwargs["credential_source"] == selected.source
        return [{"markdown": "retained"}], {"cost": 0.025, "model_calls": []}

    monkeypatch.setattr(opendocrouter, "parse_document", provider)
    await opendocrouter.parse_with_context(
        ctx=context(policy),
        path=page,
        engine="opendocrouter/rednote-hilab/dots.mocr",
        capability="ocr",
    )
    assert policy.events[0][1]["prepared"]["pages"] == 1
    assert policy.events[1][1]["provider_cost_micro"] == 25_000


def audio_file(path):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 8000)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True])
async def test_direct_transcription_admits_before_http_and_preserves_usage_before_bad_output(
    tmp_path, refuse
):
    from frisket.sdk.ops.transcription.hosted import OpenAITranscriptionAdapter
    from frisket.ops.integrations.hosted_error import HostedEngineError

    policy = Purchases(refuse=refuse)
    router = SimpleNamespace(
        _adapters={
            "openai": SimpleNamespace(
                base_url="https://provider.test", api_key="fixture"
            )
        },
        credential_source_for=lambda provider: "org_byok",
        model_call_policy=policy,
    )
    calls = []

    def provider(request):
        assert policy.events[-1][0] == "admit"
        calls.append(request)
        return httpx.Response(
            200, json={"text": "saved transcript", "duration": 1.0, "segments": [None]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        ctx = context(policy, http=client)
        ctx.extras["router"] = router
        with pytest.raises(ValueError if refuse else HostedEngineError):
            await OpenAITranscriptionAdapter().transcribe(
                "openai/whisper-1", str(audio_file(tmp_path / "one.wav")), {}, ctx, None
            )
    assert len(calls) == (0 if refuse else 1)
    assert policy.events[0][1]["credential_source"] == "org_byok"
    assert policy.events[0][1]["prepared"]["audio_seconds"] == pytest.approx(1.0)
    if not refuse:
        assert [kind for kind, _ in policy.events] == ["admit", "finish"]
        assert policy.events[1][1]["provider_cost_micro"] == 100
        assert policy.events[1][1]["result"]["text"] == "saved transcript"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["deepl", "google_translate"])
async def test_translation_records_known_paid_result_before_empty_output_validation(
    monkeypatch, engine
):
    from frisket.ops.integrations.translation_engine import TranslationEngine
    from frisket.ops.integrations.hosted_error import HostedEngineError

    credential(monkeypatch)
    policy = Purchases()

    def provider(request):
        assert policy.events[-1][0] == "admit"
        body = (
            {"translations": [{"text": "", "billed_characters": 7}]}
            if engine == "deepl"
            else {"data": {"translations": [{"translatedText": ""}]}}
        )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(HostedEngineError):
            await TranslationEngine().execute(
                {"body": "example"},
                {"engine": engine, "target_language": "French"},
                context(policy, http=client),
            )
    assert [kind for kind, _ in policy.events] == ["admit", "finish"]
    assert policy.events[1][1]["usage"]["characters"] == 7
    assert policy.events[1][1]["provider_cost_micro"] > 0


@pytest.mark.asyncio
async def test_decisions_records_usage_before_result_validation(monkeypatch):
    from frisket.actions.classify import ClassifyParams
    from frisket.actions.types import Row, RowError
    from frisket.engine.executor.decision_read import AdmittedDecisionClassifier
    from frisket.execution.definitions import StaticExecutionTargetProvider
    from frisket.execution.provider import CompositionFacts
    from frisket.execution.resolver import ResolutionRequest, resolve

    policy = Purchases()
    resolution = resolve(
        ResolutionRequest(engine="openai-decisions", capability="classify"),
        StaticExecutionTargetProvider(env={"OPENAI_API_KEY": "fixture"}),
        CompositionFacts(),
    )
    monkeypatch.setattr(
        "frisket.engine.executor.decision_read.preview_resolution_in_scope",
        lambda _: resolution,
    )

    def provider(request):
        assert policy.events[-1][0] == "admit"
        return httpx.Response(
            200,
            json={
                "model": "gpt-6-luna",
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                },
                "answers": [],
            },
        )

    row = Row({"body": "current classifier input"})
    params = ClassifyParams(
        source=["body"],
        engine="openai-decisions",
        fields=[{"name": "answer", "type": "boolean"}],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        classifier = AdmittedDecisionClassifier(engine="openai-decisions").bind_row(
            row, row_id=1, ctx=context(policy, http=client)
        )
        with pytest.raises(RowError):
            await classifier.classify(row, row.values["body"], params.fields)
    assert [kind for kind, _ in policy.events] == ["admit", "finish"]
    assert policy.events[1][1]["provider_cost_micro"] > 0


@pytest.mark.asyncio
async def test_ask_search_owns_authority_in_actual_search_task():
    from frisket.ai.research.search import SearchResponse, SearchService, SearchUsage
    from frisket.execution.dispatch_control import STANDALONE
    from frisket.execution.purchases import PurchaseAdmission
    from frisket.server.services.project_qa_runner import _search_with_purchase

    events = []
    owner = None
    response = SearchResponse(
        provider="exa",
        results=(),
        usage=SearchUsage(
            provider="exa",
            service="search",
            request_count=1,
            provider_reported_cost_usd=0.005,
            provider_cost_usd=0.005,
            cost_source="provider_reported",
        ),
    )

    @contextmanager
    def scope():
        nonlocal owner
        owner = asyncio.current_task()
        events.append("scope")
        yield
        owner = None

    class Policy:
        def admit_purchase(self, request):
            assert owner is asyncio.current_task()
            assert request.credential_source == "org_byok"
            events.append("admit")
            return PurchaseAdmission("byok", "search-operation")

        def record_purchase(self, request, admission, observation):
            assert owner is asyncio.current_task()
            assert observation.provider_cost_micro == 5000
            events.append("finish")

    class Search:
        credential_source = "org_byok"

        def estimate(self, **kwargs):
            return SearchService(
                preference="exa", effective_keys={"exa": "fixture"}
            ).estimate(**kwargs)

        async def search(self, query, **kwargs):
            assert events[-1] == "admit"
            events.append("http")
            return response

    assert (
        await asyncio.wait_for(
            _search_with_purchase(
                Search(),
                "actual query",
                policy=Policy(),
                dispatch=STANDALONE,
                call_scope=scope,
            ),
            2,
        )
        is response
    )
    assert events == ["scope", "admit", "http", "finish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"pages": None}, {"pages": "not a list"}, []])
async def test_sidecar_malformed_pages_records_returned_purchase_before_validation(
    tmp_path, monkeypatch, body
):
    from frisket.ops import ocr_engines_sidecar as sidecar

    policy, ctx, page, calls = _sidecar_purchase(tmp_path, monkeypatch, body)
    with pytest.raises(
        ValueError, match="OCR sidecar response must contain a pages list"
    ):
        await sidecar.ocr_sidecar("dots.mocr", [page], ctx)
    assert len(calls) == 1
    assert [kind for kind, _ in policy.events] == ["admit", "finish"]
    assert policy.events[-1][1]["result"] == body
    assert policy.events[-1][1]["usage"] == {}
    assert policy.events[-1][1]["provider_cost_micro"] is None
    assert policy.events[-1][1]["operation_id"] == "sidecar-operation"


def _sidecar_purchase(tmp_path, monkeypatch, body):
    from frisket.ops import ocr_engines_sidecar as sidecar
    from frisket.ops import _sidecar

    class SidecarPurchases(Purchases):
        def admit_purchase(self, request):
            self.events.append(("admit", vars(request)))
            return PurchaseAdmission("metered", "sidecar-operation")

    policy = SidecarPurchases()
    ctx = context(policy)
    page = tmp_path / "one.png"
    Image.new("RGB", (4, 4)).save(page)
    connection = SimpleNamespace(connect_timeout_seconds=10, timeout_seconds=60)
    monkeypatch.setattr(
        sidecar,
        "routed_admission_in_scope",
        lambda extras: SimpleNamespace(binding=SimpleNamespace(connection=connection)),
    )
    calls = []

    async def post(*args, **kwargs):
        calls.append(kwargs)
        assert [kind for kind, _ in policy.events] == ["admit"]
        return body

    monkeypatch.setattr(_sidecar, "_sidecar_post_transport", post)
    return policy, ctx, page, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("accounting_fails", [False, True])
async def test_sidecar_valid_pages_retained_without_misclassifying_accounting_error(
    tmp_path, monkeypatch, accounting_fails
):
    from frisket.ops import ocr_engines_sidecar as sidecar

    body = {"pages": [{"text": "Saved OCR", "blocks": []}]}
    policy, ctx, page, calls = _sidecar_purchase(tmp_path, monkeypatch, body)
    finish = policy.record_purchase

    def record(request, admission, observation):
        finish(request, admission, observation)
        if accounting_fails:
            raise RuntimeError("accounting unavailable")

    monkeypatch.setattr(policy, "record_purchase", record)
    if accounting_fails:
        with pytest.raises(
            PurchaseRecordingError, match="accounting unavailable"
        ) as error:
            await sidecar.ocr_sidecar("dots.mocr", [page], ctx)
        assert error.value.provider_result is body
        assert error.value.observation.result is body
    else:
        assert await sidecar.ocr_sidecar("dots.mocr", [page], ctx) is body["pages"]
    assert len(calls) == 1
    assert [kind for kind, _ in policy.events] == (
        ["admit", "finish", "fail"] if accounting_fails else ["admit", "finish"]
    )
    if accounting_fails:
        assert policy.events[-1][1]["error"] is error.value
    assert policy.events[1][1] == {
        "operation_id": "sidecar-operation",
        "result": body,
        "usage": {"pages": 1},
        "provider_cost_micro": None,
    }

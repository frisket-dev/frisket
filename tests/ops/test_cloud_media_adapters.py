from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import pytest

from frisket.actions.types import ColumnRef, Row
from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudMediaExecutionBinding,
    CloudPayerSubject,
    CloudQuoteAuthority,
)
from frisket.contracts.http.cloud_connection import (
    CloudCapabilityRoute,
    CloudDocumentConvertOptionSupport,
    CloudDocumentConvertResult,
    CloudDocumentConvertUsage,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudOCROptionSupport,
    CloudOCRResult,
    CloudOCRUsage,
    CloudTranscriptionOptionSupport,
    CloudTranscriptionResult,
    CloudTranscriptionUsage,
)
from frisket.engine.executor.document_convert import AdmittedDocumentConverter
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteRow
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    RoutedAdmission,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.provider import ConnectionConfig
from frisket.execution.resolver import CandidateBinding, RouteRowFacts
from frisket.execution.targets import (
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    ExecutionTarget,
    OcrOptionSupport,
    TargetEngineSupport,
    ToMarkdownOptionSupport,
)
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines_cloud import ocr_cloud
from frisket.ops.ocr_engines import OcrEngines
from frisket.sdk.ops.transcription.cloud import CloudTranscriptionAdapter
from frisket.sdk.ops.transcribe_engines import run_transcription_engine


ENGINE = "cloud-provider/media-model"


def _payer(*, engine: str = ENGINE) -> CloudPayerSubject:
    return CloudPayerSubject(
        user_id=7,
        account_id=41,
        lane="frisket_credits",
        provider="cloud-provider",
        engine=engine,
        route_revision="route-revision-1",
    )


def _attempt(*, engine: str = ENGINE) -> CloudExecutionAttempt:
    return CloudExecutionAttempt(
        request_id="durable-request-1",
        payer=_payer(engine=engine),
        quote_authority=CloudQuoteAuthority(maximum_charge_micro=90_000),
    )


def _response(
    capability: str,
    result: Any,
    usage: Any,
    option_support: Any,
    *,
    engine: str = ENGINE,
) -> CloudExecutionResponse:
    return CloudExecutionResponse(
        execution_id=f"cloud-{capability}-execution-1",
        status="completed",
        route=CloudCapabilityRoute(
            capability=capability,
            engine=engine,
            provider="cloud-provider",
            lane="frisket_credits",
            route_revision="route-revision-1",
            ready=True,
            blocker=None,
            price=None,
            options=option_support,
        ),
        result=result,
        receipt=CloudExecutionReceipt(
            user_id=7,
            account_id=41,
            lane="frisket_credits",
            provider="cloud-provider",
            engine=engine,
            charge_micro=75_000,
            provider_cost_micro=50_000,
            usage=usage,
        ),
    )


class _Transport:
    def __init__(self, response: CloudExecutionResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.streams: list[Any] = []

    async def execute_media(self, attempt, **request):
        uploads = list(request["uploads"])
        self.streams.extend(upload.stream for upload in uploads)
        self.calls.append(
            {
                "attempt": attempt,
                **request,
                "uploads": [
                    {
                        "file_name": upload.file_name,
                        "content_type": upload.content_type,
                        "body": upload.stream.read(),
                        "closed_during_call": upload.stream.closed,
                    }
                    for upload in uploads
                ],
            }
        )
        return self.response


def _ctx(
    transport: _Transport,
    capability: str,
    *,
    engine: str = ENGINE,
    ocr_geometry: bool = True,
) -> OpContext:
    return OpContext(
        extras=(
            _route_extras(
                engine,
                capability=CAPABILITY_OCR,
                option_support=OcrOptionSupport(geometry=ocr_geometry),
            )
            if capability == "ocr"
            else {}
        ),
        cloud_execution=CloudMediaExecutionBinding(
            transport=transport,
            attempt=_attempt(engine=engine),
            capability=capability,
        ),
    )


def test_cloud_ocr_batches_page_streams_once_and_preserves_geometry(tmp_path):
    engine = "dots.mocr"
    first = tmp_path / "page-1.png"
    second = tmp_path / "page-2.png"
    first.write_bytes(b"first-page")
    second.write_bytes(b"second-page")
    response = _response(
        "ocr",
        CloudOCRResult(
            pages=[
                {
                    "page": 1,
                    "text": "First",
                    "blocks": [
                        {
                            "text": "First",
                            "bbox": [[1, 2], [3, 2], [3, 4], [1, 4]],
                            "score": 0.95,
                        }
                    ],
                },
                {"page": 2, "text": "Second", "blocks": []},
            ]
        ),
        CloudOCRUsage(kind="ocr", pages=2),
        CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
        engine=engine,
    )
    transport = _Transport(response)
    usage: dict[str, Any] = {}

    pages = asyncio.run(
        ocr_cloud(
            engine,
            [first, second],
            _ctx(transport, "ocr", engine=engine, ocr_geometry=True),
            usage,
            language="en",
        )
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["capability"] == "ocr"
    assert call["options"].model_dump() == {
        "language": "en",
        "geometry": True,
        "searchable_pdf": False,
    }
    assert [item["body"] for item in call["uploads"]] == [
        b"first-page",
        b"second-page",
    ]
    assert all(not item["closed_during_call"] for item in call["uploads"])
    assert all(stream.closed for stream in transport.streams)
    assert pages[0]["blocks"][0]["bbox"] == [
        [1.0, 2.0],
        [3.0, 2.0],
        [3.0, 4.0],
        [1.0, 4.0],
    ]
    assert [page["page"] for page in pages] == [1, 2]
    assert usage["cloud_receipt"]["usage"] == {"kind": "ocr", "pages": 2}
    [fact] = usage["model_calls"]
    assert fact["id"] == "cloud-ocr-execution-1"
    assert fact["units"]["cloud_account_id"] == 41


def test_cloud_ocr_disables_geometry_for_admitted_plain_text_route(tmp_path):
    engine = "glm-ocr"
    page = tmp_path / "page.png"
    page.write_bytes(b"page")
    response = _response(
        "ocr",
        CloudOCRResult(pages=[{"page": 1, "text": "Plain text", "blocks": []}]),
        CloudOCRUsage(kind="ocr", pages=1),
        CloudOCROptionSupport(
            kind="ocr", language=True, geometry=False, searchable_pdf=False
        ),
        engine=engine,
    )
    transport = _Transport(response)

    pages = asyncio.run(
        ocr_cloud(
            engine,
            [page],
            _ctx(transport, "ocr", engine=engine, ocr_geometry=False),
            {},
        )
    )

    assert transport.calls[0]["options"].geometry is False
    assert pages == [{"page": 1, "text": "Plain text", "blocks": []}]


def test_cloud_ocr_requires_exact_typed_binding_before_opening_files(tmp_path):
    missing = tmp_path / "missing.png"

    with pytest.raises(CloudExecutionRefused, match="admitted paid attempt"):
        asyncio.run(ocr_cloud(ENGINE, [missing], OpContext(), {}))


def test_cloud_ocr_requires_admitted_engine_support_before_egress(tmp_path):
    page = tmp_path / "page.png"
    page.write_bytes(b"page")
    response = _response(
        "ocr",
        CloudOCRResult(pages=[{"page": 1, "text": "", "blocks": []}]),
        CloudOCRUsage(kind="ocr", pages=1),
        CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
    )
    transport = _Transport(response)
    ctx = _ctx(transport, "ocr")
    ctx.extras = {}

    with pytest.raises(CloudExecutionRefused, match="exact admitted engine support"):
        asyncio.run(ocr_cloud(ENGINE, [page], ctx, {}))

    assert transport.calls == []


def test_ocr_dispatch_selects_cloud_from_the_admitted_transport(tmp_path):
    page = tmp_path / "page.png"
    page.write_bytes(b"page")
    response = _response(
        "ocr",
        CloudOCRResult(pages=[{"page": 1, "text": "Cloud", "blocks": []}]),
        CloudOCRUsage(kind="ocr", pages=1),
        CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
        engine=ENGINE,
    )
    transport = _Transport(response)
    ctx = _ctx(transport, "ocr")

    pages = asyncio.run(
        OcrEngines().run_engine_on_pages(
            ENGINE,
            [page],
            ctx,
            usage={},
            scratch=tmp_path,
        )
    )

    assert pages[0]["text"] == "Cloud"
    assert len(transport.calls) == 1


def test_cloud_transcription_preserves_segments_speakers_words_and_receipt(tmp_path):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"RIFF-audio")
    response = _response(
        "transcribe",
        CloudTranscriptionResult(
            text=" Speaker one ",
            segments=[
                {
                    "segment_index": 4,
                    "start": 0.1,
                    "end": 1.4,
                    "text": "Speaker one",
                    "speaker": "A",
                    "speaker_confidence": "high",
                    "words": [{"word": "Speaker", "start": 0.1, "end": 0.7}],
                }
            ],
            language="en",
            duration=1.4,
        ),
        CloudTranscriptionUsage(kind="transcribe", audio_seconds=1.4),
        CloudTranscriptionOptionSupport(
            kind="transcribe",
            diarization_mode="optional",
            speaker_hint="count",
            language=True,
            model_size=False,
            vad=True,
            context=True,
        ),
    )
    transport = _Transport(response)
    adapter = CloudTranscriptionAdapter()

    out = asyncio.run(
        adapter.transcribe(
            ENGINE,
            str(audio),
            {"language": "en", "diarize": True},
            _ctx(transport, "transcribe"),
            {"filename": "interview.wav", "mime": "audio/wav"},
        )
    )
    calls = adapter.model_calls(ENGINE, str(audio), {}, out)

    assert len(transport.calls) == 1
    assert transport.calls[0]["uploads"] == [
        {
            "file_name": "interview.wav",
            "content_type": "audio/wav",
            "body": b"RIFF-audio",
            "closed_during_call": False,
        }
    ]
    assert transport.streams[0].closed
    assert out["text"] == "Speaker one"
    assert out["language"] == "en"
    assert out["duration"] == 1.4
    assert out["segments"] == [
        {
            "segment_index": 4,
            "start": 0.1,
            "end": 1.4,
            "text": "Speaker one",
            "speaker": "A",
            "speaker_confidence": "high",
            "words": [{"word": "Speaker", "start": 0.1, "end": 0.7}],
        }
    ]
    assert calls[0]["id"] == "cloud-transcribe-execution-1"
    assert calls[0]["units"]["audio_seconds"] == 1.4
    assert out["cloud_receipt"]["charge_micro"] == 75_000


def test_transcription_dispatch_selects_cloud_transport(tmp_path):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"RIFF-audio")
    response = _response(
        "transcribe",
        CloudTranscriptionResult(
            text="Cloud transcript", segments=[], language="en", duration=1.0
        ),
        CloudTranscriptionUsage(kind="transcribe", audio_seconds=1.0),
        CloudTranscriptionOptionSupport(
            kind="transcribe",
            diarization_mode="optional",
            speaker_hint="count",
            language=True,
            model_size=False,
            vad=True,
            context=True,
        ),
    )
    transport = _Transport(response)

    result = asyncio.run(
        run_transcription_engine(
            ENGINE,
            str(audio),
            {},
            _ctx(transport, "transcribe"),
            transport="frisket.cloud.media.v1",
        )
    )

    assert result.output["text"] == "Cloud transcript"
    assert len(transport.calls) == 1


def _route_extras(
    engine: str,
    *,
    capability: str = CAPABILITY_TO_MARKDOWN,
    option_support: OcrOptionSupport | ToMarkdownOptionSupport | None = None,
) -> dict[str, Any]:
    if option_support is None:
        option_support = ToMarkdownOptionSupport()
    payer = _payer(engine=engine)
    facts = RouteRowFacts(
        target_id="frisket-cloud:41:frisket_credits:cloud-provider",
        engine=engine,
        operator="frisket",
        egress_class="frisket_shared",
        region=None,
        credential_source="platform_key",
        cost_posture="platform_metered",
        cloud_payer=payer,
    )
    route = RouteRow(
        id="route_CLOUDDOCUMENT0000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_CLOUD",
        engine=engine,
        options={},
        target_snapshot={
            "target_id": facts.target_id,
            "capability": capability,
            "transport": "frisket.cloud.media.v1",
            "run_scoped": False,
            "cloud_payer": payer.to_snapshot(),
        },
        route_fact_hash="hash",
        operator=facts.operator,
        egress_class=facts.egress_class,
        region=None,
        credential_source=facts.credential_source,
        cost_posture=facts.cost_posture,
        created_at="2026-10-11T00:00:00+00:00",
    )
    binding = CandidateBinding(
        facts=facts,
        connection=ConnectionConfig(base_url="https://unused.invalid"),
        target=ExecutionTarget(
            id=facts.target_id,
            operator=facts.operator,
            egress_class=facts.egress_class,
            engines=(
                TargetEngineSupport(
                    engine=engine,
                    transport="frisket.cloud.media.v1",
                    capability=capability,
                    options=option_support,
                ),
            ),
        ),
    )
    return {
        ATTEMPT_EXTRA: AttemptCommitment(
            attempt_id="attempt_CLOUDDOCUMENT00000000",
            run_id=1,
            seq=0,
            identity="identity",
            scope=(1,),
            admission=RoutedAdmission(
                head_route_id=route.id,
                head_promise_set_id=route.promise_set_id,
                route=route,
                promise_set=None,
                binding=binding,
                evaluation=None,
                admitted_by_consent_id=None,
            ),
            cost_basis=OperatorBorneZeroCost(),
            price_card_version=None,
        )
    }


def test_cloud_document_convert_keeps_source_provenance_and_external_accounting(
    tmp_path,
):
    engine = "docling"
    response = _response(
        "document.convert",
        CloudDocumentConvertResult(markdown=" # Cloud result ", ocr_used=[True, False]),
        CloudDocumentConvertUsage(kind="document.convert", pages=2),
        CloudDocumentConvertOptionSupport(kind="document.convert", mime_types=[]),
        engine=engine,
    )
    transport = _Transport(response)
    with closing(
        Project.create(tmp_path / "cloud-doc.frisket", name="cloud-doc")
    ) as project:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "doc", type="text")
        [row_id] = project.add_rows(
            sheet_id, [{"doc": "source document"}], {"doc": column_id}
        )
        extras = _route_extras(engine)
        ctx = OpContext(
            project=project,
            extras=extras,
            cloud_execution=CloudMediaExecutionBinding(
                transport=transport,
                attempt=_attempt(engine=engine),
                capability="document.convert",
            ),
        )
        owner = AdmittedDocumentConverter(ctx, engine=engine)
        row = Row({"doc": "source document"})
        bound = owner.bind_row(
            row,
            sheet_id=sheet_id,
            row_id=row_id,
            sources={
                "doc": {
                    "column_id": column_id,
                    "column_type": "text",
                    "value": "source document",
                }
            },
            ctx=ctx,
        )

        converted = asyncio.run(bound.convert(row, ColumnRef("doc")))

    assert converted.markdown == "# Cloud result"
    assert converted.ocr_used == [True, False]
    assert len(transport.calls) == 1
    assert transport.calls[0]["uploads"][0]["body"] == b"source document"
    assert transport.streams[0].closed
    [read] = owner.calls_by_row[row_id]
    assert read["external"] is True
    assert read["tier"] == "hosted"
    assert read["document_read"] == {
        "kind": "text",
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "source_column": "doc",
    }
    accounting = owner.accounting_by_row[row_id]
    assert accounting["cost"] == 0.075
    assert accounting["cloud_receipt"]["charge_micro"] == 75_000
    [fact] = accounting["model_calls"]
    assert fact["provider"] == "cloud-provider"
    assert fact["units"]["pages"] == 2

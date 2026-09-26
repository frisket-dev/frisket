from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel

from frisket.ai.llm import LLMError, ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.server.services import project_qa as project_qa_service
from frisket.server.services import project_qa_tracing
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_tracing import ProjectQATracing
from frisket.server.workspace import Workspace


class _CaptureExporter(SpanExporter):
    def __init__(
        self, *, result: SpanExportResult = SpanExportResult.SUCCESS, **kwargs
    ):
        self.kwargs = kwargs
        self.result = result
        self.spans: list[ReadableSpan] = []
        self.shutdown_calls = 0

    def export(self, spans):
        self.spans.extend(spans)
        return self.result

    def shutdown(self):
        self.shutdown_calls += 1


def test_standard_otlp_environment_builds_private_http_provider(monkeypatch):
    exporters: list[_CaptureExporter] = []

    def exporter(**kwargs):
        created = _CaptureExporter(**kwargs)
        exporters.append(created)
        return created

    global_provider = trace.get_tracer_provider()
    monkeypatch.setattr(project_qa_tracing, "OTLPSpanExporter", exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel/")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Bearer%20not-a-real-key,x-bt-parent=project_name%3AReporting",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("FRISKET_ASK_TRACE_CONTENT", "true")

    tracing = project_qa_tracing.build_project_qa_tracing()
    assert tracing is not None
    assert trace.get_tracer_provider() is global_provider
    assert tracing.instrumentation.include_content is True
    assert tracing.instrumentation.include_binary_content is False
    assert exporters[0].kwargs == {
        "endpoint": "https://api.example/otel/v1/traces",
        "headers": {
            "authorization": "Bearer not-a-real-key",
            "x-bt-parent": "project_name:Reporting",
        },
    }
    tracing.shutdown()
    assert exporters[0].shutdown_calls == 1


def test_trace_specific_otlp_values_take_precedence(monkeypatch):
    exporters: list[_CaptureExporter] = []

    def exporter(**kwargs):
        created = _CaptureExporter(**kwargs)
        exporters.append(created)
        return created

    monkeypatch.setattr(project_qa_tracing, "OTLPSpanExporter", exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://generic.example/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://traces.example/custom",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "generic=value")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "traces=value")

    tracing = project_qa_tracing.build_project_qa_tracing()
    assert tracing is not None
    assert exporters[0].kwargs == {
        "endpoint": "https://traces.example/custom",
        "headers": {"traces": "value"},
    }
    tracing.shutdown()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OTEL_SDK_DISABLED", "true"),
        ("OTEL_TRACES_EXPORTER", "none"),
    ],
)
def test_standard_otlp_opt_outs_disable_ask_tracing(monkeypatch, name, value):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv(name, value)
    assert project_qa_tracing.build_project_qa_tracing() is None


def test_unsupported_protocol_names_only_the_offending_knob(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    with pytest.raises(ValueError) as raised:
        project_qa_tracing.build_project_qa_tracing()
    assert "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL" in str(raised.value)


def test_malformed_headers_do_not_reach_errors_or_logs(monkeypatch, caplog):
    secret = "not-a-real-secret-value"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization {secret}")
    with pytest.raises(ValueError) as raised:
        project_qa_tracing.build_project_qa_tracing()
    observed = str(raised.value) + caplog.text
    assert "OTEL_EXPORTER_OTLP_HEADERS" in observed
    assert secret not in observed


def test_actual_http_export_honors_content_opt_in(monkeypatch):
    received: list[tuple[str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(
                (
                    self.path,
                    {name.lower(): value for name, value in self.headers.items()},
                    self.rfile.read(length),
                )
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://{host}:{port}")
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20local-test"
        )
        monkeypatch.setenv("FRISKET_ASK_TRACE_CONTENT", "true")

        tracing = project_qa_tracing.build_project_qa_tracing()
        assert tracing is not None
        agent = Agent(
            TestModel(custom_output_text="private-content-answer-sentinel"),
            name="project_ask",
        )
        agent.instrument = tracing.instrumentation
        agent.run_sync("private-content-question-sentinel")
        tracing.shutdown()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert len(received) == 1
    path, headers, body = received[0]
    assert path == "/v1/traces"
    assert headers["authorization"] == "Bearer local-test"
    assert headers["content-type"] == "application/x-protobuf"
    assert b"private-content-question-sentinel" in body
    assert b"private-content-answer-sentinel" in body


def test_ask_only_metadata_spans_survive_export_failure_and_shutdown_once(
    tmp_path, monkeypatch
):
    async def scenario():
        exporter = _CaptureExporter(result=SpanExportResult.FAILURE)
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=1)
        )
        tracing = ProjectQATracing(
            instrumentation=InstrumentationSettings(
                tracer_provider=provider,
                include_content=False,
                include_binary_content=False,
            ),
            provider=provider,
        )
        monkeypatch.setattr(
            project_qa_service, "build_project_qa_tracing", lambda: tracing
        )
        global_provider = trace.get_tracer_provider()

        question = "private-question-sentinel"
        answer = "private-answer-sentinel"
        sheet_name = "private-sheet-sentinel"

        class Adapter:
            calls = 0

            async def complete(self, request, client):
                self.calls += 1
                if self.calls == 1:
                    name = "inspect_sheets"
                    args = {}
                    call_id = "inspect"
                else:
                    name = "final_result"
                    args = {"text": answer, "citation_ids": []}
                    call_id = "answer"
                return LLMResponse(
                    content=None,
                    data=None,
                    model=request.model,
                    tokens_in=2,
                    tokens_out=3,
                    cost=0.02,
                    tool_calls=[
                        {
                            "name": name,
                            "id": call_id,
                            "args": args,
                        }
                    ],
                )

        router = ModelRouter(
            keys={"anthropic": "test"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()
        workspace = Workspace(tmp_path / "ws", router=router)
        pid = workspace.create("Ask")["id"]
        workspace.get(pid).add_sheet(sheet_name)
        service = ProjectQAService(workspace)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="trace-once",
                question=question,
                scope={"kind": "project"},
                model="anthropic/test",
            ),
        )
        await asyncio.gather(*list(service._tasks.values()))
        detail = await service.detail(pid, thread["id"])
        assert detail["history"]["events"][-1]["payload"]["status"] == "completed"

        await Agent(TestModel(custom_output_text="untraced sibling")).run(
            "sibling-agent-sentinel"
        )
        await asyncio.gather(service.shutdown(), service.shutdown())

        assert trace.get_tracer_provider() is global_provider
        assert exporter.shutdown_calls == 1
        assert exporter.spans
        trace_text = repr(
            [
                (span.name, span.attributes, span.events, span.status.description)
                for span in exporter.spans
            ]
        )
        assert "project_ask" in trace_text
        assert "inspect_sheets" in trace_text
        assert question not in trace_text
        assert answer not in trace_text
        assert sheet_name not in trace_text
        assert "sibling-agent-sentinel" not in trace_text
        assert "untraced sibling" not in trace_text

    asyncio.run(scenario())


def test_provider_error_is_sanitized_before_metadata_only_export(tmp_path, monkeypatch):
    async def scenario():
        exporter = _CaptureExporter()
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=1)
        )
        tracing = ProjectQATracing(
            instrumentation=InstrumentationSettings(
                tracer_provider=provider,
                include_content=False,
                include_binary_content=False,
            ),
            provider=provider,
        )
        monkeypatch.setattr(
            project_qa_service, "build_project_qa_tracing", lambda: tracing
        )

        secret = "provider-secret-sentinel"

        class Adapter:
            async def complete(self, request, client):
                raise LLMError(f"api_key={secret}")

        router = ModelRouter(
            keys={"anthropic": secret},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()
        workspace = Workspace(tmp_path / "ws", router=router)
        pid = workspace.create("Ask")["id"]
        service = ProjectQAService(workspace)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="trace-error",
                question="private-error-question",
                scope={"kind": "project"},
                model="anthropic/test",
            ),
        )
        await asyncio.gather(*list(service._tasks.values()))
        await service.shutdown()

        trace_text = repr(
            [
                (span.attributes, span.events, span.status.description)
                for span in exporter.spans
            ]
        )
        assert secret not in trace_text

    asyncio.run(scenario())

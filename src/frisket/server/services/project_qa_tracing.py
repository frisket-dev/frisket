"""Private OpenTelemetry tracing for Project Ask agent runs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import unquote

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai.models.instrumented import InstrumentationSettings

_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
_TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_HEADERS = "OTEL_EXPORTER_OTLP_HEADERS"
_TRACES_HEADERS = "OTEL_EXPORTER_OTLP_TRACES_HEADERS"
_PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"
_TRACES_PROTOCOL = "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"
_SDK_DISABLED = "OTEL_SDK_DISABLED"
_TRACES_EXPORTER = "OTEL_TRACES_EXPORTER"
_CONTENT = "FRISKET_ASK_TRACE_CONTENT"

_HEADER_KEY = r"[\x21\x23-\x27\x2a\x2b\x2d\x2e\x30-\x39\x41-\x5a\x5e-\x7a\x7c\x7e]+"
_HEADER_VALUE = r"[\x20\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*"
_HEADER = re.compile(rf"[ \t]*({_HEADER_KEY})[ \t]*=[ \t]*({_HEADER_VALUE})[ \t]*")
_HEADER_SPLIT = re.compile(r"[ \t]*,[ \t]*")
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class ProjectQATracing:
    """The instrumentation settings and provider owned by one Ask service."""

    instrumentation: InstrumentationSettings
    provider: TracerProvider

    def shutdown(self) -> None:
        self.provider.shutdown()


def _first_nonblank(*names: str) -> tuple[str, str] | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return None


def _parse_headers(name: str, raw: str) -> dict[str, str]:
    """Parse standard OTLP headers without logging a malformed secret value."""

    headers: dict[str, str] = {}
    for item in _HEADER_SPLIT.split(raw):
        if not item:
            continue
        match = _HEADER.fullmatch(item)
        if match is None:
            raise ValueError(f"{name} must be a comma-separated name=value list")
        key, value = (unquote(part).strip() for part in match.groups())
        if re.fullmatch(_HEADER_KEY, key) is None or "\r" in value or "\n" in value:
            raise ValueError(f"{name} must be a comma-separated name=value list")
        headers[key.lower()] = value
    if not headers:
        raise ValueError(f"{name} must contain at least one name=value header")
    return headers


def build_project_qa_tracing() -> ProjectQATracing | None:
    """Build one private HTTP/protobuf provider from standard OTLP variables."""

    endpoint = _first_nonblank(_TRACES_ENDPOINT, _ENDPOINT)
    if endpoint is None:
        return None
    if os.environ.get(_SDK_DISABLED, "").strip().lower() == "true":
        return None
    if os.environ.get(_TRACES_EXPORTER, "").strip().lower() == "none":
        return None

    protocol = _first_nonblank(_TRACES_PROTOCOL, _PROTOCOL)
    if protocol is not None and protocol[1].lower() != "http/protobuf":
        raise ValueError(f"{protocol[0]} must be http/protobuf for Project Ask tracing")

    endpoint_name, endpoint_value = endpoint
    if endpoint_name == _ENDPOINT:
        endpoint_value = endpoint_value.rstrip("/") + "/v1/traces"

    selected_headers = _first_nonblank(_TRACES_HEADERS, _HEADERS)
    headers = (
        _parse_headers(*selected_headers) if selected_headers is not None else None
    )

    provider = TracerProvider(shutdown_on_exit=False)
    try:
        exporter = OTLPSpanExporter(endpoint=endpoint_value, headers=headers)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        instrumentation = InstrumentationSettings(
            tracer_provider=provider,
            include_content=os.environ.get(_CONTENT, "").strip().lower() in _TRUE,
            include_binary_content=False,
        )
    except Exception:
        provider.shutdown()
        raise
    return ProjectQATracing(instrumentation=instrumentation, provider=provider)

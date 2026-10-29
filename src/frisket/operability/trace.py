"""Best-effort model-call traces stored outside the project database.

Each run that reaches a model gets one gzip JSONL file under ``traces/``.
Trace capture is diagnostic only: capture, serialization, storage, and read
failures never change action results. Deterministic runs create no trace file.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Awaitable, Iterable, Protocol

from frisket.ai.llm.types import (
    LLMRequest,
    LLMResponse,
    ModelRouterTransport,
    ModelTrace,
    SchemaViolation,
)
from frisket.redaction import (
    CYCLE,
    MAX_REDACTION_INPUT_CHARS,
    TRUNCATED,
    redact_value,
    safe_error,
)

MAX_CAPTURED_STR = 4096
MAX_OBSERVED_TRACE_PROVIDERS = 64
MAX_CALL_BODIES = 32
MAX_CALL_HEADERS = 1024
MAX_ROW_BYTES = 1024 * 1024
_CALL_BODY_KEYS = ("prompt", "request", "raw_response", "data", "tool_calls", "events")
_CALL_HEADER_KEYS = (
    "model",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "cost",
    "cached",
    "error",
    "schema_violation",
    "truncated",
)
logger = logging.getLogger(__name__)


class _ModelCompletionCall(Protocol):
    def __call__(
        self,
        req: LLMRequest,
        *,
        recipe_version: str,
        trace: ModelTrace | None,
    ) -> Awaitable[LLMResponse]: ...


def trace_path(bundle_path: str | Path, run_id: int) -> Path:
    return Path(bundle_path) / "traces" / f"run-{run_id}.jsonl.gz"


def _pdf_metadata(value: Any, metadata: dict | None = None) -> dict:
    """Hash attachment bytes before generic redaction can stringify a binary part."""
    metadata = metadata or {}
    raw = bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else None
    if isinstance(value, str):
        encoded = (
            value.split(",", 1)[1]
            if value.startswith("data:application/pdf;base64,")
            else value
        )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raw = None
    result = {
        "type": "document",
        "media_type": "application/pdf",
        "attachment": "omitted",
    }
    if raw is not None:
        result.update(sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
    for key in ("filename", "page_count", "text_char_count"):
        if key in metadata and isinstance(metadata[key], (str, int)):
            result[key] = metadata[key]
    return result


def _without_pdf_bytes(value: Any) -> Any:
    """Keep trace capture bounded and cycle safe, including BinaryContent objects."""
    seen: set[int] = set()
    remaining = 4096

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 12:
            return TRUNCATED
        if isinstance(item, str):
            if item.startswith("data:application/pdf;base64,"):
                return json.dumps(_pdf_metadata(item))
            if "BinaryContent(" in item and "application/pdf" in item:
                return "[PDF binary attachment omitted]"
            # Some diagnostics already serialize request content as JSON text.
            if item.startswith(("{", "[")) and any(
                marker in item for marker in ("application/pdf", "__pdf_b64__")
            ):
                if len(item) > MAX_ROW_BYTES:
                    return "[PDF attachment omitted; oversized diagnostic]"
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    return "[PDF attachment omitted; malformed diagnostic]"
                else:
                    safe = visit(decoded, depth + 1)
                    if safe != decoded:
                        return json.dumps(safe, ensure_ascii=False)
            return item
        if isinstance(item, Mapping):
            if id(item) in seen:
                return CYCLE
            seen.add(id(item))
            try:
                if "__pdf_b64__" in item:
                    return _pdf_metadata(item["__pdf_b64__"], item)
                if item.get("media_type") == "application/pdf" and "data" in item:
                    return _pdf_metadata(item["data"], item)
                if item.get("type") == "document" and isinstance(
                    item.get("source"), Mapping
                ):
                    source = item["source"]
                    if source.get("media_type") == "application/pdf":
                        return _pdf_metadata(source.get("data"), item)
                # OpenAI file objects carry an explicit PDF data URL.
                if isinstance(item.get("file_data"), str) and item[
                    "file_data"
                ].startswith("data:application/pdf;base64,"):
                    return _pdf_metadata(item["file_data"], item)
                result = {
                    key: visit(child, depth + 1)
                    for key, child in islice(item.items(), 256)
                }
                if len(item) > 256:
                    result[TRUNCATED] = TRUNCATED
                return result
            finally:
                seen.remove(id(item))
        if isinstance(item, (list, tuple)):
            if id(item) in seen:
                return CYCLE
            seen.add(id(item))
            try:
                result = [visit(child, depth + 1) for child in item[:256]]
                if len(item) > 256:
                    result.append(TRUNCATED)
                return result
            finally:
                seen.remove(id(item))
        # pydantic-ai's BinaryContent is a dataclass; generic repr includes bytes.
        if getattr(item, "media_type", None) == "application/pdf":
            return _pdf_metadata(getattr(item, "data", None))
        if is_dataclass(item) and not isinstance(item, type):
            if id(item) in seen:
                return CYCLE
            seen.add(id(item))
            try:
                return {
                    f.name: visit(getattr(item, f.name), depth + 1)
                    for f in fields(item)[:256]
                }
            finally:
                seen.remove(id(item))
        return item

    return visit(value)


def redact(obj: Any, *, secret_values: tuple[str, ...] = ()) -> Any:
    """Return the bounded, credential-redacted representation used by traces."""
    return redact_value(
        _without_pdf_bytes(obj),
        secret_values=secret_values,
        max_string_chars=MAX_CAPTURED_STR,
    )


def _part(
    value: Any,
    secrets: tuple[str, ...],
    *,
    chars: int = MAX_CAPTURED_STR,
    truncated: list[bool] | None = None,
) -> Any:
    # Redact once, then apply the trace string limit while recording actual
    # omissions. Ordinary ellipses in snippets or model prose are just text.
    safe = redact_value(
        _without_pdf_bytes(value),
        secret_values=secrets,
        max_string_chars=MAX_REDACTION_INPUT_CHARS,
    )
    clipped = False

    def clip(item: Any) -> Any:
        nonlocal clipped
        if isinstance(item, str) and len(item) > chars:
            clipped = True
            return item[: max(chars - 1, 0)] + ("…" if chars else "")
        if isinstance(item, dict):
            return {clip(key): clip(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clip(child) for child in item]
        return item

    result = clip(safe)
    if truncated is not None:
        truncated[0] |= clipped or _was_truncated(result)
    return result


def _was_truncated(value: Any) -> bool:
    if isinstance(value, str):
        return value in (TRUNCATED, CYCLE)
    if isinstance(value, dict):
        return any(_was_truncated(k) or _was_truncated(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_was_truncated(item) for item in value)
    return False


def _parts(
    values: dict, secrets: tuple[str, ...], *, truncated: list[bool] | None = None
) -> dict:
    result = {}
    for key, value in values.items():
        result.update(_part({key: value}, secrets, truncated=truncated))
    return result


def _redact_call(call: dict, secrets: tuple[str, ...], *, body: bool) -> dict:
    # Each body has its own traversal budget. In particular an accumulated
    # conversation must not consume the budget of later calls or tool steps.
    truncated = [False]
    result = {
        key: _part(call[key], secrets, chars=500, truncated=truncated)
        for key in _CALL_HEADER_KEYS
        if key in call
    }
    for key in _CALL_BODY_KEYS:
        if key in call:
            if body:
                result[key] = (
                    _parts(call[key], secrets, truncated=truncated)
                    if key == "request" and isinstance(call[key], dict)
                    else _part(call[key], secrets, truncated=truncated)
                )
            else:
                result["truncated"] = True
    if truncated[0]:
        result["truncated"] = True
    return result


def _redact_row(record: dict, secrets: tuple[str, ...] = ()) -> dict:
    truncated = [False]
    result = _parts(
        {
            key: value
            for key, value in record.items()
            if key not in ("calls", "research")
        },
        secrets,
        truncated=truncated,
    )
    calls = record.get("calls")
    if isinstance(calls, list):
        result["calls"] = [
            _redact_call(call, secrets, body=i < MAX_CALL_BODIES)
            for i, call in enumerate(calls[:MAX_CALL_HEADERS])
            if isinstance(call, dict)
        ]
        if len(calls) > MAX_CALL_BODIES:
            result["truncated"] = True
    research = record.get("research")
    if isinstance(research, dict):
        steps = research.get("steps")
        if isinstance(steps, list):
            safe_steps = []
            for step in steps[:24]:
                if not isinstance(step, dict):
                    continue
                step_truncated = [False]
                safe = _part(
                    {k: v for k, v in step.items() if k not in ("text", "results")},
                    secrets,
                    truncated=step_truncated,
                )
                for key, chars in (("query", 500), ("url", 2000), ("error", 500)):
                    if key in step:
                        safe[key] = _part(
                            step[key], secrets, chars=chars, truncated=step_truncated
                        )
                if isinstance(step.get("results"), list):
                    safe["results"] = [
                        {
                            key: _part(
                                hit.get(key),
                                secrets,
                                chars=chars,
                                truncated=step_truncated,
                            )
                            for key, chars in (
                                ("title", 300),
                                ("url", 2000),
                                ("snippet", 1000),
                            )
                        }
                        for hit in step["results"][:6]
                        if isinstance(hit, dict)
                    ]
                    if len(step["results"]) > 6:
                        safe["truncated"] = True
                    if len(safe["results"]) < min(len(step["results"]), 6):
                        safe["truncated"] = True
                if "text" in step:
                    safe["text"] = _part(
                        step["text"], secrets, chars=8000, truncated=step_truncated
                    )
                if step_truncated[0]:
                    safe["truncated"] = True
                safe_steps.append(safe)
            result["research"] = {
                "steps": safe_steps,
                "truncated": bool(research.get("truncated"))
                or len(steps) > 24
                or len(safe_steps) < min(len(steps), 24)
                or any(step.get("truncated") for step in safe_steps),
            }
        else:
            result["research"] = _part(research, secrets, truncated=truncated)
    if (
        truncated[0]
        or _was_truncated(result)
        or any(call.get("truncated") for call in result.get("calls", []))
        or (
            isinstance(result.get("research"), dict)
            and result["research"].get("truncated")
        )
    ):
        result["truncated"] = True
    return _limit_row_bytes(result)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _research_steps(record: dict) -> list[dict]:
    research = record.get("research")
    steps = research.get("steps") if isinstance(research, dict) else None
    return (
        [step for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else []
    )


def _limit_row_bytes(record: dict) -> dict:
    def fits() -> bool:
        size = 0
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(
            record
        ):
            size += len(chunk.encode("utf-8"))
            if size > MAX_ROW_BYTES:
                return False
        return True

    if fits():
        return record
    record["truncated"] = True
    # Summaries duplicate call bodies; preserve actual calls and tool activity.
    for key in ("prompt", "raw_response"):
        if record.get(key) is not None:
            record[key] = None
            if fits():
                return record
    for call in record.get("calls", []):
        for key in _CALL_BODY_KEYS:
            call.pop(key, None)
        call["truncated"] = True
        if fits():
            return record
    for step in _research_steps(record):
        for key in ("text", "results"):
            step.pop(key, None)
        step["truncated"] = True
        record["research"]["truncated"] = True
        if fits():
            return record
    # Defensive fallback for malformed callers with oversized unknown fields.
    for key in tuple(record):
        if key not in ("kind", "row_id", "trace_id", "calls", "research", "truncated"):
            record.pop(key)
            if fits():
                return record
    # A generic action may have hundreds of calls. Keep their headers, using
    # smaller marked excerpts only after all model/tool bodies were omitted.
    for call in record.get("calls", []):
        for key, value in tuple(call.items()):
            if isinstance(value, str):
                call[key] = _part(value, (), chars=64)
            elif isinstance(value, (dict, list)):
                call[key] = TRUNCATED
        call["truncated"] = True
    for step in _research_steps(record):
        for key, value in tuple(step.items()):
            if isinstance(value, str):
                step[key] = _part(value, (), chars=128)
        step["truncated"] = True
    if not fits():
        raise ValueError("trace headers exceed the serialized row bound")
    return record


def raw_response_of(
    resp: LLMResponse, *, secret_values: tuple[str, ...] = ()
) -> str | None:
    if resp.content:
        return resp.content
    if resp.tool_calls:
        return None
    if resp.raw:
        return json.dumps(redact(resp.raw, secret_values=secret_values))
    if resp.data is not None:
        return json.dumps(redact(resp.data, secret_values=secret_values))
    return None


@dataclass
class RowTrace:
    """Accumulate every model call made for one row."""

    row_id: int | None
    trace_id: str
    calls: list[dict] = field(default_factory=list)
    research: dict | None = None
    started: float = field(default_factory=time.perf_counter)

    def record(
        self,
        *,
        data: Any = None,
        meta: dict | None = None,
        error: str | None = None,
        secret_values: tuple[str, ...] = (),
    ) -> dict:
        meta = meta or {}
        first = self.calls[0] if self.calls else {}
        last = self.calls[-1] if self.calls else {}
        first_request = first.get("request")
        prompt = first.get("prompt")
        if prompt is None and isinstance(first_request, dict):
            prompt = first_request.get("messages")
        retries: list[dict] = []
        for call in self.calls:
            for event in call.get("events", []):
                if (
                    isinstance(event, dict)
                    and event.get("event") == "attempt"
                    and event.get("outcome") == "error"
                ):
                    retries.append(event)
            if call.get("error"):
                retries.append(
                    {
                        "event": "call_failed",
                        "error": call["error"],
                        "schema_violation": call.get("schema_violation", False),
                    }
                )
        return _redact_row(
            {
                "kind": "row",
                "row_id": self.row_id,
                "trace_id": self.trace_id,
                "prompt": prompt,
                "raw_response": last.get("raw_response"),
                "data": data,
                "error": error,
                "tokens_in": meta.get("tokens_in"),
                "tokens_out": meta.get("tokens_out"),
                "cost": meta.get("cost"),
                "cached": bool(last.get("cached")),
                "latency_ms": int((time.perf_counter() - self.started) * 1000),
                "retries": retries,
                "calls": self.calls,
                **({"research": self.research} if self.research is not None else {}),
            },
            secret_values,
        )


class TracingRouter:
    """Per-row router proxy that captures model calls without owning outcomes."""

    def __init__(self, inner: ModelRouterTransport, row: RowTrace):
        self._inner = inner
        self._row = row
        self._observed_models: list[str] = []
        self._extra_secret_values: list[str] = []

    def add_secret_values(self, values: Iterable[str]) -> None:
        """Accept only secrets already selected by the admitted capability."""
        try:
            for index, value in enumerate(values):
                if index >= 64 or len(self._extra_secret_values) >= 64:
                    break
                if (
                    isinstance(value, str)
                    and value
                    and value not in self._extra_secret_values
                ):
                    self._extra_secret_values.append(value)
        except Exception:  # noqa: BLE001 - optional capture must not affect execution
            logger.warning("could not attach trace redaction values")

    def set_research(self, snapshot: dict) -> None:
        try:
            self._row.research = snapshot
        except Exception:  # noqa: BLE001 - optional capture must not affect execution
            logger.warning("could not attach research trace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def adapter_for(self, provider: str) -> Any:
        self._remember_model(provider)
        return self._inner.adapter_for(provider)

    def resolve_local_model(self, model: str) -> tuple[Any, Any, str]:
        self._remember_model(model)
        return self._inner.resolve_local_model(model)

    def _remember_model(self, model: str) -> None:
        if (
            model
            and model not in self._observed_models
            and len(self._observed_models) < MAX_OBSERVED_TRACE_PROVIDERS
        ):
            self._observed_models.append(model)

    def _selected_secret_values(self, model: str) -> tuple[str, ...]:
        self._remember_model(model)
        return self._inner.secret_values_for_model(model)

    def secret_values_for_model(self, model: str) -> tuple[str, ...]:
        return self._selected_secret_values(model)

    def note_schema_reject(self, provider: str) -> None:
        self._inner.note_schema_reject(provider)

    def record(
        self, *, data: Any = None, meta: dict | None = None, error: str | None = None
    ) -> dict | None:
        try:
            secret_values = tuple(self._extra_secret_values) + tuple(
                secret
                for model in tuple(self._observed_models)
                for secret in self._selected_secret_values(model)
            )
            return self._row.record(
                data=data,
                meta=meta,
                error=error,
                secret_values=secret_values,
            )
        except Exception:  # noqa: BLE001 - tracing must never change a run
            logger.warning("dropping row trace after capture failure")
            return None

    async def complete(
        self,
        req: LLMRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        return await self._record_complete(
            req,
            recipe_version=recipe_version,
            complete=self._inner.complete,
            events=trace,
        )

    async def complete_transport(
        self,
        req: LLMRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        return await self._record_complete(
            req,
            recipe_version=recipe_version,
            complete=self._inner.complete_transport,
            events=trace,
        )

    async def _record_complete(
        self,
        req: LLMRequest,
        *,
        recipe_version: str,
        complete: _ModelCompletionCall,
        events: ModelTrace | None = None,
    ) -> LLMResponse:
        events = events if events is not None else []
        entry: dict[str, Any] | None = None
        capture_body = len(self._row.calls) < MAX_CALL_BODIES
        truncated = [False]
        secret_values: tuple[str, ...] = ()
        try:
            secret_values = tuple(
                self._extra_secret_values
            ) + self._selected_secret_values(req.model)
            messages = [
                {
                    **{
                        key: value
                        for key, value in message.items()
                        if key != "provider_details"
                    },
                    **(
                        {
                            "tool_calls": [
                                {key: call.get(key) for key in ("name", "id", "args")}
                                for call in message["tool_calls"][:256]
                                if isinstance(call, dict)
                            ]
                        }
                        if isinstance(message.get("tool_calls"), list)
                        else {}
                    ),
                }
                for message in (req.messages[:256] if capture_body else [])
            ]
            entry = {
                "model": req.model,
                "request": {
                    key: _part(value, secret_values, truncated=truncated)
                    for key, value in {
                        "model": req.model,
                        "max_tokens": req.max_tokens,
                        "temperature": req.temperature,
                        "params": req.params,
                        "mechanism": req.mechanism,
                        "reasoning_policy": req.reasoning_policy,
                        "tools": req.tools,
                        "schema": req.schema,
                        "messages": messages,
                    }.items()
                },
                "events": [],
            }
            if len(req.messages) > 256 or truncated[0]:
                entry["truncated"] = True
            if not capture_body:
                entry = {"model": req.model, "truncated": True}
            self._row.calls.append(entry)
        except Exception:  # noqa: BLE001 - provider call remains authoritative
            logger.warning("dropping model-call trace after capture failure")
        started = time.perf_counter()
        try:
            response = await complete(req, recipe_version=recipe_version, trace=events)
        except BaseException as exc:  # record cancellation too; preserve original
            try:
                if entry is not None:
                    entry["latency_ms"] = int((time.perf_counter() - started) * 1000)
                    if capture_body:
                        entry["events"] = _part(
                            events, secret_values, truncated=truncated
                        )
                    entry["error"] = safe_error(
                        "llm_call_failed",
                        exc,
                        secret_values=secret_values,
                        max_chars=500,
                    ).text
                    if isinstance(exc, SchemaViolation):
                        entry["schema_violation"] = True
                        if capture_body:
                            entry["raw_response"] = _part(
                                exc.raw_text, secret_values, truncated=truncated
                            )
                    if truncated[0]:
                        entry["truncated"] = True
            except Exception:  # noqa: BLE001 - preserve provider exception
                logger.warning("dropping failed-call trace details")
            raise
        try:
            if entry is not None:
                entry["latency_ms"] = int((time.perf_counter() - started) * 1000)
                if capture_body:
                    entry["events"] = _part(events, secret_values, truncated=truncated)
                    entry["raw_response"] = _part(
                        raw_response_of(response, secret_values=secret_values),
                        secret_values,
                        truncated=truncated,
                    )
                    entry["data"] = _part(
                        response.data, secret_values, truncated=truncated
                    )
                    entry["tool_calls"] = _part(
                        [
                            {key: call.get(key) for key in ("name", "id", "args")}
                            for call in (response.tool_calls or [])[:256]
                        ],
                        secret_values,
                        truncated=truncated,
                    )
                if len(response.tool_calls or []) > 256 or truncated[0]:
                    entry["truncated"] = True
                entry["tokens_in"] = response.tokens_in
                entry["tokens_out"] = response.tokens_out
                entry["cost"] = response.cost
                entry["cached"] = response.cached
        except Exception:  # noqa: BLE001 - preserve successful provider response
            logger.warning("dropping successful-call trace details")
        return response


class TraceWriter:
    """Lazy, locked gzip JSONL writer for one run."""

    def __init__(
        self,
        path: Path,
        run_id: int,
        trace_id: str,
        *,
        action_kind: str | None,
        model: str | None,
        meta_written: bool,
    ):
        self.path = path
        self.run_id = run_id
        self.trace_id = trace_id
        self.action_kind = action_kind
        self.model = model
        self._meta_written = meta_written
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls,
        bundle_path: str | Path,
        run_id: int,
        *,
        action_kind: str | None = None,
        model: str | None = None,
    ) -> "TraceWriter":
        path = trace_path(bundle_path, run_id)
        trace_id = uuid.uuid4().hex
        meta_written = False
        try:
            if path.exists():
                first = next(_iter_trace_records(path), None)
                if first and first.get("kind") == "meta":
                    existing = first.get("trace_id")
                    if isinstance(existing, str) and existing:
                        trace_id = existing
                    meta_written = True
        except Exception:  # noqa: BLE001 - tracing remains optional
            logger.warning("could not inspect existing run trace")
        return cls(
            path,
            run_id,
            trace_id,
            action_kind=action_kind,
            model=model,
            meta_written=meta_written,
        )

    def row_trace(self, row_id: int | None) -> RowTrace:
        return RowTrace(row_id=row_id, trace_id=self.trace_id)

    def write_row(self, record: dict | None) -> None:
        if not record or (not record.get("calls") and record.get("research") is None):
            return
        try:
            row_line = self._record_line(record)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(self.path, "at", encoding="utf-8") as stream:
                    if not self._meta_written:
                        stream.write(
                            self._record_line(
                                {
                                    "kind": "meta",
                                    "run_id": self.run_id,
                                    "trace_id": self.trace_id,
                                    "action_kind": self.action_kind,
                                    "model": self.model,
                                    "created_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                }
                            )
                        )
                        self._meta_written = True
                    stream.write(row_line)
        except Exception:  # noqa: BLE001 - trace persistence is fail-open
            logger.warning("dropping row trace after write failure")

    @staticmethod
    def _record_line(record: dict) -> str:
        safe_record = (
            _redact_row(record) if record.get("kind") == "row" else redact(record)
        )
        return _json(safe_record) + "\n"


def _iter_trace_records(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line, parse_constant=_invalid_json_constant)
            if not isinstance(record, dict):
                raise ValueError("trace record must be a JSON object")
            yield record


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def _normalize_research(value: Any) -> tuple[dict | None, bool]:
    """Admit old partial diagnostics into the closed HTTP research contract."""
    if value is None:
        return None, False
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        return None, True
    incomplete = bool(set(value) - {"steps", "truncated"})
    if "truncated" in value and type(value["truncated"]) is not bool:
        incomplete = True
    steps = []
    for raw in value["steps"][:24]:
        if (
            not isinstance(raw, dict)
            or raw.get("tool") not in ("search", "fetch")
            or raw.get("status") not in ("ok", "error", "cancelled", "started")
        ):
            incomplete = True
            continue
        tool = raw["tool"]
        identity = "query" if tool == "search" else "url"
        if not isinstance(raw.get(identity), str):
            incomplete = True
            continue
        step: dict[str, Any] = {"tool": tool, "status": raw["status"]}
        allowed = {"tool", "status", "duration_ms", "error_code", "error", "truncated"}
        strings = {
            identity: 500 if tool == "search" else 2000,
            "error_code": 500,
            "error": 500,
        }
        strings.update(
            {"provider": 500, "service": 500} if tool == "search" else {"text": 8000}
        )
        allowed.update(strings)
        if tool == "search":
            allowed.add("results")
        step_incomplete = bool(set(raw) - allowed)
        for key, limit in strings.items():
            item = raw.get(key)
            if item is None:
                continue
            if not isinstance(item, str):
                step_incomplete = True
                continue
            step[key] = _part(item, (), chars=limit)
            step_incomplete |= len(item) > limit or item in (TRUNCATED, CYCLE)
        if raw.get("duration_ms") is not None:
            if type(raw["duration_ms"]) is int and raw["duration_ms"] >= 0:
                step["duration_ms"] = raw["duration_ms"]
            else:
                step_incomplete = True
        if tool == "search":
            results = raw.get("results", [])
            step["results"] = []
            if not isinstance(results, list):
                step_incomplete = True
            else:
                step_incomplete |= len(results) > 6
                for hit in results[:6]:
                    if not isinstance(hit, dict) or not all(
                        isinstance(hit.get(key), str)
                        for key in ("title", "url", "snippet")
                    ):
                        step_incomplete = True
                        continue
                    step_incomplete |= bool(set(hit) - {"title", "url", "snippet"})
                    result = {}
                    for key, limit in (
                        ("title", 300),
                        ("url", 2000),
                        ("snippet", 1000),
                    ):
                        result[key] = _part(hit[key], (), chars=limit)
                        step_incomplete |= len(hit[key]) > limit or hit[key] in (
                            TRUNCATED,
                            CYCLE,
                        )
                    step["results"].append(result)
        if "truncated" in raw and type(raw["truncated"]) is not bool:
            step_incomplete = True
        step["truncated"] = raw.get("truncated") is True or step_incomplete
        incomplete |= step_incomplete
        steps.append(step)
    incomplete |= len(value["steps"]) > 24
    if value["steps"] and not steps:
        return None, True
    return {
        "steps": steps,
        "truncated": value.get("truncated") is True or incomplete,
    }, incomplete


def normalize_trace_row(value: dict) -> dict:
    """Drop malformed legacy fields before a strict response model sees them."""
    row: dict[str, Any] = {}
    truncated = [False]
    for key in ("trace_id", "raw_response", "error"):
        if value.get(key) is None or isinstance(value[key], str):
            if key in value:
                row[key] = (
                    _part(value[key], (), truncated=truncated)
                    if key == "raw_response"
                    else value[key]
                )
    for key in ("row_id", "tokens_in", "tokens_out", "latency_ms"):
        if value.get(key) is None or type(value[key]) is int:
            if key in value:
                row[key] = value[key]
    cost = value.get("cost")
    if cost is None or (type(cost) in (int, float) and math.isfinite(cost)):
        if "cost" in value:
            row["cost"] = cost
    for key in ("cached", "truncated"):
        if type(value.get(key)) is bool:
            row[key] = value[key]
    for key in ("prompt", "data"):
        if key in value:
            row[key] = _part(value[key], (), truncated=truncated)
    if value.get("calls") is None:
        if "calls" in value:
            row["calls"] = None
    elif isinstance(value["calls"], list):
        row["calls"] = [
            _redact_call(call, (), body=i < MAX_CALL_BODIES)
            for i, call in enumerate(value["calls"][:MAX_CALL_HEADERS])
            if isinstance(call, dict)
        ]
    if value.get("retries") is None or isinstance(value["retries"], list):
        if "retries" in value:
            row["retries"] = _part(value["retries"], (), truncated=truncated)
    if truncated[0] or _was_truncated(row) or TRUNCATED in value:
        row["truncated"] = True
    research, incomplete = _normalize_research(value.get("research"))
    row["research"] = research
    row["research_incomplete"] = incomplete or value.get("research_incomplete") is True
    return row


def read_trace(bundle_path: str | Path, run_id: int) -> dict | None:
    """Load a run trace, or return None when it is absent or unreadable."""
    path = trace_path(bundle_path, run_id)
    if not path.exists():
        return None
    meta: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    try:
        for record in _iter_trace_records(path):
            if record.get("kind") == "meta":
                meta = dict(record)
            elif record.get("kind") == "row":
                row = normalize_trace_row(record)
                rows.append(row)
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError):
        logger.warning("could not read run trace")
        return None
    return {
        "run_id": run_id,
        "trace_id": meta.get("trace_id"),
        "action_kind": meta.get("action_kind"),
        "model": meta.get("model"),
        "created_at": meta.get("created_at"),
        "rows": rows,
    }


def _same_row_id(left: Any, right: int) -> bool:
    try:
        return int(left) == right
    except (TypeError, ValueError, OverflowError):
        return False


def read_trace_row(bundle_path: str | Path, run_id: int, row_id: int) -> dict | None:
    """Scan a run's compressed log for the latest record for ``row_id``."""
    path = trace_path(bundle_path, run_id)
    if not path.exists():
        return None
    meta: dict[str, Any] = {}
    row: dict[str, Any] | None = None
    record_count = 0
    try:
        for record in _iter_trace_records(path):
            if record.get("kind") == "meta":
                meta = dict(record)
            elif record.get("kind") == "row" and _same_row_id(
                record.get("row_id"), row_id
            ):
                row = normalize_trace_row(record)
                record_count += 1
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError):
        logger.warning("could not scan run trace")
        return None
    return {
        "run_id": run_id,
        "trace_id": meta.get("trace_id")
        if isinstance(meta.get("trace_id"), str)
        else None,
        "action_kind": meta.get("action_kind")
        if isinstance(meta.get("action_kind"), str)
        else None,
        "model": meta.get("model") if isinstance(meta.get("model"), str) else None,
        "created_at": meta.get("created_at")
        if isinstance(meta.get("created_at"), str)
        else None,
        "row": row,
        "record_count": record_count,
    }


def read_trace_row_retries(
    bundle_path: str | Path, run_id: int, row_ids: Iterable[int]
) -> dict[int, list[dict[str, Any]]]:
    """Scan once for the latest retry diagnostics for the requested rows."""
    targets: set[int] = set()
    for value in row_ids:
        try:
            row_id = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if row_id > 0:
            targets.add(row_id)
    path = trace_path(bundle_path, run_id)
    if not targets or not path.exists():
        return {}
    retries_by_row: dict[int, list[dict[str, Any]]] = {}
    try:
        for record in _iter_trace_records(path):
            if record.get("kind") != "row":
                continue
            try:
                row_id = int(record.get("row_id"))
            except (TypeError, ValueError):
                continue
            if row_id not in targets:
                continue
            retries = record.get("retries")
            retries_by_row[row_id] = (
                [dict(item) for item in retries if isinstance(item, dict)]
                if isinstance(retries, list)
                else []
            )
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError):
        logger.warning("could not scan run trace retries")
        return {}
    return retries_by_row

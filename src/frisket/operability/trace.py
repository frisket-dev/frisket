"""Best-effort model-call traces stored outside the project database.

Each run that reaches a model gets one gzip JSONL file under ``traces/``.
Trace capture is diagnostic only: capture, serialization, storage, and read
failures never change action results. Deterministic runs create no trace file.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Iterable, Protocol

from frisket.ai.llm.types import (
    LLMRequest,
    LLMResponse,
    ModelRouterTransport,
    ModelTrace,
    SchemaViolation,
)
from frisket.redaction import DEFAULT_TRACE_STRING_MAX_CHARS, redact_value, safe_error

MAX_CAPTURED_STR = 4096
MAX_OBSERVED_TRACE_PROVIDERS = 64
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


def redact(obj: Any, *, secret_values: tuple[str, ...] = ()) -> Any:
    """Return the bounded, credential-redacted representation used by traces."""
    return redact_value(
        obj,
        secret_values=secret_values,
        max_string_chars=MAX_CAPTURED_STR,
    )


def raw_response_of(
    resp: LLMResponse, *, secret_values: tuple[str, ...] = ()
) -> str | None:
    if resp.content:
        return resp.content
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
        retries: list[dict] = []
        for call in self.calls:
            for event in call.get("events", []):
                if event.get("event") == "attempt" and event.get("outcome") == "error":
                    retries.append(event)
            if call.get("error"):
                retries.append(
                    {
                        "event": "call_failed",
                        "error": call["error"],
                        "schema_violation": call.get("schema_violation", False),
                    }
                )
        return redact(
            {
                "kind": "row",
                "row_id": self.row_id,
                "trace_id": self.trace_id,
                "prompt": first.get("prompt"),
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
            },
            secret_values=secret_values,
        )


class TracingRouter:
    """Per-row router proxy that captures model calls without owning outcomes."""

    def __init__(self, inner: ModelRouterTransport, row: RowTrace):
        self._inner = inner
        self._row = row
        self._observed_models: list[str] = []

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
            secret_values = tuple(
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
        secret_values: tuple[str, ...] = ()
        try:
            secret_values = self._selected_secret_values(req.model)
            entry = {
                "model": req.model,
                "prompt": redact(req.messages, secret_values=secret_values),
                "events": [],
            }
            self._row.calls.append(entry)
        except Exception:  # noqa: BLE001 - provider call remains authoritative
            logger.warning("dropping model-call trace after capture failure")
        started = time.perf_counter()
        try:
            response = await complete(req, recipe_version=recipe_version, trace=events)
        except Exception as exc:  # noqa: BLE001 - record and preserve original
            try:
                if entry is not None:
                    entry["latency_ms"] = int((time.perf_counter() - started) * 1000)
                    entry["events"] = redact(events, secret_values=secret_values)
                    entry["error"] = safe_error(
                        "llm_call_failed",
                        exc,
                        secret_values=secret_values,
                        max_chars=500,
                    ).text
                    if isinstance(exc, SchemaViolation):
                        entry["schema_violation"] = True
                        entry["raw_response"] = redact(
                            exc.raw_text, secret_values=secret_values
                        )
            except Exception:  # noqa: BLE001 - preserve provider exception
                logger.warning("dropping failed-call trace details")
            raise
        try:
            if entry is not None:
                entry["latency_ms"] = int((time.perf_counter() - started) * 1000)
                entry["events"] = redact(events, secret_values=secret_values)
                entry["raw_response"] = redact(
                    raw_response_of(response, secret_values=secret_values),
                    secret_values=secret_values,
                )
                entry["data"] = redact(response.data, secret_values=secret_values)
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
        if not record or not record.get("calls"):
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
        safe_record = redact_value(
            record,
            max_string_chars=DEFAULT_TRACE_STRING_MAX_CHARS,
        )
        return json.dumps(safe_record, allow_nan=False) + "\n"


def _iter_trace_records(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("trace record must be a JSON object")
            yield record


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
                row = dict(record)
                row.pop("kind", None)
                rows.append(row)
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError):
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
    except (TypeError, ValueError):
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
                row = dict(record)
                row.pop("kind", None)
                record_count += 1
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError):
        logger.warning("could not scan run trace")
        return None
    return {
        "run_id": run_id,
        "trace_id": meta.get("trace_id"),
        "action_kind": meta.get("action_kind"),
        "model": meta.get("model"),
        "created_at": meta.get("created_at"),
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
        except (TypeError, ValueError):
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
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError):
        logger.warning("could not scan run trace retries")
        return {}
    return retries_by_row

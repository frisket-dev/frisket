"""Structured JSON logging for backend request and worker context.

Message arguments pass through the central redactor before interpolation and
the rendered text is redacted again, so this module stays a write-boundary
for secrets. Formatting failures from ordinary bugs (mismatched args, a buggy
``__str__``, a malformed record) fall back to bounded markers so logging never
crashes the app. It does not defend against hostile objects — an attacker who
can hand the formatter one already executes in this process.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TextIO

from frisket.redaction import (
    DEFAULT_LOG_STRING_MAX_CHARS,
    MAX_SAFE_FRAMES,
    REDACTED as REDACTED,
    SafeFrame,
    canonical_error_code,
    redact_text,
    redact_value,
    safe_error,
    safe_stack_frames,
)

LOG_LEVEL_ENV = "FRISKET_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"

REQUEST_ID_HEADER = "x-request-id"
TRACE_ID_HEADER = "x-trace-id"
ALERT_LOG_SCHEMA = "frisket.alert.v1"

_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "frisket_log_context"
)
_LEVEL_OVERRIDE: str | int | None = None

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}
_ALERT_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_ALERT_ROUTING_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ALERT_SEVERITIES = {"info", "warning", "error", "critical"}
_FORMATTER_FAILURE_JSON = '{"level":"ERROR","message":"[LOG FORMAT FAILED]"}'

_LOG_RECORD_ATTRS = set(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"message", "asctime"}


def normalize_log_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    raw = (level or DEFAULT_LOG_LEVEL).strip().upper()
    if raw.isdigit():
        return int(raw)
    return _LEVELS.get(raw, logging.INFO)


def current_log_level() -> int:
    level = (
        _LEVEL_OVERRIDE
        if _LEVEL_OVERRIDE is not None
        else os.environ.get(LOG_LEVEL_ENV)
    )
    return normalize_log_level(level)


def set_log_verbosity(level: str | int | None) -> int:
    """Update process-local log verbosity without rebuilding handlers.

    Passing ``None`` clears the override and returns to ``FRISKET_LOG_LEVEL``.
    """
    global _LEVEL_OVERRIDE
    _LEVEL_OVERRIDE = level
    return current_log_level()


def redact(value: Any) -> Any:
    """Compatibility wrapper around the central cycle-safe redactor."""
    return redact_value(value)


def current_log_context() -> dict[str, Any]:
    return dict(_CONTEXT.get({}))


def correlation_log_payload() -> dict[str, Any]:
    """Fields safe to copy into queued job payloads for log correlation."""
    ctx = _CONTEXT.get({})
    return {
        key: ctx[key] for key in ("trace_id", "request_id") if ctx.get(key) is not None
    }


def _validate_alert_name(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _ALERT_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be lower snake_case")
    return normalized


def _validate_alert_routing_component(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _ALERT_ROUTING_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be lowercase routing text")
    return normalized


def alert_log_extra(
    event: str,
    *,
    owner: str,
    category: str,
    severity: str = "error",
    **fields: Any,
) -> dict[str, Any]:
    """Build the standard extra payload for alertable structured log records."""
    event_name = _validate_alert_name(event, field="event")
    alert_owner = _validate_alert_routing_component(owner, field="owner")
    alert_category = _validate_alert_routing_component(category, field="category")
    alert_severity = severity.strip().lower()
    if alert_severity not in _ALERT_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(_ALERT_SEVERITIES)}")

    payload = {key: value for key, value in fields.items() if value is not None}
    payload.update(
        {
            "event": event_name,
            "alert": True,
            "alert_schema": ALERT_LOG_SCHEMA,
            "alert_owner": alert_owner,
            "alert_category": alert_category,
            "alert_severity": alert_severity,
            "alert_routing_key": (
                f"{alert_owner}.{alert_category}.{alert_severity}.{event_name}"
            ),
        }
    )
    return payload


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    clean = {key: value for key, value in fields.items() if value is not None}
    prior = _CONTEXT.get({})
    merged = {**prior, **clean}
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def job_log_context(job_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {"job_id": job_id}
    for key in (
        "trace_id",
        "request_id",
        "run_id",
        "project_id",
        "sheet_id",
        "source_id",
        "row_id",
    ):
        if payload.get(key) is not None:
            context[key] = payload[key]
    return context


def _safe_format_argument(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    safe = redact_value(value, max_string_chars=DEFAULT_LOG_STRING_MAX_CHARS)
    if safe is None or isinstance(safe, (bool, int, float, str)):
        return safe
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_log_message(record: logging.LogRecord) -> str:
    """Render the record message with arguments redacted before interpolation.

    Every argument passes through the central redactor before ``%`` formatting
    and the rendered text is redacted again, so secrets are replaced whether
    they arrive as arguments or only appear after interpolation. Ordinary
    formatting bugs (mismatched args, malformed templates, a buggy ``__str__``)
    fall back to a bounded marker; logging never raises.
    """
    try:
        if isinstance(record.msg, str):
            template = record.msg
        else:
            template_value = _safe_format_argument(record.msg)
            template = (
                template_value
                if isinstance(template_value, str)
                else str(template_value)
            )
        if len(template) > DEFAULT_LOG_STRING_MAX_CHARS:
            template = f"{template[: DEFAULT_LOG_STRING_MAX_CHARS - 1]}…"
        if not record.args:
            rendered = template
        elif isinstance(record.args, Mapping):
            safe_mapping = redact_value(
                record.args,
                max_string_chars=DEFAULT_LOG_STRING_MAX_CHARS,
            )
            arguments: object = {
                key: _safe_format_argument(value) for key, value in safe_mapping.items()
            }
            rendered = template % arguments
        elif isinstance(record.args, tuple):
            rendered = template % tuple(
                _safe_format_argument(value) for value in record.args
            )
        else:
            rendered = template % _safe_format_argument(record.args)
        return redact_text(
            rendered,
            max_chars=DEFAULT_LOG_STRING_MAX_CHARS,
            one_line=False,
        )
    except Exception:  # noqa: BLE001 - logging must never mask the real failure
        return "[UNFORMATTABLE LOG MESSAGE]"


class RuntimeLevelFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("frisket") and record.levelno < logging.WARNING:
            return False
        return record.levelno >= current_log_level()


class _UvicornAccessRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            safe_args: list[object] = []
            for index, value in enumerate(record.args):
                if isinstance(value, str):
                    if index == 2:
                        value = value.partition("?")[0]
                    value = redact_text(
                        value,
                        max_chars=DEFAULT_LOG_STRING_MAX_CHARS,
                        one_line=False,
                    )
                safe_args.append(value)
            record.args = tuple(safe_args)
        return True


_UVICORN_ACCESS_REDACTION_FILTER = _UvicornAccessRedactionFilter()
# Uvicorn owns a non-propagating handler, so the root JSON formatter cannot
# protect its request target. Logger filters survive handler reconfiguration.
logging.getLogger("uvicorn.access").addFilter(_UVICORN_ACCESS_REDACTION_FILTER)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format_record(record)
        except Exception:
            # Final logging boundary: a formatter bug must not crash the app,
            # and the fallback stays constant so it cannot itself fail.
            return _FORMATTER_FAILURE_JSON

    def _format_record(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_log_message(record),
        }
        payload.update(current_log_context())
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        supplied_frames = payload.pop("exception_frames", ())
        exception_frames = _safe_frame_payloads(supplied_frames)
        if record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, BaseException):
                safe = safe_error(
                    "unhandled_exception",
                    exc,
                    include_frames=True,
                )
                payload["exception"] = safe.detail
                if safe.exception_type:
                    payload["exception_type"] = safe.exception_type
                exception_frames = _safe_frame_payloads(safe.frames)
                if not exception_frames:
                    exception_frames = _safe_frame_payloads(
                        safe_stack_frames(record.exc_info[2])
                    )
            else:
                safe = safe_error("unhandled_exception", "operation failed")
                payload["exception"] = safe.detail
            payload["error_code"] = canonical_error_code(
                payload.get("error_code"),
                fallback="unhandled_exception",
            )
        if exception_frames:
            payload["exception_frames"] = exception_frames
        # ``record.stack_info`` is a preformatted traceback string. It is
        # intentionally omitted; structured frames above are the only stack
        # representation this formatter supports.
        return json.dumps(
            redact_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _safe_frame_payloads(value: object) -> list[dict[str, object]]:
    """Serialize frames produced by ``safe_stack_frames``.

    The reserved ``exception_frames`` field carries only ``SafeFrame`` items —
    the producer already sanitizes paths and text. Anything else in the field
    is a caller bug and is dropped rather than serialized.
    """
    candidates: tuple[object, ...] | list[object]
    if isinstance(value, SafeFrame):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []
    frames: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, SafeFrame):
            continue
        frames.append(
            {
                "module": candidate.module,
                "path": candidate.path,
                "function": candidate.function,
                "line": candidate.line,
            }
        )
        if len(frames) == MAX_SAFE_FRAMES:
            break
    return frames


def _structured_handler(stream: TextIO | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(RuntimeLevelFilter())
    handler._frisket_structured = True  # type: ignore[attr-defined]
    return handler


def configure_logging(
    *, level: str | int | None = None, stream: TextIO | None = None, force: bool = False
) -> None:
    if level is not None:
        set_log_verbosity(level)

    root = logging.getLogger()
    if force:
        root.handlers = []

    if not any(
        getattr(handler, "_frisket_structured", False) for handler in root.handlers
    ):
        root.addHandler(_structured_handler(stream))

    # Keep frisket logger thresholds permissive; RuntimeLevelFilter owns
    # verbosity so FRISKET_LOG_LEVEL or set_log_verbosity() can change it while
    # running without opening low-level third-party loggers.
    logging.getLogger("frisket").setLevel(logging.DEBUG)
    logging.captureWarnings(True)


def install_fastapi_logging(app: Any, *, logger_name: str = "frisket.server") -> None:
    """Attach request log middleware.

    This intentionally does not install handlers. Reusable app factories can
    emit standard logging records without forcing stderr output in tests or
    manifest probes; runtime entrypoints call ``configure_logging()``.
    """

    logger = logging.getLogger(logger_name)

    @app.middleware("http")
    async def structured_logging_middleware(request: Any, call_next: Any) -> Any:
        request_id = (
            request.headers.get(REQUEST_ID_HEADER)
            or request.headers.get("x-correlation-id")
            or uuid.uuid4().hex
        )
        trace_id = request.headers.get(TRACE_ID_HEADER) or request_id
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        started = time.perf_counter()
        with bind_log_context(request_id=request_id, trace_id=trace_id):
            logger.info(
                "http_request_started",
                extra={
                    "event": "http_request_started",
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            try:
                response = await call_next(request)
            except Exception as exc:
                safe = safe_error(
                    "http_request_failed",
                    exc,
                    include_frames=True,
                )
                logger.error(
                    "http_request_failed",
                    extra={
                        "event": "http_request_failed",
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error_code": safe.code,
                        "error": safe.text,
                        "exception_type": safe.exception_type,
                        "exception_frames": safe.frames,
                    },
                    exc_info=False,
                )
                raise
            if REQUEST_ID_HEADER not in response.headers:
                response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "http_request_completed",
                extra={
                    "event": "http_request_completed",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            return response

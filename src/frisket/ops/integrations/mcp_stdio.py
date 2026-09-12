from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO, TypeVar

from mcp import ClientSession, McpError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field

from frisket.redaction import redact_text
from frisket.runtime.supervisor import guarded_argv


_DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_FRAME_BYTES = 1_048_576
_DEFAULT_MAX_TEXT_CHARS = 32_768
_DEFAULT_MAX_RESULT_BYTES = 262_144
_DEFAULT_MAX_STDERR_CHARS = 8_192
_MAX_TOOL_PAGES = 100
_MAX_TOOLS = 512

_T = TypeVar("_T")


@dataclass(frozen=True)
class McpStdioServerConfig:
    """Direct-launch configuration for one trusted-local stdio MCP server."""

    command: str
    args: tuple[str, ...] = ()
    cwd: str | Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    secret_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("MCP command must not be empty")
        object.__setattr__(self, "args", tuple(str(value) for value in self.args))
        object.__setattr__(
            self,
            "env",
            {str(name): str(value) for name, value in self.env.items()},
        )
        object.__setattr__(
            self,
            "secret_values",
            tuple(value for value in self.secret_values if value),
        )


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpToolResult:
    tool_name: str
    is_error: bool
    content: list[dict[str, Any]]
    structured_content: dict[str, Any] | None
    truncated: bool


class McpStdioError(RuntimeError):
    """A bounded, credential-redacted failure at the outbound MCP boundary."""

    def __init__(
        self,
        code: str,
        *,
        detail: str | None = None,
        capture: _StderrCapture | None = None,
        secret_values: tuple[str, ...] = (),
        may_have_dispatched: bool = False,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.may_have_dispatched = may_have_dispatched
        self.retryable = retryable
        self._detail = detail
        self._capture = capture
        self._secret_values = secret_values
        super().__init__(code)

    @property
    def diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "code": self.code,
            "may_have_dispatched": self.may_have_dispatched,
            "retryable": self.retryable,
        }
        if self._detail:
            diagnostics["detail"] = redact_text(
                self._detail,
                secret_values=self._secret_values,
                max_chars=500,
            )
        if self._capture is not None:
            stderr, truncated = self._capture.snapshot()
            if stderr:
                safe_stderr = redact_text(
                    stderr,
                    secret_values=self._secret_values,
                    max_chars=self._capture.retained_chars,
                    one_line=False,
                )
                diagnostics["stderr"] = safe_stderr[: self._capture.max_chars]
            if truncated:
                diagnostics["stderr_truncated"] = True
        return diagnostics


class _RawCallToolResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    content: list[Any] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = Field(
        default=None,
        alias="structuredContent",
    )
    is_error: bool = Field(default=False, alias="isError")


class _ProtocolMonitor:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.error: Exception | None = None

    async def handle(self, message: Any) -> None:
        if isinstance(message, Exception) and self.error is None:
            self.error = message
            self.event.set()


class _ProtocolFailure(RuntimeError):
    pass


class _StderrCapture:
    """A draining pipe that retains only a bounded stderr prefix."""

    def __init__(self, max_chars: int, secret_values: tuple[str, ...]) -> None:
        self.max_chars = max_chars
        lookahead = max((len(value) - 1 for value in secret_values), default=0)
        self.retained_chars = max_chars + lookahead
        self._read_fd, write_fd = os.pipe()
        self.writer: TextIO = os.fdopen(write_fd, "w", encoding="utf-8")
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._length = 0
        self._truncated = False
        self._task = asyncio.create_task(asyncio.to_thread(self._drain))

    def _drain(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = os.read(self._read_fd, 8_192)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        self._append(tail)
                    return
                self._append(decoder.decode(chunk))
        finally:
            os.close(self._read_fd)

    def _append(self, text: str) -> None:
        with self._lock:
            remaining = self.retained_chars - self._length
            if remaining > 0:
                retained = text[:remaining]
                self._parts.append(retained)
                self._length += len(retained)
            if len(text) > remaining:
                self._truncated = True

    def snapshot(self) -> tuple[str, bool]:
        with self._lock:
            retained = "".join(self._parts)
            return retained, self._truncated or len(retained) > self.max_chars

    async def aclose(self) -> None:
        if not self.writer.closed:
            self.writer.close()
        try:
            await asyncio.wait_for(self._task, timeout=1)
        except TimeoutError:  # pragma: no cover - the writer is closed above
            self._task.cancel()


async def _await_sdk_request(
    request: Awaitable[_T],
    monitor: _ProtocolMonitor,
    *,
    timeout_seconds: float,
) -> _T:
    request_task = asyncio.ensure_future(request)
    protocol_task = asyncio.create_task(monitor.event.wait())
    try:
        done, _pending = await asyncio.wait(
            {request_task, protocol_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError
        if protocol_task in done and monitor.error is not None:
            raise _ProtocolFailure(str(monitor.error)) from monitor.error
        return await request_task
    except BaseException:
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        raise
    finally:
        if not protocol_task.done():
            protocol_task.cancel()
        await asyncio.gather(protocol_task, return_exceptions=True)


class McpStdioSession:
    def __init__(
        self,
        sdk_session: ClientSession,
        tools: list[McpTool],
        *,
        monitor: _ProtocolMonitor,
        capture: _StderrCapture,
        secret_values: tuple[str, ...],
        request_timeout_seconds: float,
        max_frame_bytes: int,
        max_text_chars: int,
        max_result_bytes: int,
    ) -> None:
        self._sdk_session = sdk_session
        self._monitor = monitor
        self._capture = capture
        self._secret_values = secret_values
        self._request_timeout_seconds = request_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._max_text_chars = max_text_chars
        self._max_result_bytes = max_result_bytes
        self.tools = tuple(tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> McpToolResult:
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=name,
                    arguments=arguments,
                )
            )
        )
        try:
            raw = await _await_sdk_request(
                self._sdk_session.send_request(
                    request,
                    _RawCallToolResult,
                    request_read_timeout_seconds=timedelta(
                        seconds=self._request_timeout_seconds
                    ),
                ),
                self._monitor,
                timeout_seconds=self._request_timeout_seconds,
            )
            self._require_frame_bound(raw.model_dump(by_alias=True))
            return self._normalize_result(name, raw)
        except asyncio.CancelledError:
            raise McpStdioError(
                "mcp_call_cancelled",
                capture=self._capture,
                secret_values=self._secret_values,
                may_have_dispatched=True,
                retryable=False,
            ) from None
        except McpStdioError:
            raise
        except BaseException as exc:
            raise _mapped_error(
                exc,
                capture=self._capture,
                secret_values=self._secret_values,
                may_have_dispatched=True,
            ) from None

    def _require_frame_bound(self, value: Any) -> None:
        if len(_canonical_bytes(value)) > self._max_frame_bytes:
            raise McpStdioError(
                "mcp_frame_too_large",
                capture=self._capture,
                secret_values=self._secret_values,
                may_have_dispatched=True,
                retryable=False,
            )

    def _normalize_result(
        self,
        tool_name: str,
        raw: _RawCallToolResult,
    ) -> McpToolResult:
        content: list[dict[str, Any]] = []
        truncated = False
        remaining = self._max_result_bytes

        structured_content = raw.structured_content
        if structured_content is not None:
            structured_size = len(_canonical_bytes(structured_content))
            if structured_size > remaining:
                structured_content = None
                truncated = True
            else:
                remaining -= structured_size

        for block in raw.content:
            normalized, block_truncated = _normalize_content_block(
                block,
                max_text_chars=self._max_text_chars,
            )
            normalized_size = len(_canonical_bytes(normalized))
            if normalized_size > remaining:
                truncated = True
                break
            content.append(normalized)
            remaining -= normalized_size
            truncated = truncated or block_truncated

        return McpToolResult(
            tool_name=tool_name,
            is_error=raw.is_error,
            content=content,
            structured_content=structured_content,
            truncated=truncated,
        )


class McpStdioClient:
    def __init__(
        self,
        config: McpStdioServerConfig,
        *,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        max_text_chars: int = _DEFAULT_MAX_TEXT_CHARS,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        max_stderr_chars: int = _DEFAULT_MAX_STDERR_CHARS,
    ) -> None:
        self.config = config
        self.startup_timeout_seconds = _positive(
            startup_timeout_seconds,
            "startup_timeout_seconds",
        )
        self.request_timeout_seconds = _positive(
            request_timeout_seconds,
            "request_timeout_seconds",
        )
        # The pinned SDK owns JSON framing for trusted-local code, so this is a
        # post-decode semantic admission bound rather than a parser allocation
        # limit.  Keeping it here prevents oversized schemas/results from
        # reaching the model-facing layer without replacing the SDK transport.
        self.max_frame_bytes = _positive_int(max_frame_bytes, "max_frame_bytes")
        self.max_text_chars = _positive_int(max_text_chars, "max_text_chars")
        self.max_result_bytes = _positive_int(max_result_bytes, "max_result_bytes")
        self.max_stderr_chars = _positive_int(max_stderr_chars, "max_stderr_chars")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[McpStdioSession]:
        capture = _StderrCapture(
            self.max_stderr_chars,
            self.config.secret_values,
        )
        transport_stack = AsyncExitStack()
        session_stack = AsyncExitStack()
        monitor = _ProtocolMonitor()
        transport_read_stream: Any | None = None
        sdk_session: ClientSession | None = None
        try:
            (
                transport_read_stream,
                write_stream,
            ) = await transport_stack.enter_async_context(
                stdio_client(
                    _server_parameters(self.config),
                    errlog=capture.writer,
                )
            )
            # The SDK session closes the streams it receives. Keep the transport's
            # receive endpoint for a teardown-only drain after that session closes.
            sdk_session = await session_stack.enter_async_context(
                ClientSession(
                    transport_read_stream.clone(),
                    write_stream,
                    read_timeout_seconds=timedelta(
                        seconds=self.request_timeout_seconds
                    ),
                    message_handler=monitor.handle,
                )
            )
            tools = await self._initialize_and_discover(sdk_session, monitor)
        except asyncio.CancelledError:
            await _close_session_resources(
                transport_stack,
                session_stack,
                capture,
                transport_read_stream,
            )
            raise
        except BaseException as exc:
            await _close_session_resources(
                transport_stack,
                session_stack,
                capture,
                transport_read_stream,
            )
            raise _mapped_error(
                exc,
                capture=capture,
                secret_values=self.config.secret_values,
            ) from None

        assert sdk_session is not None
        public_session = McpStdioSession(
            sdk_session,
            tools,
            monitor=monitor,
            capture=capture,
            secret_values=self.config.secret_values,
            request_timeout_seconds=self.request_timeout_seconds,
            max_frame_bytes=self.max_frame_bytes,
            max_text_chars=self.max_text_chars,
            max_result_bytes=self.max_result_bytes,
        )
        try:
            yield public_session
        finally:
            await _close_session_resources(
                transport_stack,
                session_stack,
                capture,
                transport_read_stream,
            )

    async def _initialize_and_discover(
        self,
        sdk_session: ClientSession,
        monitor: _ProtocolMonitor,
    ) -> list[McpTool]:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds

        async def startup_request(request: Awaitable[_T]) -> _T:
            remaining = deadline - asyncio.get_running_loop().time()
            timeout = min(self.request_timeout_seconds, max(remaining, 0.0))
            if timeout <= 0:
                raise TimeoutError
            return await _await_sdk_request(
                request,
                monitor,
                timeout_seconds=timeout,
            )

        await startup_request(sdk_session.initialize())
        discovered: list[McpTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(_MAX_TOOL_PAGES):
            page = await startup_request(sdk_session.list_tools(cursor=cursor))
            if (
                len(_canonical_bytes(page.model_dump(by_alias=True)))
                > self.max_frame_bytes
            ):
                raise McpStdioError(
                    "mcp_frame_too_large",
                    capture=None,
                    secret_values=self.config.secret_values,
                )
            for tool in page.tools:
                discovered.append(
                    McpTool(
                        name=tool.name,
                        description=tool.description,
                        input_schema=dict(tool.inputSchema),
                    )
                )
                if len(discovered) > _MAX_TOOLS:
                    raise McpStdioError("mcp_tool_count_exceeded")
            cursor = page.nextCursor
            if cursor is None:
                return discovered
            if cursor in seen_cursors:
                raise McpStdioError("mcp_protocol_error", detail="repeated cursor")
            seen_cursors.add(cursor)
        raise McpStdioError("mcp_tool_page_limit_exceeded")


async def _close_session_resources(
    transport_stack: AsyncExitStack,
    session_stack: AsyncExitStack,
    capture: _StderrCapture,
    transport_read_stream: Any | None,
) -> None:
    """Drain late stdout while the transport closes its child process."""
    drain_task: asyncio.Task[None] | None = None
    drain_ready = asyncio.Event()
    try:
        await session_stack.aclose()
    finally:
        try:
            if transport_read_stream is not None:
                drain_task = asyncio.create_task(
                    _drain_transport_read_stream(
                        transport_read_stream,
                        drain_ready,
                    )
                )
                await drain_ready.wait()
            await transport_stack.aclose()
        finally:
            try:
                if drain_task is not None:
                    await drain_task
            finally:
                await capture.aclose()


async def _drain_transport_read_stream(
    stream: Any,
    ready: asyncio.Event,
) -> None:
    ready.set()
    async with stream:
        async for _message in stream:
            pass


def _server_parameters(config: McpStdioServerConfig) -> StdioServerParameters:
    command = config.command
    args = list(config.args)
    if os.name == "posix":
        command, *args = guarded_argv([config.command, *config.args])
    return StdioServerParameters(
        command=command,
        args=args,
        cwd=config.cwd,
        env=dict(config.env),
    )


def _normalize_content_block(
    value: Any,
    *,
    max_text_chars: int,
) -> tuple[dict[str, Any], bool]:
    block = value if isinstance(value, dict) else {"type": "unknown", "value": value}
    content_type = str(block.get("type") or "unknown")
    if content_type == "text" and isinstance(block.get("text"), str):
        text = block["text"]
        truncated = len(text) > max_text_chars
        return {
            "type": "text",
            "text": text[:max_text_chars],
            "truncated": truncated,
        }, truncated
    if content_type == "resource" and isinstance(block.get("resource"), dict):
        resource = block["resource"]
        if isinstance(resource.get("text"), str):
            text = resource["text"]
            truncated = len(text) > max_text_chars
            return {
                "type": "text_resource",
                "uri": str(resource.get("uri") or ""),
                "mime_type": resource.get("mimeType"),
                "text": text[:max_text_chars],
                "truncated": truncated,
            }, truncated
        return _binary_descriptor(
            "resource",
            resource.get("mimeType"),
            resource.get("blob"),
        ), False
    if content_type in {"image", "audio"}:
        return _binary_descriptor(
            content_type,
            block.get("mimeType"),
            block.get("data"),
        ), False
    if content_type == "resource_link":
        return {
            "type": "resource_link",
            "name": block.get("name"),
            "uri": block.get("uri"),
            "mime_type": block.get("mimeType"),
            "size_bytes": block.get("size"),
        }, False
    return {
        "type": "unknown_descriptor",
        "content_type": content_type,
        "mime_type": block.get("mimeType"),
        "size_bytes": len(_canonical_bytes(block)),
        "sha256": hashlib.sha256(_canonical_bytes(block)).hexdigest(),
    }, False


def _binary_descriptor(
    content_type: str,
    mime_type: Any,
    encoded: Any,
) -> dict[str, Any]:
    raw = str(encoded or "").encode("utf-8")
    try:
        raw = base64.b64decode(raw, validate=True)
    except ValueError:
        pass
    return {
        "type": "binary_descriptor",
        "content_type": content_type,
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _mapped_error(
    exc: BaseException,
    *,
    capture: _StderrCapture | None,
    secret_values: tuple[str, ...],
    may_have_dispatched: bool = False,
) -> McpStdioError:
    if isinstance(exc, McpStdioError):
        if exc._capture is None:
            exc._capture = capture
        if may_have_dispatched:
            exc.may_have_dispatched = True
            exc.retryable = False
        return exc
    flattened = _flatten_exception(exc)
    rendered = " ".join(str(item) for item in flattened if str(item))
    if any(isinstance(item, _ProtocolFailure) for item in flattened):
        code = "mcp_protocol_error"
    elif any(
        isinstance(item, TimeoutError | asyncio.TimeoutError) for item in flattened
    ):
        code = "mcp_request_timeout"
    elif any(isinstance(item, OSError) for item in flattened):
        code = "mcp_spawn_failed"
    elif any(isinstance(item, McpError) for item in flattened):
        lowered = rendered.lower()
        code = (
            "mcp_session_eof"
            if "connection closed" in lowered
            else "mcp_request_failed"
        )
    else:
        lowered = rendered.lower()
        code = (
            "mcp_session_eof"
            if "connection closed" in lowered
            else "mcp_protocol_error"
        )
    return McpStdioError(
        code,
        detail=rendered or type(exc).__name__,
        capture=capture,
        secret_values=secret_values,
        may_have_dispatched=may_have_dispatched,
        retryable=False,
    )


def _flatten_exception(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        flattened: list[BaseException] = []
        for child in exc.exceptions:
            flattened.extend(_flatten_exception(child))
        return flattened
    return [exc]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _positive(value: float, name: str) -> float:
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be positive")
    return normalized


def _positive_int(value: int, name: str) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be positive")
    return normalized


__all__ = [
    "McpStdioClient",
    "McpStdioError",
    "McpStdioServerConfig",
    "McpStdioSession",
    "McpTool",
    "McpToolResult",
]

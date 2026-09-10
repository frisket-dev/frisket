"""Sandbox-delegating host client for the trusted-local plugin RPC boundary.

The client owns plugin wire framing only. Callers resolve plugin policy and
domain inputs before constructing the typed requests accepted here; the shared
sandbox shim remains the sole owner of process supervision and teardown.
"""

from __future__ import annotations

import asyncio
import copy
import json
import queue
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import TypeAdapter, ValidationError

from frisket.contracts import plugin_rpc as rpc
from frisket.engine.sandbox import shim


_T = TypeVar("_T")
_RunSandboxed = Callable[..., Awaitable[shim.SandboxResult]]
_RunSandboxedStdoutLines = Callable[..., Awaitable[shim.SandboxResult]]
_ShouldCancel = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class PluginProcessFailure:
    """The single host-side shape for process and protocol failures."""

    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = copy.deepcopy(self.details)
        return {
            "status": "failed",
            "outputs": [],
            "errors": [error],
            "warnings": [],
        }


class PluginProcessError(RuntimeError):
    """A supervised child or its stdout violated the RPC contract."""

    def __init__(self, failure: PluginProcessFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def run_sandboxed_sync(
    runner: _RunSandboxed,
    argv: list[str],
    **kwargs: Any,
) -> shim.SandboxResult:
    """Bridge a synchronous host call to the sandbox async entry point.

    This bridge imposes no second timeout. Wall/cancel/tree-kill behavior
    remains wholly inside engine.sandbox.shim.
    """

    async def invoke() -> shim.SandboxResult:
        return await runner(argv, **kwargs)

    return _run_awaitable_sync(invoke)


def _run_awaitable_sync(factory: Callable[[], Awaitable[_T]]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: _T | None = None
    error: BaseException | None = None

    def target() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller
            error = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    assert result is not None
    return result


class PluginProcessClient:
    """One-process-per-invocation transport peer for subprocess_runner."""

    def __init__(
        self,
        plugin_root: str | Path,
        *,
        run_sandboxed_call: _RunSandboxed | None = None,
        run_sandboxed_stdout_lines_call: _RunSandboxedStdoutLines | None = None,
    ) -> None:
        self._plugin_root = str(plugin_root)
        self._run_sandboxed_override = run_sandboxed_call
        self._run_sandboxed_stdout_lines_override = run_sandboxed_stdout_lines_call

    @staticmethod
    def sandbox_policy(env: dict[str, str]) -> shim.SandboxPolicy:
        """Build plugin policy without resolving or widening caller env."""

        return shim.SandboxPolicy(
            allow_network=True,
            wall_seconds=rpc.PLUGIN_ACTION_WALL_SECONDS,
            allowed_extra_env=sorted(env),
        )

    @staticmethod
    def response_payload(response: rpc.PluginRpcModel) -> dict[str, Any]:
        payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload.pop("mode", None)
        return payload

    @staticmethod
    def frame_payload(frame: rpc.PluginRpcModel) -> dict[str, Any]:
        return frame.model_dump(mode="json", by_alias=True, exclude_none=True)

    @staticmethod
    def request_payload(request: rpc.PluginRpcModel) -> dict[str, Any]:
        """Serialize one concrete typed request for its matching entrypoint."""

        return request.model_dump(mode="json", by_alias=True)

    def importer(
        self,
        request: rpc.ImporterRequest,
        env: dict[str, str],
        *,
        should_cancel: _ShouldCancel | None = None,
    ) -> rpc.ImporterFrameStream:
        return self._stream(
            mode="importer",
            argv=self._argv("--importer"),
            request=request,
            env=env,
            adapter=TypeAdapter(rpc.ImporterFrame),
            should_cancel=should_cancel,
        )

    def projection(
        self,
        request: rpc.ProjectionRequest,
        env: dict[str, str],
        *,
        should_cancel: _ShouldCancel | None = None,
    ) -> rpc.ProjectionFrameStream:
        return self._stream(
            mode="projection",
            argv=self._argv("--projection"),
            request=request,
            env=env,
            adapter=TypeAdapter(rpc.ProjectionFrame),
            should_cancel=should_cancel,
        )

    def operator(
        self,
        request: rpc.OperatorRequest,
        env: dict[str, str],
        *,
        should_cancel: _ShouldCancel | None = None,
    ) -> rpc.OperatorResponse:
        result = self._run_single(
            argv=self._argv("--operator"),
            request=request,
            env=env,
            should_cancel=should_cancel,
        )
        response = self._validate_single_response(result, mode="operator")
        assert isinstance(response, rpc.OperatorResponse)
        return response

    def _argv(self, mode_flag: str | None = None) -> list[str]:
        argv = [sys.executable, "-m", "frisket.plugins.subprocess_runner"]
        if mode_flag is not None:
            argv.append(mode_flag)
        argv.append(self._plugin_root)
        return argv

    def _run_single(
        self,
        *,
        argv: list[str],
        request: rpc.PluginRpcModel,
        env: dict[str, str],
        should_cancel: _ShouldCancel | None,
    ) -> shim.SandboxResult:
        return run_sandboxed_sync(
            self._invoke_sandboxed,
            argv,
            policy=self.sandbox_policy(env),
            stdin_data=json.dumps(
                self.request_payload(request), separators=(",", ":")
            ).encode("utf-8"),
            extra_env=env,
            should_cancel=should_cancel,
        )

    async def _invoke_sandboxed(
        self,
        argv: list[str],
        **kwargs: Any,
    ) -> shim.SandboxResult:
        if self._run_sandboxed_override is not None:
            return await self._run_sandboxed_override(argv, **kwargs)
        return await shim.run_sandboxed(argv, **kwargs)

    async def _invoke_sandboxed_stdout_lines(
        self,
        argv: list[str],
        **kwargs: Any,
    ) -> shim.SandboxResult:
        if self._run_sandboxed_stdout_lines_override is not None:
            return await self._run_sandboxed_stdout_lines_override(argv, **kwargs)
        return await shim.run_sandboxed_stdout_lines(argv, **kwargs)

    def _validate_single_response(
        self,
        result: shim.SandboxResult,
        *,
        mode: Literal["operator"],
    ) -> rpc.OperatorResponse:
        payload = _single_json_payload(result)
        payload["mode"] = mode
        try:
            response = rpc.OperatorResponse.model_validate(payload)
        except ValidationError as exc:
            raise _error(
                "plugin_subprocess_invalid_response",
                "Trusted-local plugin subprocess returned invalid data",
            ) from exc
        return response

    def _stream(
        self,
        *,
        mode: Literal["importer", "projection"],
        argv: list[str],
        request: rpc.PluginRpcModel,
        env: dict[str, str],
        adapter: TypeAdapter[Any],
        should_cancel: _ShouldCancel | None,
    ) -> Iterator[Any]:
        stdin_data = json.dumps(
            self.request_payload(request), separators=(",", ":")
        ).encode("utf-8")
        return self._stream_bytes(
            mode=mode,
            argv=argv,
            stdin_data=stdin_data,
            env=env,
            adapter=adapter,
            should_cancel=should_cancel,
        )

    def _stream_bytes(
        self,
        *,
        mode: Literal["importer", "projection"],
        argv: list[str],
        stdin_data: bytes,
        env: dict[str, str],
        adapter: TypeAdapter[Any],
        should_cancel: _ShouldCancel | None,
    ) -> Iterator[Any]:
        events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
        closed = threading.Event()
        teardown_error: shim.SandboxTeardownError | None = None

        def put_event(kind: str, value: Any) -> None:
            while not closed.is_set():
                try:
                    events.put((kind, value), timeout=0.05)
                    return
                except queue.Full:
                    continue

        def cancelled() -> bool:
            return closed.is_set() or bool(should_cancel and should_cancel())

        def worker() -> None:
            nonlocal teardown_error

            async def invoke() -> shim.SandboxResult:
                return await self._invoke_sandboxed_stdout_lines(
                    argv,
                    policy=self.sandbox_policy(env),
                    on_stdout_line=lambda line: put_event("line", line),
                    stdin_data=stdin_data,
                    extra_env=env,
                    should_cancel=cancelled,
                )

            try:
                put_event("result", asyncio.run(invoke()))
            except shim.SandboxTeardownError as exc:
                # Closing a prefix stops queue delivery, not infrastructure
                # failure reporting. The joining consumer must still see this.
                teardown_error = exc
                put_event("error", exc)
            except BaseException as exc:  # noqa: BLE001 - delivered to consumer
                put_event("error", exc)

        thread = threading.Thread(target=worker)

        def iterate() -> Iterator[Any]:
            state = _StreamState(mode=mode)
            thread.start()
            try:
                while True:
                    kind, value = events.get()
                    if kind == "error":
                        assert isinstance(value, BaseException)
                        if isinstance(value, shim.SandboxTeardownError):
                            raise value
                        raise _error(
                            "plugin_subprocess_failed",
                            "Trusted-local plugin subprocess failed",
                        ) from value
                    if kind == "result":
                        result = value
                        if not result.ok:
                            raise _child_failure(result)
                        state.finish()
                        return
                    assert kind == "line"
                    line = value
                    if not isinstance(line, str) or not line.strip():
                        raise _invalid_stream_json(mode)
                    try:
                        raw = json.loads(line)
                    except ValueError as exc:
                        raise _invalid_stream_json(mode) from exc
                    if not isinstance(raw, dict):
                        raise _invalid_stream_frame(mode)
                    try:
                        frame = adapter.validate_python(raw)
                    except ValidationError as exc:
                        raise _invalid_stream_frame(mode) from exc
                    state.accept(frame)
                    yield frame
            finally:
                closed.set()
                if thread.is_alive():
                    thread.join()
                if teardown_error is not None:
                    raise teardown_error

        return iterate()


@dataclass(slots=True)
class _StreamState:
    mode: Literal["importer", "projection"]
    terminal: bool = False
    saw_schema: bool = False
    row_count: int = 0

    def accept(self, frame: rpc.PluginRpcModel) -> None:
        if self.terminal:
            raise _invalid_stream_frame(self.mode)
        if isinstance(frame, rpc.TableSchemaFrame):
            if self.saw_schema or self.row_count:
                raise _invalid_stream_frame(self.mode)
            self.saw_schema = True
        elif isinstance(frame, rpc.TableRowFrame):
            if not self.saw_schema:
                raise _invalid_stream_frame(self.mode)
            self.row_count += 1
        elif isinstance(frame, rpc.TableDoneFrame):
            if not self.saw_schema or frame.row_count != self.row_count:
                raise _invalid_stream_frame(self.mode)
            self.terminal = True
        elif isinstance(frame, rpc.TableErrorFrame):
            self.terminal = True
        elif isinstance(frame, rpc.ProjectionTimelineItemFrame):
            pass
        elif isinstance(frame, (rpc.ProjectionDoneFrame, rpc.ProjectionErrorFrame)):
            self.terminal = True

    def finish(self) -> None:
        if not self.terminal:
            messages = {
                "importer": "Trusted-local plugin importer did not finish the row stream",
                "projection": (
                    "Trusted-local plugin projection did not finish the timeline stream"
                ),
            }
            raise _error(
                "plugin_subprocess_invalid_response",
                messages[self.mode],
                field="response",
            )


def _single_json_payload(result: shim.SandboxResult) -> dict[str, Any]:
    if not result.ok:
        raise _child_failure(result)
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise _error(
            "plugin_subprocess_invalid_json",
            "Trusted-local plugin subprocess returned invalid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise _error(
            "plugin_subprocess_invalid_response",
            "Trusted-local plugin subprocess returned invalid data",
        )
    return payload


def _child_failure(result: shim.SandboxResult) -> PluginProcessError:
    if result.cancelled:
        return _error(
            "action_cancelled",
            "Trusted-local plugin subprocess was cancelled",
        )
    return _error(
        "plugin_subprocess_failed",
        "Trusted-local plugin subprocess failed",
    )


def _invalid_stream_json(
    mode: Literal["importer", "projection"],
) -> PluginProcessError:
    messages = {
        "importer": "Trusted-local plugin importer returned invalid NDJSON",
        "projection": "Trusted-local plugin projection returned invalid NDJSON",
    }
    return _error(
        "plugin_subprocess_invalid_json",
        messages[mode],
        field="response",
    )


def _invalid_stream_frame(
    mode: Literal["importer", "projection"],
) -> PluginProcessError:
    messages = {
        "importer": "Trusted-local plugin importer returned invalid frame",
        "projection": "Trusted-local plugin projection returned invalid frame",
    }
    return _error(
        "plugin_subprocess_invalid_response",
        messages[mode],
        field="response",
    )


def _error(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> PluginProcessError:
    return PluginProcessError(
        PluginProcessFailure(
            code=code,
            message=message,
            field=field,
            details=details,
        )
    )

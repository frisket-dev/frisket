"""Child-process entrypoint for plugin importers, operators, and projections."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import time
import traceback
from collections.abc import Iterable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar

from frisket.contracts.plugin_rpc import (
    ImporterRequest,
    OperatorRequest,
    ProjectionRequest,
    RUNTIME_OPERATOR_PLAN_SCHEMA_VERSION,
)
from frisket.plugins.sdk import (
    Plugin,
    PluginDependencyMissing,
    PluginImportSource,
    PluginImporterContext,
    PluginImporterError,
    PluginOperatorContext,
    PluginProjectionContext,
    PluginRateLimited,
    PluginUserError,
)

_RequestT = TypeVar(
    "_RequestT",
    ImporterRequest,
    ProjectionRequest,
    OperatorRequest,
)

# The debug relay is enabled only by the test-backend CLI; production dispatch never
# sets it, preserving redacted responses.
PLUGIN_TEST_BACKEND_DEBUG_ENV_VAR = "FRISKET_PLUGIN_TEST_BACKEND_DEBUG"


def _debug_traceback() -> str | None:
    """The currently-handled exception's traceback text, gated behind
    PLUGIN_TEST_BACKEND_DEBUG_ENV_VAR. Must only be called from inside an
    `except` block."""
    if not os.environ.get(PLUGIN_TEST_BACKEND_DEBUG_ENV_VAR):
        return None
    return traceback.format_exc()


def _importer_error_frame(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "type": "error",
        "error": error,
    }


def _projection_error_frame(code: str, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"code": code, "message": message},
    }


def _write_frame(frame: dict[str, Any]) -> None:
    print(json.dumps(frame, separators=(",", ":"), sort_keys=True), flush=True)


def _retry_after_ms(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0, int(float(text) * 1000))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    try:
        delta = parsed.timestamp() - time.time()
    except Exception:
        return None
    return max(0, int(delta * 1000))


def _error_from_exception(exc: Exception) -> dict[str, Any]:
    """Map a handler exception onto the redacted child error envelope.

    Allowlisted SDK exception types (`PluginDependencyMissing`,
    `PluginUserError`) surface their message verbatim; `PluginRateLimited`
    surfaces retry metadata only; every other exception is redacted to the
    generic message — a buggy or malicious plugin must not leak arbitrary
    details (secrets, paths, internals) through this boundary.

    """
    details: dict[str, Any] = {}
    debug = _debug_traceback()
    code = "plugin_subprocess_failed"
    message = "Trusted-local plugin subprocess failed"
    if isinstance(exc, PluginDependencyMissing):
        # Allowlisted (see frisket.plugins.sdk.PluginDependencyMissing): by
        # construction its message can only ever be an "install X" hint.
        code = "plugin_dependency_missing"
        message = str(exc)
    elif isinstance(exc, PluginRateLimited):
        code = "plugin_provider_rate_limited"
        message = "External service rate limited the plugin request"
        details["retryable"] = True
        if exc.retry_after_seconds is not None:
            retry_after = _retry_after_ms(exc.retry_after_seconds)
            if retry_after is not None:
                details["retry_after_ms"] = retry_after
    elif isinstance(exc, PluginUserError):
        # Allowlisted (see frisket.plugins.sdk.PluginUserError): the typed
        # family is the explicit safe channel for user-facing messages.
        code = "plugin_user_error"
        message = str(exc)
    if debug:
        details["debug_traceback"] = debug
    return {
        "status": "failed",
        "rows": [],
        "errors": [{"code": code, "message": message, "details": details}],
        "warnings": [],
    }


def _load_request(
    request_type: type[_RequestT],
) -> _RequestT:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        raise ValueError("request stdin must be JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("request stdin must be a JSON object")
    return request_type.model_validate(payload)


def _module_path(plugin_root: Path, module_path: str) -> Path:
    candidate = (plugin_root / module_path).resolve()
    if not candidate.is_relative_to(plugin_root):
        raise ValueError("plugin module path must stay inside the plugin root")
    if candidate.suffix != ".py" or not candidate.is_file():
        raise ValueError("plugin module path must point to a Python file")
    return candidate


def _load_module(plugin_root: Path, module_path: Path) -> Any:
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    root = plugin_root.resolve()
    module_file = module_path.resolve()
    digest = hashlib.sha256(
        f"{root.as_posix()}::{module_file.as_posix()}".encode("utf-8")
    ).hexdigest()[:16]
    module_name = "_frisket_trusted_plugin_" + digest
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ValueError("plugin module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _find_importer_plugin(
    module: Any,
    *,
    kind: str,
    handler_key: str,
    plugin_id: str,
) -> Plugin:
    for value in vars(module).values():
        if isinstance(value, Plugin) and value.importer_for(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            return value
    raise ValueError("plugin importer was not found in the module")


def _find_projection_plugin(
    module: Any,
    *,
    kind: str,
    handler_key: str,
    plugin_id: str,
) -> Plugin:
    for value in vars(module).values():
        if isinstance(value, Plugin) and value.projection_for(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            return value
    raise ValueError("plugin projection was not found in the module")


def _find_operator_plugin(
    module: Any,
    *,
    kind: str,
    handler_key: str,
    plugin_id: str,
) -> Plugin:
    for value in vars(module).values():
        if isinstance(value, Plugin) and value.operator_for(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            return value
    raise ValueError("plugin operator was not found in the module")


def _projection_context(request: ProjectionRequest) -> PluginProjectionContext:
    context = request.context
    return PluginProjectionContext(
        project_id=context.project_id,
        plugin_id=request.plugin_id,
        handler_key=request.handler_key,
        projection_kind=request.projection_kind,
        capabilities=context.capabilities,
    )


def _operator_context(request: OperatorRequest) -> PluginOperatorContext:
    context = request.context
    return PluginOperatorContext(
        project_id=context.project_id,
        plugin_id=request.plugin_id,
        handler_key=request.handler_key,
        operator_kind=request.operator_kind,
        capabilities=context.capabilities,
    )


def _importer_context(request: ImporterRequest) -> PluginImporterContext:
    context = request.context
    return PluginImporterContext(
        project_id=context.project_id,
        plugin_id=request.plugin_id,
        handler_key=request.handler_key,
        importer_kind=request.importer_kind,
        capabilities=context.capabilities,
        diagnostics=[],
        secrets=context.requires_secrets,
    )


async def run_importer_async(plugin_root: Path, request: ImporterRequest) -> None:
    kind = request.importer_kind
    handler_key = request.handler_key
    plugin_id = request.plugin_id
    module_path = _module_path(plugin_root, request.module_path)
    module = _load_module(plugin_root, module_path)
    plugin = _find_importer_plugin(
        module,
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    importer = plugin.importer_for(
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    if importer is None:
        raise ValueError("plugin importer was not found in the module")

    ctx = _importer_context(request)

    _write_frame(
        {
            "type": "schema",
            "columns": list(importer.columns),
        }
    )
    try:
        result = importer.func(
            ctx,
            PluginImportSource(request.source, request.handler_params),
            **dict(request.handler_params),
        )
        if inspect.isawaitable(result):
            result = await result
        row_count = 0
        if inspect.isasyncgen(result) or hasattr(result, "__aiter__"):
            async for row in result:
                row_count += 1
                _write_importer_row(row)
        elif isinstance(result, Iterable) and not isinstance(
            result, (str, bytes, dict)
        ):
            for row in result:
                row_count += 1
                _write_importer_row(row)
        else:
            raise ValueError("plugin importer must return an iterable of rows")
    except PluginImporterError as exc:
        diagnostics = [*ctx.diagnostics, exc.diagnostic]
        _write_frame(
            _importer_error_frame(
                str(exc.diagnostic.get("code") or "plugin_importer_failed"),
                str(exc.diagnostic.get("message") or "Plugin importer failed"),
                details={"diagnostics": diagnostics},
            )
        )
        return
    except Exception as exc:  # noqa: BLE001 - details must stay redacted
        error = _error_from_exception(exc)
        first = error["errors"][0]
        _write_frame(
            _importer_error_frame(
                str(first.get("code") or "plugin_subprocess_failed"),
                str(first.get("message") or "Trusted-local plugin subprocess failed"),
                details=first.get("details")
                if isinstance(first.get("details"), dict)
                else None,
            )
        )
        return

    for diagnostic in ctx.diagnostics:
        _write_frame(
            {
                "type": "diagnostic",
                "diagnostic": diagnostic,
            }
        )
    _write_frame(
        {
            "type": "done",
            "row_count": row_count,
        }
    )


def _write_importer_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("plugin importer yielded a non-object row")
    _write_frame(
        {
            "type": "row",
            "row": row,
        }
    )


async def run_projection_async(plugin_root: Path, request: ProjectionRequest) -> None:
    kind = request.projection_kind
    handler_key = request.handler_key
    plugin_id = request.plugin_id
    module_path = _module_path(plugin_root, request.module_path)
    module = _load_module(plugin_root, module_path)
    plugin = _find_projection_plugin(
        module,
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    projection = plugin.projection_for(
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    if projection is None:
        raise ValueError("plugin projection was not found in the module")
    ctx = _projection_context(request)
    try:
        mode = request.operation
        result = projection.func(
            ctx,
            list(request.rows),
            target=dict(request.target),
            params=dict(request.params),
            **({"mode": mode} if mode in {"status", "build"} else {}),
        )
        if inspect.isawaitable(result):
            result = await result
        if mode in {"status", "build"}:
            if not isinstance(result, dict):
                raise ValueError("plugin projection plan must return an object")
            _write_frame(
                {
                    "type": "plan",
                    "plan": result,
                }
            )
            _write_frame(
                {
                    "type": "done",
                    "metrics": {},
                }
            )
            return
        item_count = 0
        if inspect.isasyncgen(result) or hasattr(result, "__aiter__"):
            async for item in result:
                _write_timeline_item(item)
                item_count += 1
        elif isinstance(result, Iterable) and not isinstance(
            result, (str, bytes, dict)
        ):
            for item in result:
                _write_timeline_item(item)
                item_count += 1
        else:
            raise ValueError("plugin projection must return an iterable of items")
    except Exception as exc:  # noqa: BLE001 - details must stay redacted
        error = _error_from_exception(exc)
        first = error["errors"][0]
        _write_frame(
            {
                "type": "error",
                "error": first,
            }
        )
        return
    _write_frame(
        {
            "type": "done",
            "metrics": {"emittedItemCount": item_count},
        }
    )


async def run_operator_async(
    plugin_root: Path, request: OperatorRequest
) -> dict[str, Any]:
    kind = request.operator_kind
    handler_key = request.handler_key
    plugin_id = request.plugin_id
    module_path = _module_path(plugin_root, request.module_path)
    module = _load_module(plugin_root, module_path)
    plugin = _find_operator_plugin(
        module,
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    operator = plugin.operator_for(
        kind=kind,
        handler_key=handler_key,
        plugin_id=plugin_id,
    )
    if operator is None:
        raise ValueError("plugin operator was not found in the module")
    ctx = _operator_context(request)
    result = operator.func(
        ctx,
        [{"rowId": row.row_id, "value": row.value} for row in request.rows],
        value=request.value,
        target=dict(request.target),
        params=dict(request.params),
    )
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        row_ids = result.get("rowIds", result.get("row_ids"))
    else:
        row_ids = result
    if not isinstance(row_ids, list):
        raise ValueError("plugin operator must return a list of row ids")
    return {
        "status": "completed",
        "plan": {
            "schemaVersion": RUNTIME_OPERATOR_PLAN_SCHEMA_VERSION,
            "rowIds": row_ids,
        },
        "errors": [],
        "warnings": [],
    }


def _write_timeline_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise ValueError("plugin projection yielded a non-object timeline item")
    _write_frame(
        {
            "type": "timeline_item",
            "item": item,
        }
    )


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--importer":
        try:
            plugin_root = Path(sys.argv[2]).resolve()
            if not plugin_root.is_dir():
                raise ValueError("plugin root must be a directory")
            request = _load_request(ImporterRequest)
            asyncio.run(run_importer_async(plugin_root, request))
        except Exception:
            _write_frame(
                _importer_error_frame(
                    "plugin_subprocess_failed",
                    "Trusted-local plugin subprocess failed",
                )
            )
        raise SystemExit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "--projection":
        try:
            plugin_root = Path(sys.argv[2]).resolve()
            if not plugin_root.is_dir():
                raise ValueError("plugin root must be a directory")
            request = _load_request(ProjectionRequest)
            asyncio.run(run_projection_async(plugin_root, request))
        except Exception:
            _write_frame(
                _projection_error_frame(
                    "plugin_subprocess_failed",
                    "Trusted-local plugin projection failed",
                )
            )
        raise SystemExit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "--operator":
        try:
            plugin_root = Path(sys.argv[2]).resolve()
            if not plugin_root.is_dir():
                raise ValueError("plugin root must be a directory")
            request = _load_request(OperatorRequest)
            response = asyncio.run(run_operator_async(plugin_root, request))
        except Exception:
            response = {
                "status": "failed",
                "plan": None,
                "errors": [
                    {
                        "code": "plugin_subprocess_failed",
                        "message": "Trusted-local plugin operator failed",
                    }
                ],
                "warnings": [],
            }
        print(json.dumps(response, separators=(",", ":"), sort_keys=True))
        raise SystemExit(0)
    raise SystemExit("Expected --importer, --projection, or --operator and plugin root")


if __name__ == "__main__":
    main()

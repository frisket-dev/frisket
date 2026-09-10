"""`frisket plugin test-backend` — run ONE row through a plugin's Action.

Author-local dev loop only: it imports the author's own plugin.py, binds a
request against the Action exactly as the host does
(`BoundTypedActionRequest.bind`, the same param validation and output-field
resolution native dispatch performs), then calls the Action's own row handler
once and validates the result against its declared output model. There is no
plugin-specific execution contract here and no subprocess — an installed
plugin Action is an ordinary Action on the same native hosts as a builtin, so
"run it once" is just binding it and calling it.

Because it runs in-process, handler exceptions are not redacted: the real
traceback is what the author sees, which is the whole point of the command.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, Row, RowError, RowResult


def plugin_test_backend(
    plugin_root: Path,
    *,
    action: str,
    input_arg: str,
) -> int:
    import sys

    plugin_root = plugin_root.resolve()
    module_path = plugin_root / "plugin.py"
    if not module_path.is_file():
        print(
            f"error: no plugin.py in {plugin_root} — test-backend runs the "
            "Actions a backend module declares",
            file=sys.stderr,
        )
        return 1

    try:
        registered_actions = _registered_actions(plugin_root, module_path)
    except Exception as exc:  # noqa: BLE001 - author-facing import diagnostics
        print(f"error: plugin.py failed to import: {exc}", file=sys.stderr)
        return 1

    resolved = _resolve_action(registered_actions, action)
    if resolved is None:
        available = ", ".join(sorted(item.action_id for item in registered_actions))
        print(
            f"error: no Action {action!r} in {module_path} "
            f"(available: {available or 'none'})",
            file=sys.stderr,
        )
        return 1

    terminal = resolved.definition.run
    handler = getattr(terminal, "handler", None)
    if handler is None or not hasattr(terminal, "output_model"):
        print(
            f"error: Action {resolved.action_id!r} is not a row-scoped Action; "
            "`frisket plugin test-backend` runs one row through a map_rows "
            "Action",
            file=sys.stderr,
        )
        return 1
    if getattr(terminal, "capabilities", ()):
        print(
            f"error: Action {resolved.action_id!r} declares host capabilities "
            f"({', '.join(cap.__name__ for cap in terminal.capabilities)}); "
            "run it through the workbench, which provisions them",
            file=sys.stderr,
        )
        return 1

    try:
        row_inputs, params_json = _load_fixture(input_arg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        bound = BoundTypedActionRequest.bind(
            resolved,
            ActionRequest.model_validate(
                {
                    "action_id": resolved.action_id,
                    "scope": {"kind": "sheet_rows", "sheet_id": 1},
                    "params": params_json,
                    "idempotency_key": "frisket-plugin-test-backend",
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 - author-facing binding diagnostics
        print(
            f"error: --input params do not bind to the Action: {exc}", file=sys.stderr
        )
        return 1

    try:
        result = _run_row(bound, row_inputs)
    except RowError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - the author's own traceback is the point
        import traceback

        print("error: the row handler raised:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_row(bound: BoundTypedActionRequest, row_inputs: dict[str, Any]) -> Any:
    terminal = bound.action.definition.run
    result = terminal.handler(bound.params, Row(row_inputs))
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    if not isinstance(result, RowResult):
        raise ValueError("row handler must return RowResult")
    output = terminal.output_model.model_validate(result.output)
    return RowResult(output=output).model_dump(mode="json", by_alias=True)


async def _await(value: Any) -> Any:
    return await value


def _registered_actions(plugin_root: Path, module_path: Path) -> tuple[Any, ...]:
    """Import the backend with the loader real dispatch uses and collect every
    Action its Plugin declarations register."""
    import sys

    from frisket.plugins.action_loader import load_action_module
    from frisket.plugins.sdk import Plugin

    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = load_action_module(plugin_root, module_path.resolve())
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    collected: list[Any] = []
    for value in vars(module).values():
        if isinstance(value, Plugin):
            collected.extend(value.actions)
    return tuple(collected)


def _resolve_action(registered_actions: tuple[Any, ...], action: str) -> Any | None:
    """Accept the full namespaced action id or the Action's local name."""
    for item in registered_actions:
        if item.action_id == action:
            return item
    local = [item for item in registered_actions if item.definition.name == action]
    return local[0] if len(local) == 1 else None


def _load_fixture(input_arg: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse --input (a path to a JSON file, or an inline JSON string) into the
    row's column values plus the Action params. Fixture shape:
    `{"row": {...}, "params": {...}}`, where `row` maps column name to value.
    """
    candidate = Path(input_arg)
    raw = candidate.read_text(encoding="utf-8") if candidate.is_file() else input_arg
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--input is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("--input JSON must be an object with a 'row' field")
    row_raw = payload.get("row")
    if not isinstance(row_raw, dict):
        raise ValueError("--input JSON must contain a 'row' object")
    inputs = row_raw.get("inputs")
    if inputs is None:
        inputs = {key: value for key, value in row_raw.items() if key != "rowId"}
    if not isinstance(inputs, dict):
        raise ValueError("--input 'row.inputs' must be an object")
    params_raw = payload.get("params")
    params = params_raw if isinstance(params_raw, dict) else {}
    return dict(inputs), dict(params)

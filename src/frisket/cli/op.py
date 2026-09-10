"""`frisket op try` — one-row tools for typed first-party actions.

Params use the action's contract. Model-backed rows render the exact prompt
and schema without calling a model or spending money. Web search executes
through the host-owned capability adapter; other terminal shapes refuse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _load_input(input_arg: str) -> dict[str, Any]:
    path = Path(input_arg)
    text = path.read_text(encoding="utf-8") if path.exists() else input_arg
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('input must be a JSON object: {"row": {...}, "params": {...}}')
    return data


def _find_typed_action(name: str) -> Any | None:
    from frisket.actions.registry import ACTION_REGISTRY

    matches = [
        action
        for action in ACTION_REGISTRY.actions
        if name in (action.action_id, action.definition.name)
    ]
    if len(matches) > 1:
        raise LookupError(f"ambiguous typed action name {name!r}")
    return matches[0] if matches else None


def _try_op(name: str, input_arg: str) -> int:
    from pydantic import ValidationError

    from frisket.ops.base import OpContext

    try:
        typed = _find_typed_action(name)
        if typed is None:
            from frisket.actions.registry import ACTION_REGISTRY

            raise LookupError(
                f"unknown op {name!r}. Known ops: {sorted(ACTION_REGISTRY.action_ids)}"
            )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    data = _load_input(input_arg)
    row = dict(data.get("row") or {})
    raw_params = dict(data.get("params") or {})
    terminal = typed.definition.run
    try:
        params = terminal.params_model.model_validate(raw_params)
    except ValidationError as exc:
        return _print_param_errors(exc)

    from frisket.actions.core import (
        ModelRows,
        ModelRowsEvaluationContext,
        compile_output_schema,
    )
    from frisket.actions.types import Row, discover_references

    from frisket.actions.research_types import WebSearcher

    if getattr(terminal, "capabilities", ()) == (WebSearcher,):
        from frisket.engine.executor.web_search_read import AdmittedWebSearcher

        async def run_search():
            searcher = AdmittedWebSearcher(
                OpContext(project=None, http=None, extras={})
            )
            try:
                return await terminal.handler(params, Row(row), searcher)
            finally:
                await searcher.aclose()

        produced = asyncio.run(run_search())
        output = terminal.output_model.model_validate(produced.output)
        print(
            json.dumps(
                {
                    "op": typed.action_id,
                    "mode": "executed_external_row",
                    "outputs": output.model_dump(mode="json"),
                },
                indent=2,
                default=str,
            )
        )
        return 0
    if not isinstance(terminal, ModelRows):
        print(
            f"typed action {typed.action_id!r} is not supported by op try yet",
            file=sys.stderr,
        )
        return 2
    try:
        terminal.validate_source(params)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if terminal.evaluation is None:
        source = getattr(params, terminal.source_param)
        values = (
            {column.name: row.get(column.name) for column in source}
            if isinstance(source, list)
            else {"input": source.render(Row(row))}
        )
        prompt = terminal.renderer(params, Row(values))
    else:
        subject = getattr(params, terminal.evaluation.subject_param).name
        prompt = terminal.renderer(
            params,
            Row(
                {ref.column: row.get(ref.column) for ref in discover_references(params)}
            ),
            ModelRowsEvaluationContext(subject, 0, 0),
        )
    print(
        json.dumps(
            {
                "op": typed.action_id,
                "mode": "rendered_model_call",
                "note": (
                    "this action calls a model; frisket op try renders the "
                    "exact request instead of spending money"
                ),
                "messages": prompt.messages,
                "schema": compile_output_schema(terminal.output_model),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _print_param_errors(error: Any) -> int:
    from frisket.contracts.actions.validation_helpers import (
        _code_from_validation_error,
    )

    code = _code_from_validation_error(error)
    print(f"params did not validate ({code}):", file=sys.stderr)
    for detail in error.errors(include_url=False, include_context=False):
        loc = ".".join(str(part) for part in detail["loc"]) or "(params)"
        print(f"  {loc}: {detail['msg']}", file=sys.stderr)
    return 1


def op(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="frisket op")
    subcommands = parser.add_subparsers(dest="command", required=True)
    try_parser = subcommands.add_parser(
        "try", help="run one row of a first-party op locally (no server)"
    )
    try_parser.add_argument("name", help="op kind (map.summarize) or slug (summarize)")
    try_parser.add_argument(
        "--input",
        required=True,
        dest="input_arg",
        help='inline JSON or a file path: {"row": {...}, "params": {...}}',
    )
    args = parser.parse_args(argv)
    if args.command == "try":
        return _try_op(args.name, args.input_arg)
    parser.error("unknown op command")
    return 2

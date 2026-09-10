#!/usr/bin/env python3
"""Emit normalized action-catalog served truth for cross-language tests."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import MutableMapping, MutableSequence, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frisket.server.action_catalog_hints import (  # noqa: E402
    action_catalog_payload_with_launcher_hints,
)


_RUNTIME_ENGINE_FIELDS = ("available", "error", "models", "installed")


def _strip_runtime_engine_state(value: object) -> None:
    if isinstance(value, MutableMapping):
        for field in _RUNTIME_ENGINE_FIELDS:
            value.pop(field, None)
        for nested in value.values():
            _strip_runtime_engine_state(nested)
    elif isinstance(value, MutableSequence):
        for nested in value:
            _strip_runtime_engine_state(nested)


def _normalize_runtime_engine_state(entry: dict[str, Any]) -> None:
    ui_hints = entry.get("ui_hints")
    if not isinstance(ui_hints, dict):
        return
    engines = ui_hints.get("engines")
    if engines is None:
        return
    if not isinstance(engines, list):
        raise RuntimeError(f"{entry['kind']} has a non-list served engine roster")

    for engine in engines:
        if not isinstance(engine, dict):
            raise RuntimeError(f"{entry['kind']} has a non-object engine entry")
        _strip_runtime_engine_state(engine)


def _normalized_payload(kinds: frozenset[str] | None = None) -> dict[str, Any]:
    served = action_catalog_payload_with_launcher_hints({})
    # Preserve served order and all entries; TypeScript derives dispositions.
    actions = [
        deepcopy(entry)
        for entry in served["actions"]
        if kinds is None or entry["kind"] in kinds
    ]
    for entry in actions:
        _normalize_runtime_engine_state(entry)
    return {
        "schema_version": served["schema_version"],
        "action_schema": deepcopy(served["action_schema"]),
        "error_schema": deepcopy(served["error_schema"]),
        "result_schema": deepcopy(served["result_schema"]),
        "receipt_schema": deepcopy(served["receipt_schema"]),
        "validation_result_schema": deepcopy(served["validation_result_schema"]),
        "actions": actions,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate served truth and schema-signature invariants without emitting JSON",
    )
    parser.add_argument(
        "--kind",
        action="append",
        default=[],
        help="emit only this action kind (repeatable); shared schemas remain complete",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print emitted JSON for human inspection",
    )
    args = parser.parse_args(argv)
    kinds = frozenset(args.kind) if args.kind else None
    payload = _normalized_payload(kinds)

    if kinds is not None:
        emitted_kinds = {entry["kind"] for entry in payload["actions"]}
        missing = sorted(kinds - emitted_kinds)
        if missing:
            parser.error(f"unknown action kind(s): {', '.join(missing)}")

    if args.check:
        print("normalized served action-catalog projection is valid")
        return 0

    json.dump(
        payload,
        sys.stdout,
        indent=2 if args.pretty else None,
        separators=None if args.pretty else (",", ":"),
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

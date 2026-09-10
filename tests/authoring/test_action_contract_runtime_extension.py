from __future__ import annotations

import ast
from pathlib import Path

from frisket.contracts.action import ActionSpec
from frisket.contracts.action_validation import validate_action_spec
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)


ROOT = Path(__file__).resolve().parents[2]


def _runtime_action(kind: str) -> dict:
    return ActionSpec(
        kind=kind,
        capabilities=["project:write"],
        params={"value": "Ada"},
        idempotency_key="runtime-action@sha256:registry-seam",
    ).model_dump(mode="json")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_runtime_action_validation_uses_explicit_extension_seam() -> None:
    runtime_kind = "frisket.runtime_registry_seam.action"
    _reset_default_registry_for_tests()
    try:
        default_registry().register_runtime_binding(
            "actions",
            runtime_kind,
            handler_key="frisket-runtime-registry-seam:actions/demo",
            handler=lambda _action, _ctx: {"status": "completed"},
            handler_api="plugin_typed_action_native",
            plugin="frisket-runtime-registry-seam",
        )

        trusted = validate_action_spec(_runtime_action(runtime_kind))
        unbound = validate_action_spec(
            _runtime_action("frisket.runtime_registry_seam.unbound")
        )
    finally:
        _reset_default_registry_for_tests()

    assert trusted.ok is True
    assert trusted.action is not None
    assert trusted.action.kind == runtime_kind
    assert trusted.params == {"value": "Ada"}

    assert unbound.ok is False
    assert unbound.error is not None
    assert unbound.error.code == "unsupported_action_kind"

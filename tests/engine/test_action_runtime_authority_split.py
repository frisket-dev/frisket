from __future__ import annotations

import ast
import importlib.util
import inspect
from types import ModuleType

from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor import action_inventory
from frisket.engine.executor import action_lifecycle
from frisket.engine.executor import action_receipts
from frisket.engine.executor import action_reservations
from frisket.engine.executor import action_support


_SPLIT_MODULES = {
    "frisket.engine.executor.action_inventory",
    "frisket.engine.executor.action_lifecycle",
    "frisket.engine.executor.action_receipts",
    "frisket.engine.executor.action_reservations",
    "frisket.engine.executor.action_support",
}


def _split_imports(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module in _SPLIT_MODULES
    }


def test_action_runtime_authorities_have_one_acyclic_dependency_direction() -> None:
    assert importlib.util.find_spec("frisket.engine.executor.action_runtime") is None
    assert _split_imports(action_inventory) == set()
    assert _split_imports(action_support) == set()
    assert _split_imports(action_receipts) == set()
    assert _split_imports(action_reservations) == {
        "frisket.engine.executor.action_inventory",
        "frisket.engine.executor.action_receipts",
        "frisket.engine.executor.action_support",
    }
    assert _split_imports(action_lifecycle) == {
        "frisket.engine.executor.action_inventory",
        "frisket.engine.executor.action_receipts",
        "frisket.engine.executor.action_reservations",
        "frisket.engine.executor.action_support",
    }


def test_executor_deps_public_surface_is_owned_by_inventory() -> None:
    assert ExecutorDeps is action_inventory.ExecutorDeps
    assert ExecutorDeps.__module__ == "frisket.engine.executor.action_inventory"
    assert (
        action_receipts._result_from_receipt.__module__  # noqa: SLF001
        == "frisket.engine.executor.action_receipts"
    )
    assert (
        action_reservations._reserve_queued_action.__module__  # noqa: SLF001
        == "frisket.engine.executor.action_reservations"
    )
    assert (
        action_lifecycle._run_action_core_spec.__module__  # noqa: SLF001
        == "frisket.engine.executor.action_lifecycle"
    )

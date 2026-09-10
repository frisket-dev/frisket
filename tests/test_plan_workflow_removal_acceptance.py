from __future__ import annotations

import importlib


def test_formal_plan_and_workflow_facilities_are_retired() -> None:
    remaining: list[str] = []

    for module_name, symbol in (
        ("frisket.contracts", "PlanSpec"),
        ("frisket.contracts", "WorkflowSpec"),
        ("frisket.workflows", "compile_workflow_folder"),
        ("frisket.engine.executor", "run_plan_spec"),
    ):
        if hasattr(importlib.import_module(module_name), symbol):
            remaining.append(f"Python: {module_name}.{symbol}")

    assert not remaining, "formal facilities still available:\n" + "\n".join(remaining)

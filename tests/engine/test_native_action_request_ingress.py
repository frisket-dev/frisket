"""Execution accepts typed requests only, without a legacy handler fallback."""

from contextlib import closing

import pytest

from frisket.authoring.workbench import plugin_runtime_capabilities
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "import.rows",
        {},
        {"action_id": None},
        {"action_id": True},
        {"action_id": 42},
        {
            "schema_version": "frisket.action.v2",
            "kind": "example.retired",
            "params": {"value": "must not execute"},
            "capabilities": ["project:write"],
            "idempotency_key": "retired",
        },
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "params": {},
            "kind": "example.retired",
        },
    ],
)
def test_malformed_and_legacy_envelopes_refuse_before_plugin_lookup(
    tmp_path, monkeypatch, payload
):
    def forbidden_lookup(*args, **kwargs):
        pytest.fail("malformed requests must not resolve any installed handler")

    monkeypatch.setattr(
        plugin_runtime_capabilities, "project_runtime_binding", forbidden_lookup
    )
    # installed_actions imports this function directly; patch that same seam.
    monkeypatch.setattr(
        "frisket.authoring.workbench.installed_actions.project_runtime_binding",
        forbidden_lookup,
    )
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_action_spec(project, payload, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert result.receipt_id is None
        assert result.outputs == []
        for table in ("sheets", "rows", "columns", "receipts"):
            assert (
                project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )


def test_unknown_typed_action_returns_structured_refusal_without_publication(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_action_spec(
            project,
            {
                "action_id": "example.uninstalled",
                "scope": {"kind": "project"},
                "params": {},
                "idempotency_key": "unknown",
            },
            project_id="p",
        )
        assert result.status == "failed"
        assert result.action.kind == "example.uninstalled"
        assert result.errors[0].code == "invalid_action_request"
        assert result.receipt_id is None
        assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0

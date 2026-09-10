from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.engine.store import Project


def _source_create_action(
    *,
    name: str = "Policy feed",
    config: dict[str, Any] | None = None,
    idempotency_key: str = "source_create@sha256:first-source",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": name,
        "kind": "rss",
        "url": "https://example.test/feed.xml",
        "schedule": "@hourly",
        "enabled": True,
    }
    if config is not None:
        params["config"] = config
    return {
        "action_id": "source.create",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project, tmp_path
    return {}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _source_create_action(config={"download_enclosures": "queued"})


def _conflicting_name_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _source_create_action(name="Changed")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.store.sources import SourceStore

    del seeded
    source_output = next(output for output in result.outputs if output.kind == "source")
    source_id = int(source_output.ref["source_id"])
    assert source_output.ref["kind"] == "source_create_source"
    assert source_output.ref["name"] == "Policy feed"
    assert source_output.ref["source_kind"] == "rss"
    assert source_output.ref["schedule"] == "@hourly"
    assert source_output.ref["enabled"] is True

    source = SourceStore(project).get_source(source_id)
    assert source is not None
    assert source["last_status"] == "never"
    assert json.loads(source["config"]) == {"download_enclosures": "queued"}

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert row is not None
    receipt = Receipt.model_validate(json.loads(row["body"]))
    assert receipt.action_kind == "source.create"
    assert receipt.status == "completed"
    assert receipt.outputs[0].ref["source_id"] == source_id


CASES = [
    ExecutorCase(
        kind="source.create",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset({"create_source", "write_receipt"}),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("name", "kind", "url", "schedule", "enabled"),
            output_schema_properties=("source_id", "receipt_id"),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "idempotency_conflict",
                _conflicting_name_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sources": 1, "receipts": 1},
        check_state=_check_state,
        replay_output_names=False,
        request_style="typed",
    )
]


def test_source_create_validation_normalizes_params() -> None:
    """Validation echoes supplied params without materializing omitted defaults."""
    from frisket.actions.system import validate_root_action

    valid = validate_root_action(
        _source_create_action(config={"download_enclosures": "queued"})
    )
    assert valid.ok is True
    assert valid.params == {
        "name": "Policy feed",
        "kind": "rss",
        "url": "https://example.test/feed.xml",
        "config": {"download_enclosures": "queued"},
        "schedule": "@hourly",
        "enabled": True,
    }


def test_source_create_replay_normalizes_omitted_and_explicit_defaults(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "defaults.frisket", name="defaults")
    try:
        base = {
            "action_id": "source.create",
            "scope": {"kind": "project"},
            "params": {"name": "Feed"},
            "idempotency_key": "source-create-defaults",
        }
        first = run_action_spec(project, base, project_id="project-defaults")
        replay = run_action_spec(
            project,
            {
                **base,
                "params": {
                    "name": "Feed",
                    "kind": "url",
                    "url": None,
                    "config": {},
                    "sheet_id": None,
                    "schedule": None,
                    "enabled": True,
                },
            },
            project_id="project-defaults",
        )
        assert first.status == replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert replay.outputs[0].ref == first.outputs[0].ref
    finally:
        project.close()

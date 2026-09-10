from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project


def _source_update_action(
    source_id: int,
    *,
    patch: dict[str, Any] | None = None,
    idempotency_key: str = "source_update@sha256:first-update",
) -> dict[str, Any]:
    return {
        "action_id": "source.update",
        "scope": {"kind": "project"},
        "params": {
            "source_id": source_id,
            "patch": patch
            if patch is not None
            else {
                "schedule": "@daily",
                "enabled": False,
                "config": {"download_enclosures": "queued"},
            },
        },
        "idempotency_key": idempotency_key,
    }


def _source_delete_action(
    source_id: int,
    *,
    idempotency_key: str = "source_delete@sha256:first-delete",
) -> dict[str, Any]:
    return {
        "action_id": "source.delete",
        "scope": {"kind": "project"},
        "params": {"source_id": source_id},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    from frisket.engine.store.sources import SourceStore

    del tmp_path
    store = SourceStore(project)
    source_id = store.add_source(
        name="Policy feed",
        kind="rss",
        url="https://example.test/feed.xml",
        config={"download_enclosures": "skip"},
        schedule="@hourly",
        enabled=True,
    )
    store.record_source_run(source_id, new_rows=3, status="ok")
    return {"source_id": source_id}


def _get_source(project: Project, source_id: int) -> Any:
    from frisket.engine.store.sources import SourceStore

    return SourceStore(project).get_source(source_id)


# --- source.update ---------------------------------------------------------


def _make_update_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_update_action(seeded["source_id"])


def _empty_patch_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_update_action(
        seeded["source_id"],
        patch={},
        idempotency_key="source_update@sha256:empty-patch",
    )


def _conflicting_patch_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_update_action(seeded["source_id"], patch={"schedule": "@weekly"})


def _check_update_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    source_output = next(output for output in result.outputs if output.kind == "source")
    assert source_output.ref["kind"] == "source_update_source"
    assert source_output.ref["source_id"] == seeded["source_id"]
    assert source_output.ref["source_kind"] == "rss"
    assert source_output.ref["updated_fields"] == ["config", "enabled", "schedule"]

    source = _get_source(project, seeded["source_id"])
    assert source is not None
    assert source["schedule"] == "@daily"
    assert source["enabled"] == 0
    assert json.loads(source["config"]) == {"download_enclosures": "queued"}


# --- source.delete ---------------------------------------------------------


def _make_delete_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_delete_action(seeded["source_id"])


def _delete_missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # After the primary delete the source is gone; a new key must not "re-delete".
    return _source_delete_action(
        seeded["source_id"],
        idempotency_key="source_delete@sha256:missing-after-delete",
    )


def _check_delete_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    deleted_output = next(
        output for output in result.outputs if output.kind == "source"
    )
    assert deleted_output.ref["kind"] == "source_delete_source"
    assert deleted_output.ref["source_id"] == seeded["source_id"]
    assert deleted_output.ref["deleted"] is True
    assert _get_source(project, seeded["source_id"]) is None

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert row is not None
    receipt = Receipt.model_validate(json.loads(row["body"]))
    assert receipt.action_kind == "source.delete"
    assert receipt.outputs[0].ref["deleted"] is True


_SOURCE_POLL_CATALOG = dict(
    execution_mode="whole_project",
    async_mode="sync",
    writes_project=True,
    receipt_policy="writes_receipt",
    required_capabilities=("project:write",),
    cost_policy_kind="none",
)

CASES = [
    ExecutorCase(
        kind="source.update",
        catalog=CatalogEntry(
            side_effects=frozenset({"update_source", "write_receipt"}),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            input_schema_properties=("source_id", "patch"),
            output_schema_properties=("updated_fields", "receipt_id"),
            **_SOURCE_POLL_CATALOG,
        ),
        seed=_seed,
        make_action=_make_update_action,
        gates=(
            Gate(
                "idempotency_conflict",
                _conflicting_patch_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sources": 0, "receipts": 1},
        check_state=_check_update_state,
        replay_output_names=False,
        request_style="typed",
    ),
    ExecutorCase(
        kind="source.delete",
        catalog=CatalogEntry(
            side_effects=frozenset(
                {"delete_source", "cascade_source_runs", "write_receipt"}
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            input_schema_properties=("source_id",),
            output_schema_properties=("deleted", "receipt_id"),
            **_SOURCE_POLL_CATALOG,
        ),
        seed=_seed,
        make_action=_make_delete_action,
        gates=(
            Gate(
                "delete_missing_source",
                _delete_missing_source_action,
                "invalid_input_ref",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sources": -1, "source_runs": -1, "receipts": 1},
        check_state=_check_delete_state,
        replay_output_names=False,
        request_style="typed",
    ),
]


def test_source_update_clears_schedule_with_null_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patch value of None is an explicit clear, not "leave unchanged"."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        cleared = env.run(
            _source_update_action(
                env.seeded["source_id"],
                patch={"schedule": None},
                idempotency_key="source_update@sha256:clear-schedule",
            )
        )
        assert cleared.status == "completed", cleared.errors
        source = _get_source(env.project, env.seeded["source_id"])
        assert source is not None
        assert source["schedule"] is None


def test_source_update_rejects_empty_patch_at_typed_ingress(tmp_path: Path) -> None:
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "empty-patch.frisket", name="empty-patch")
    try:
        seeded = _seed(project, tmp_path)
        before = dict(_get_source(project, seeded["source_id"]))

        result = run_action_spec(
            project,
            _empty_patch_action(seeded),
            project_id="project-empty-patch",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert dict(_get_source(project, seeded["source_id"])) == before
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize("field", ["name", "kind", "enabled"])
def test_source_update_rejects_null_for_nonnullable_patch_fields(field: str) -> None:
    from pydantic import ValidationError

    from frisket.actions.sources import SourceUpdateParams

    with pytest.raises(ValidationError):
        SourceUpdateParams.model_validate({"source_id": 1, "patch": {field: None}})


@pytest.mark.parametrize("case_index", [0, 1], ids=["update", "delete"])
def test_source_mutation_validates_existence_inside_transaction(
    case_index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[case_index], tmp_path, monkeypatch) as env:

        def missing_in_transaction(store: SourceStore, source_id: int):
            assert env.project.db.in_transaction
            return None

        monkeypatch.setattr(SourceStore, "get_source", missing_in_transaction)
        action = (
            _make_update_action(env.seeded)
            if case_index == 0
            else _make_delete_action(env.seeded)
        )
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        )


@pytest.mark.parametrize("case_index", [0, 1], ids=["update", "delete"])
def test_source_mutation_and_receipt_roll_back_together(
    case_index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    with case_env(CASES[case_index], tmp_path, monkeypatch) as env:
        source_id = env.seeded["source_id"]
        before = dict(_get_source(env.project, source_id))

        def fail_receipt(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("receipt write failed")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        action = (
            _make_update_action(env.seeded)
            if case_index == 0
            else _make_delete_action(env.seeded)
        )
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert dict(_get_source(env.project, source_id)) == before


def test_source_delete_replay_preserves_full_ref_after_source_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[1], tmp_path, monkeypatch) as env:
        action = _make_delete_action(env.seeded)
        first = env.run(action)
        replay = env.run(action)

        assert _get_source(env.project, env.seeded["source_id"]) is None
        assert replay.receipt_id == first.receipt_id
        assert replay.outputs[0].ref == first.outputs[0].ref

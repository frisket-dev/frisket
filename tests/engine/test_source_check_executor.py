from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    CheckedSource,
    ProjectScope,
    SourceChecker,
)
from frisket.engine.store import Project


class ReportParams(ActionParams):
    target_id: int
    page: int | None


def report_page(params: ReportParams, sources: SourceChecker) -> CheckedSource:
    return sources.check(
        params.target_id,
        new_rows=3,
        status="ok",
        error=None,
        cursor=f"page/{params.page}" if params.page is not None else None,
    )


@pytest.mark.parametrize("page", [7, None])
def test_custom_source_check_records_actual_capability_arguments(
    tmp_path: Path, page: int | None
) -> None:
    import hashlib
    import json

    from frisket.engine.executor.source_action import run_typed_source_action
    from frisket.engine.store.sources import SourceStore

    definition = action(
        name="report",
        title="Report page",
        description="Record a checked page.",
        category=ActionCategory.SOURCES,
        run=report_page,
    )
    registered = ActionRegistry([ActionNamespace("custom", actions=[definition])]).get(
        "custom.report"
    )
    project = Project.create(tmp_path / "custom.frisket", name="custom")
    try:
        store = SourceStore(project)
        source_id = store.add_source(name="API", kind="api")
        store.record_source_run(source_id, cursor="existing")
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id="custom.report",
                scope=ProjectScope(),
                params={"target_id": source_id, "page": page},
                idempotency_key="custom-report",
            ),
        )

        result = run_typed_source_action(project, "custom-project", bound)

        assert result.status == "completed", result.errors
        cursor = f"page/{page}" if page is not None else None
        source = store.get_source(source_id)
        assert source["cursor"] == (cursor if cursor is not None else "existing")
        assert source["new_rows_total"] == 3
        body = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
        request = next(
            entry["ref"] for entry in body["inputs"] if entry["name"] == "request"
        )
        evidence = body["evidence"][0]["ref"]
        assert request["source_id"] == evidence["source_id"] == source_id
        assert request["reported_new_rows"] == 3
        assert request["cursor_provided"] is (cursor is not None)
        assert evidence["cursor_provided"] is (cursor is not None)
        assert evidence["cursor_hash"] == (
            "sha256:" + hashlib.sha256((cursor or "").encode()).hexdigest()
        )

        # Receipt replay must not need the handler's target to still exist.
        store.delete_source(source_id)
        replay = run_typed_source_action(project, "custom-project", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def _source_check_action(
    source_id: int,
    *,
    new_rows: int = 0,
    status: str = "ok",
    error: str | None = None,
    cursor: str | None = None,
    idempotency_key: str = "source_check@sha256:first-check",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source_id": source_id,
        "new_rows": new_rows,
        "status": status,
    }
    if error is not None:
        params["error"] = error
    if cursor is not None:
        params["cursor"] = cursor
    return {
        "action_id": "source.check",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    from frisket.engine.store.sources import SourceStore

    del tmp_path
    store = SourceStore(project)
    return {
        "url_source_id": store.add_source(
            name="Search feed",
            kind="courtlistener.custom",
            url="https://example.test/search",
            config={"query": {"court": "ca9", "precedential": True}},
        ),
        "rss_source_id": store.add_source(
            name="RSS feed", kind="rss", url="https://example.test/feed.xml"
        ),
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_check_action(seeded["url_source_id"], new_rows=5, cursor="cursor-5")


def _conflicting_rows_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _source_check_action(seeded["url_source_id"], new_rows=6)


def _rss_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # rss sources are checked by the poller, not by manual source.check.
    return _source_check_action(
        seeded["rss_source_id"], idempotency_key="source_check@sha256:rss-source"
    )


def _new_rows_total(project: Project, source_id: int) -> int:
    from frisket.engine.store.sources import SourceStore

    source = SourceStore(project).get_source(source_id)
    assert source is not None
    return int(source["new_rows_total"])


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    import json

    from frisket.contracts.action import Receipt
    from frisket.engine.store.sources import SourceStore

    source_id = seeded["url_source_id"]
    assert result.outputs[0].kind == "source"
    source_run = next(
        output for output in result.outputs if output.kind == "source_run"
    )
    assert source_run.ref["source_id"] == source_id
    assert source_run.ref["source_run_id"]
    assert source_run.ref["new_rows"] == 5
    assert source_run.ref["status"] == "ok"
    assert result.outputs[0].ref["source_kind"] == "courtlistener.custom"

    source_after = SourceStore(project).get_source(source_id)
    assert source_after is not None
    assert source_after["last_status"] == "ok"
    assert source_after["new_rows_total"] == 5
    assert source_after["cursor"] == "cursor-5"

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert row is not None
    receipt = Receipt.model_validate(json.loads(row["body"]))
    assert receipt.action_kind == "source.check"
    assert receipt.status == "completed"
    refs = [item.ref for item in [*receipt.inputs, *receipt.outputs, *receipt.evidence]]
    assert "source_check_source" in {ref["kind"] for ref in refs}
    assert "source_check_run" in {ref["kind"] for ref in refs}


CASES = [
    ExecutorCase(
        kind="source.check",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {"advance_source_cursor", "write_source_run", "write_receipt"}
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "unsupported_source_kind",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=(
                "source_id",
                "new_rows",
                "status",
                "error",
                "cursor",
            ),
            output_schema_properties=(
                "source_id",
                "source_run_id",
                "status",
                "new_rows",
                "receipt_id",
            ),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "idempotency_conflict",
                _conflicting_rows_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "unsupported_source_kind",
                _rss_source_action,
                "unsupported_source_kind",
            ),
        ),
        expect_counts={"source_runs": 1, "receipts": 1},
        check_state=_check_state,
        replay_output_names=False,
        request_style="typed",
    )
]


def test_source_check_validation_normalizes_params() -> None:
    from frisket.actions.system import validate_root_action

    valid = validate_root_action(
        _source_check_action(
            3,
            new_rows=7,
            cursor="cursor-2",
            idempotency_key="source_check@sha256:validated",
        )
    )
    assert valid.ok is True
    assert valid.params == {
        "source_id": 3,
        "new_rows": 7,
        "status": "ok",
        "cursor": "cursor-2",
    }


def test_source_check_replay_normalizes_omitted_and_explicit_defaults(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.sources import SourceStore

    project = Project.create(tmp_path / "defaults.frisket", name="defaults")
    try:
        source_id = SourceStore(project).add_source(name="API", kind="api")
        base = {
            "action_id": "source.check",
            "scope": {"kind": "project"},
            "params": {"source_id": source_id},
            "idempotency_key": "source-check-defaults",
        }
        first = run_action_spec(project, base, project_id="project-defaults")
        replay = run_action_spec(
            project,
            {
                **base,
                "params": {
                    "source_id": source_id,
                    "new_rows": 0,
                    "status": "ok",
                    "error": None,
                    "cursor": None,
                },
            },
            project_id="project-defaults",
        )
        assert first.status == replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert [output.ref for output in replay.outputs] == [
            output.ref for output in first.outputs
        ]
        assert SourceStore(project).source_runs_total(source_id) == 1
    finally:
        project.close()


def test_source_check_replay_does_not_reapply_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay returns the stored receipt without advancing cursor bookkeeping:
    new_rows_total stays at the first run's value instead of doubling."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert _new_rows_total(env.project, env.seeded["url_source_id"]) == 5


def test_source_check_replays_after_source_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed receipt is authoritative even after its source is absent."""
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        SourceStore(env.project).delete_source(env.seeded["url_source_id"])

        replay = env.run_primary()

        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert [output.ref for output in replay.outputs] == [
            output.ref for output in first.outputs
        ]


def test_source_check_validates_source_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:

        def missing_in_transaction(store: SourceStore, source_id: int):
            assert env.project.db.in_transaction
            return None

        monkeypatch.setattr(SourceStore, "get_source", missing_in_transaction)
        result = env.run(_make_action(env.seeded))

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
            == 0
        )
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        )


def test_source_check_validates_rss_kind_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        original = SourceStore.get_source

        def rss_in_transaction(store: SourceStore, source_id: int):
            assert env.project.db.in_transaction
            row = original(store, source_id)
            assert row is not None
            return {**dict(row), "kind": "rss"}

        monkeypatch.setattr(SourceStore, "get_source", rss_in_transaction)
        result = env.run(_make_action(env.seeded))

        assert result.status == "failed"
        assert result.errors[0].code == "unsupported_source_kind"
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
            == 0
        )
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        )


def test_source_check_and_receipt_roll_back_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        source_id = env.seeded["url_source_id"]
        before = dict(SourceStore(env.project).get_source(source_id) or {})

        def fail_receipt(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("receipt write failed")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        result = env.run(_make_action(env.seeded))

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert dict(SourceStore(env.project).get_source(source_id) or {}) == before
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
            == 0
        )


def test_source_check_null_cursor_preserves_cursor_and_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.sources import SourceStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        store = SourceStore(env.project)
        source_id = env.seeded["url_source_id"]
        store.record_source_run(source_id, cursor="existing-cursor")
        result = env.run(
            _source_check_action(
                source_id,
                status="error",
                error="upstream unavailable",
                idempotency_key="source-check-error",
            )
        )

        assert result.status == "completed", result.errors
        source = store.get_source(source_id)
        assert source is not None
        assert source["cursor"] == "existing-cursor"
        assert source["last_status"] == "error"
        source_run = next(
            output for output in result.outputs if output.kind == "source_run"
        )
        assert source_run.ref["error"] == "upstream unavailable"

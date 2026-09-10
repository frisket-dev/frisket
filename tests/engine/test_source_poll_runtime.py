from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from frisket.actions.system import typed_action_for_request
from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    PolledSource,
    SourcePoller as PollCapability,
)
from frisket.engine.executor.map_rows_action import typed_request_hash

from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    encode_cursor,
    register_source_poller,
    unregister_source_poller,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


PROJECT_ID = "project-source-poll-runtime"


class FakePoller:
    kind = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.items: list[SourcePollItem] = []
        self.cursor_after: dict[str, Any] | str | None = {"page": 1}
        self.warnings: list[str] = ["fake warning"]
        self.provider_use: list[dict[str, Any]] = [
            {"provider": "fake", "model": "none", "cost_actual": 0.012}
        ]
        self.cost: dict[str, Any] = {"cost_micro": 12_000}
        self.artifacts: list[dict[str, Any]] = [{"kind": "fixture", "ref": "a"}]
        self.summary: dict[str, Any] = {"fixture": True}
        self.fail: Exception | None = None

    def validate_config(self, source: dict[str, Any]) -> str | None:
        if source.get("config", {}).get("bad"):
            return "bad fake config"
        return None

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        assert "db" not in ctx.source
        return SourcePollResult(
            items=list(self.items),
            cursor_after=self.cursor_after,
            warnings=list(self.warnings),
            provider_use=list(self.provider_use),
            cost=dict(self.cost),
            artifacts=list(self.artifacts),
            summary=dict(self.summary),
        )


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "source_poll@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["source"] = source_id
    if source is not None:
        params["source"] = source
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _source(kind: str = "fake") -> dict[str, Any]:
    return {
        "kind": kind,
        "name": "Fake Source",
        "url": "https://sources.example/fake",
        "config": {"mode": "test"},
    }


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])


def _rows(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    columns = {
        int(column["id"]): column["name"]
        for column in project.columns(sheet_id, include_hidden=True)
    }
    out: list[dict[str, Any]] = []
    for row in project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
        (sheet_id,),
    ).fetchall():
        values = project.db.execute(
            "SELECT column_id, value FROM cells WHERE row_id=?",
            (row["id"],),
        ).fetchall()
        out.append(
            {
                columns[int(value["column_id"])]: json.loads(value["value"])
                for value in values
            }
        )
    return out


def _table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class _RenamedPollParams(ActionParams):
    target: str


def _renamed_poll(params: _RenamedPollParams, poller: PollCapability) -> PolledSource:
    result = poller.poll(int(params.target.removeprefix("source:")))
    # A handler's mutable descriptive return cannot overwrite admitted facts.
    result.source_id = 999999
    result.item_count = 999999
    result.cost_micro = 999999
    return result


def test_reused_poll_capability_records_actual_arguments_and_not_custom_return(
    tmp_path,
):
    from frisket.engine.executor.source_poll_action import run_typed_source_poll_action

    poller = FakePoller()
    poller.items = [SourcePollItem(dedupe_key="a", row={"title": "Actual"})]
    register_source_poller(poller)
    project = Project.create(tmp_path / "custom.frisket", name="Custom")
    try:
        source_id = SourceStore(project).add_source(name="Actual", kind="fake")
        custom = action(
            name="poll",
            title="Poll",
            description="Poll a derived source reference.",
            category=ActionCategory.SOURCES,
            run=_renamed_poll,
        )
        registered = ActionRegistry(
            (ActionNamespace("custom", actions=(custom,)),)
        ).get("custom.poll")
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id="custom.poll",
                scope={"kind": "project"},
                params={"target": f"source:{source_id}"},
                idempotency_key="custom-poll",
            ),
        )
        assert registered.catalog_entry()["cost_policy"] == {
            "kind": "external_metered",
            "requires_confirmation": False,
        }
        result = run_typed_source_poll_action(project, PROJECT_ID, bound)
        assert result.status == "completed", result.errors
        receipt = _receipt(project, result.receipt_id)
        run_ref = next(
            item["ref"]
            for item in receipt["outputs"]
            if item["ref"]["kind"] == "source_poll_run"
        )
        assert run_ref["source_id"] == source_id
        assert run_ref["materialized_rows"] == 1
        assert run_ref["cost_micro"] == 12000
        assert receipt["provider_use"] == poller.provider_use
        assert receipt["warnings"] == poller.warnings
        assert (
            run_typed_source_poll_action(project, PROJECT_ID, bound).receipt_id
            == result.receipt_id
        )
        assert poller.calls == 1
    finally:
        project.close()
        unregister_source_poller("fake")


@pytest.mark.parametrize("failure", ["value_error", "runtime_error", "wrong_return"])
def test_post_poll_handler_failure_preserves_accounting_and_replay(tmp_path, failure):
    from frisket.engine.executor.source_poll_action import run_typed_source_poll_action

    def produce(params: _RenamedPollParams, sources: PollCapability) -> PolledSource:
        sources.poll(int(params.target))
        if failure == "value_error":
            raise ValueError("author failed after polling")
        if failure == "runtime_error":
            raise RuntimeError("secret-author-detail-after-polling")
        return None

    poller = FakePoller()
    poller.items = [SourcePollItem(dedupe_key="a", row={"title": "Unpublished"})]
    register_source_poller(poller)
    project = Project.create(tmp_path / "post-poll.frisket", name="Post poll")
    try:
        source_id = SourceStore(project).add_source(name="Actual", kind="fake")
        cursor_before = SourceStore(project).get_source(source_id)["cursor"]
        registered = ActionRegistry(
            (
                ActionNamespace(
                    "custom",
                    actions=(
                        action(
                            name="poll",
                            title="Poll",
                            description="Fail after an observed source poll.",
                            category=ActionCategory.SOURCES,
                            run=produce,
                        ),
                    ),
                ),
            )
        ).get("custom.poll")
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id="custom.poll",
                scope={"kind": "project"},
                params={"target": str(source_id)},
                idempotency_key="post-poll-failure",
            ),
        )
        results = []
        exceptions = []
        for _ in range(2):
            try:
                results.append(run_typed_source_poll_action(project, PROJECT_ID, bound))
            except Exception as error:
                exceptions.append(f"{type(error).__name__}: {error}")
        observed = {
            "poll_calls": poller.calls,
            "receipts": _table_count(project, "receipts"),
            "source_runs": _table_count(project, "source_runs"),
            "exceptions": exceptions,
        }
        assert not exceptions, observed
        failed, replay = results
        assert failed.status == "failed"
        assert failed.receipt_id is not None, observed
        assert replay.receipt_id == failed.receipt_id
        assert replay.status == "failed"
        assert poller.calls == 1
        assert _table_count(project, "source_runs") == 1
        assert _table_count(project, "rows") == 0
        assert SourceStore(project).get_source(source_id)["cursor"] == cursor_before
        run = project.db.execute("SELECT * FROM source_runs").fetchone()
        assert run["status"] == "error"
        assert run["receipt_id"] == failed.receipt_id
        assert run["cursor_after"] == run["cursor_before"]
        assert run["cost_micro"] == 12000
        assert run["warning_count"] == len(poller.warnings)
        run_summary = json.loads(run["summary_json"])
        assert run_summary["artifacts"] == poller.artifacts
        assert run_summary["summary"] == poller.summary
        assert failed.warnings == poller.warnings
        receipt = _receipt(project, failed.receipt_id)
        assert receipt["status"] == "failed"
        assert receipt["provider_use"] == poller.provider_use
        assert receipt["warnings"] == poller.warnings
        assert "author failed after polling" not in json.dumps(receipt)
        assert "secret-author-detail-after-polling" not in json.dumps(receipt)
        assert "secret-author-detail-after-polling" not in run["error"]
        assert _table_count(project, "source_items") == 0
        assert _table_count(project, "ops") == 0
        artifacts = next(
            item["ref"]
            for item in receipt["evidence"]
            if item["ref"]["kind"] == "source_poll_artifacts"
        )
        assert artifacts["artifacts"] == poller.artifacts
    finally:
        project.close()
        unregister_source_poller("fake")


def test_new_source_config_refusal_and_failed_poll_lifecycle(tmp_path):
    from frisket.engine.executor import run_action_spec

    poller = FakePoller()
    register_source_poller(poller)
    project = Project.create(tmp_path / "failure.frisket", name="Failure")
    try:
        invalid = run_action_spec(
            project,
            _source_poll_action(
                source={**_source(), "config": {"bad": True}},
                idempotency_key="bad-config",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid.status == "failed"
        assert invalid.errors[0].code == "unsupported_source_config"
        assert invalid.receipt_id is None
        assert poller.calls == 0
        assert _table_count(project, "sources") == 0
        assert _table_count(project, "source_runs") == 0

        poller.fail = RuntimeError("provider unavailable")
        request = _source_poll_action(source=_source(), idempotency_key="new-failed")
        failed = run_action_spec(project, request, project_id=PROJECT_ID)
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert _table_count(project, "sources") == 1
        assert _table_count(project, "rows") == 0
        run = project.db.execute("SELECT * FROM source_runs").fetchone()
        assert run["status"] == "error"
        assert run["receipt_id"] == failed.receipt_id
        assert run["cursor_before"] == run["cursor_after"]
        assert run["cost_micro"] == 0
        receipt = _receipt(project, failed.receipt_id)
        assert receipt["status"] == "failed"
        assert receipt["provider_use"] == []
        assert receipt["warnings"] == []
        poller.fail = None
        replay = run_action_spec(project, request, project_id=PROJECT_ID)
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert poller.calls == 1
        assert _table_count(project, "source_runs") == 1
    finally:
        project.close()
        unregister_source_poller("fake")


def test_poll_publication_rolls_back_with_receipt_failure(tmp_path, monkeypatch):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.receipts import ReceiptStore

    poller = FakePoller()
    poller.items = [SourcePollItem(dedupe_key="a", row={"title": "Actual"})]
    register_source_poller(poller)
    project = Project.create(tmp_path / "rollback.frisket", name="Rollback")
    try:
        source_id = SourceStore(project).add_source(name="Actual", kind="fake")
        before = dict(SourceStore(project).get_source(source_id))

        def fail_insert(*args, **kwargs):
            raise RuntimeError("receipt unavailable")

        monkeypatch.setattr(ReceiptStore, "insert_finished", fail_insert)
        result = run_action_spec(
            project, _source_poll_action(source_id=source_id), project_id=PROJECT_ID
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert poller.calls == 1
        for table in (
            "source_runs",
            "source_items",
            "sheets",
            "columns",
            "rows",
            "ops",
            "receipts",
        ):
            assert _table_count(project, table) == 0
        assert dict(SourceStore(project).get_source(source_id)) == before
    finally:
        project.close()
        unregister_source_poller("fake")


def test_poll_preview_and_legacy_request_refuse_before_source_creation(tmp_path):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.actions import resolve_map_preview

    poller = FakePoller()
    register_source_poller(poller)
    project = Project.create(tmp_path / "refusal.frisket", name="Refusal")
    try:
        preview = resolve_map_preview(project, _source_poll_action(source=_source()))
        assert preview.code == "unsupported_action_kind"
        old = run_action_spec(
            project,
            {
                "schema_version": "frisket.action.v2",
                "kind": "source.poll",
                "params": {"source": _source()},
                "capabilities": ["project:write", "external:source_poll"],
                "idempotency_key": "legacy-refused",
            },
            project_id=PROJECT_ID,
        )
        assert old.status == "failed"
        assert old.errors[0].code == "invalid_action_request"
        assert poller.calls == 0
        assert _table_count(project, "sources") == 0
        assert _table_count(project, "receipts") == 0
    finally:
        project.close()
        unregister_source_poller("fake")


def test_source_poll_catalog_validation_registry_dedupe_replay_and_failures(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import root_action_catalog
    from frisket.engine.executor import run_action_spec

    poller = FakePoller()
    poller.items = [
        SourcePollItem(
            source_item_id="item-a",
            dedupe_key="a",
            item_hash="sha256:a1",
            title="Alpha",
            url="https://example.com/a",
            row={"title": "Alpha", "score": 1},
            raw={"id": "a", "title": "Alpha"},
        ),
        SourcePollItem(
            source_item_id="item-b",
            dedupe_key="b",
            item_hash="sha256:b1",
            title="Beta",
            url="https://example.com/b",
            row={"title": "Beta", "score": 2},
            raw={"id": "b", "title": "Beta"},
        ),
    ]
    register_source_poller(poller)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_source_poller(FakePoller())
        assert encode_cursor({"page": 1, "done": False}) == '{"done":false,"page":1}'

        catalog = root_action_catalog()
        entry = next(item for item in catalog.actions if item.kind == "source.poll")
        assert entry.execution_mode == "whole_project"
        assert entry.required_capabilities == ["project:write", "external:source_poll"]
        assert entry.cost_policy.kind == "external_metered"
        assert entry.cost_policy.requires_confirmation is False
        # source.poll now runs on the plain body: a sync poll is never observably "in
        # progress", so it no longer advertises the running/stale-running reservation codes;
        # idempotency_conflict (the params-hash mismatch) is the surviving idempotency error.
        entry_error_codes = {error.code for error in entry.errors}
        assert "idempotency_conflict" in entry_error_codes
        assert "idempotency_in_progress" not in entry_error_codes
        assert "idempotency_stale_running" not in entry_error_codes
        assert entry.receipt_policy == "writes_receipt"

        # Requirements are derived by the host; callers cannot underdeclare them.
        with pytest.raises(ValidationError, match="capabilities"):
            typed_action_for_request(
                {
                    **_source_poll_action(source=_source()),
                    "capabilities": ["project:write"],
                }
            )
        with pytest.raises(ValidationError, match="extra"):
            typed_action_for_request(
                _source_poll_action(
                    source={"kind": "fake", "name": "Fake", "extra": True}
                )
            )
        with pytest.raises(ValidationError, match="sheet_id"):
            typed_action_for_request(
                _source_poll_action(source={**_source(), "sheet_id": True})
            )

        project = Project.create(tmp_path / "source-poll.frisket", name="Source poll")
        try:
            first = run_action_spec(
                project,
                _source_poll_action(source=_source()),
                project_id=PROJECT_ID,
            )
            assert first.status == "completed"
            assert first.receipt_id is not None
            assert poller.calls == 1
            source_id = next(
                output.ref["source_id"]
                for output in first.outputs
                if output.kind == "source"
            )
            source_run_id = next(
                output.ref["source_run_id"]
                for output in first.outputs
                if output.kind == "source_run"
            )
            sheet_id = next(
                output.sheet_id for output in first.outputs if output.kind == "sheet"
            )
            assert sheet_id is not None
            rows = _rows(project, sheet_id)
            assert [row["title"] for row in rows] == ["Alpha", "Beta"]
            assert rows[0]["source_id"] == source_id
            assert rows[0]["source_run_id"] == source_run_id
            assert rows[0]["source_item_id"] == "item-a"
            assert rows[0]["source_url"] == "https://example.com/a"
            assert rows[0]["source_raw"] == {"id": "a", "title": "Alpha"}

            source_run = project.db.execute(
                "SELECT * FROM source_runs WHERE id=?",
                (source_run_id,),
            ).fetchone()
            assert source_run is not None
            assert source_run["status"] == "ok"
            assert source_run["receipt_id"] == first.receipt_id
            assert source_run["new_rows"] == 2
            assert source_run["skipped_rows"] == 0
            assert source_run["changed_rows"] == 0
            assert source_run["revisions"] == 0
            assert source_run["warning_count"] == 1
            assert source_run["cost_micro"] == 12_000
            assert json.loads(source_run["cursor_after"]) == {"page": 1}
            assert json.loads(source_run["summary_json"])["materialized_rows"] == 2
            assert _table_count(project, "source_items") == 2

            receipt = _receipt(project, first.receipt_id)
            assert receipt["action_kind"] == "source.poll"
            assert receipt["status"] == "completed"
            assert receipt["warnings"] == ["fake warning"]
            assert receipt["provider_use"] == poller.provider_use
            refs = [
                item["ref"]
                for item in [*receipt["outputs"], *receipt["evidence"]]
                if isinstance(item, dict)
            ]
            assert {"source_poll_run", "source_poll_rows", "source_poll_cursor"} <= {
                ref["kind"] for ref in refs
            }

            before_replay = {
                table: _table_count(project, table)
                for table in ("source_runs", "source_items", "rows", "receipts")
            }
            replay = run_action_spec(
                project,
                _source_poll_action(source=_source()),
                project_id=PROJECT_ID,
            )
            assert replay.status == "completed"
            assert replay.receipt_id == first.receipt_id
            assert poller.calls == 1
            assert {
                table: _table_count(project, table)
                for table in ("source_runs", "source_items", "rows", "receipts")
            } == before_replay

            poller.items = [
                SourcePollItem(
                    source_item_id="item-a",
                    dedupe_key="a",
                    item_hash="sha256:a1",
                    title="Alpha",
                    url="https://example.com/a",
                    row={"title": "Alpha", "score": 1},
                    raw={"id": "a", "title": "Alpha"},
                ),
                SourcePollItem(
                    source_item_id="item-b",
                    dedupe_key="b",
                    item_hash="sha256:b2",
                    title="Beta revised",
                    url="https://example.com/b",
                    row={"title": "Beta revised", "score": 3},
                    raw={"id": "b", "title": "Beta revised"},
                ),
                SourcePollItem(
                    source_item_id="item-c",
                    dedupe_key="c",
                    item_hash="sha256:c1",
                    title="Gamma",
                    url="https://example.com/c",
                    row={"title": "Gamma", "score": 4},
                    raw={"id": "c", "title": "Gamma"},
                ),
            ]
            second = run_action_spec(
                project,
                _source_poll_action(
                    source_id=source_id,
                    idempotency_key="source_poll@sha256:second",
                ),
                project_id=PROJECT_ID,
            )
            assert second.status == "completed"
            assert poller.calls == 2
            second_run_id = next(
                output.ref["source_run_id"]
                for output in second.outputs
                if output.kind == "source_run"
            )
            second_run = project.db.execute(
                "SELECT * FROM source_runs WHERE id=?",
                (second_run_id,),
            ).fetchone()
            assert second_run["new_rows"] == 1
            assert second_run["skipped_rows"] == 1
            assert second_run["changed_rows"] == 1
            assert second_run["revisions"] == 1
            assert [row["title"] for row in _rows(project, sheet_id)] == [
                "Alpha",
                "Beta",
                "Beta revised",
                "Gamma",
            ]
            assert _rows(project, sheet_id)[2]["_revises"] == "b"
            assert _rows(project, sheet_id)[2]["_revision"] == 1
            assert _table_count(project, "source_items") == 3
            beta_item = project.db.execute(
                "SELECT revision FROM source_items WHERE source_id=? AND dedupe_key='b'",
                (source_id,),
            ).fetchone()
            assert beta_item is not None
            assert beta_item["revision"] == 1

            poller.items = [
                SourcePollItem(
                    source_item_id="item-b",
                    dedupe_key="b",
                    item_hash="sha256:b3",
                    title="Beta second revision",
                    url="https://example.com/b",
                    row={"title": "Beta second revision", "score": 5},
                    raw={"id": "b", "title": "Beta second revision"},
                ),
            ]
            third = run_action_spec(
                project,
                _source_poll_action(
                    source_id=source_id,
                    idempotency_key="source_poll@sha256:third",
                ),
                project_id=PROJECT_ID,
            )
            assert third.status == "completed"
            assert poller.calls == 3
            assert _rows(project, sheet_id)[-1]["_revises"] == "b"
            assert _rows(project, sheet_id)[-1]["_revision"] == 2
            beta_item = project.db.execute(
                "SELECT revision FROM source_items WHERE source_id=? AND dedupe_key='b'",
                (source_id,),
            ).fetchone()
            assert beta_item is not None
            assert beta_item["revision"] == 2

            poller.fail = RuntimeError("provider down")
            failed = run_action_spec(
                project,
                _source_poll_action(
                    source_id=source_id,
                    idempotency_key="source_poll@sha256:failed",
                ),
                project_id=PROJECT_ID,
            )
            assert failed.status == "failed"
            assert failed.receipt_id is not None
            assert failed.errors[0].code == "source_poll_failed"
            failed_run = SourceStore(project).source_runs(source_id, limit=1)[0]
            assert failed_run["status"] == "error"
            assert failed_run["receipt_id"] == failed.receipt_id
            assert failed_run["error"] == "provider down"
            assert _receipt(project, failed.receipt_id)["status"] == "failed"
        finally:
            project.close()
    finally:
        unregister_source_poller("fake")


def test_source_poll_rejects_unsupported_kind_and_scheduler_dispatches_public_action(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.jobs import (
        SOURCE_POLL_KIND,
        SqliteJobQueue,
        Worker,
        default_registry,
        register_source_poll_handler,
    )

    project = Project.create(tmp_path / "unsupported.frisket", name="Unsupported")
    try:
        unsupported_source_id = SourceStore(project).add_source(
            name="Unsupported",
            kind="not-registered",
            url="https://example.com/source",
        )
        rejected = run_action_spec(
            project,
            _source_poll_action(
                source_id=unsupported_source_id,
                idempotency_key="source_poll@sha256:unsupported",
            ),
            project_id=PROJECT_ID,
        )
        assert rejected.status == "failed"
        assert rejected.errors[0].code == "unsupported_source_kind"
    finally:
        project.close()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    scheduled = Project.create(workspace / "news.frisket", name="news")
    try:
        source_id = SourceStore(scheduled).add_source(
            name="Feed",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )
    finally:
        scheduled.close()
    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        job_id = queue.enqueue(
            SOURCE_POLL_KIND,
            {"project_id": "news", "source_id": source_id},
            max_attempts=1,
        )
        registry = default_registry()
        register_source_poll_handler(
            registry,
            workspace_root=workspace,
            queue=queue,
            fetch=lambda url: (
                "<?xml version='1.0'?><rss version='2.0'><channel>"
                "<title>Feed</title><item><guid>a</guid><title>A</title>"
                "<link>https://example.com/a</link></item></channel></rss>"
            ),
        )
        assert Worker(queue, registry).run_once()
        job = queue.get(job_id)
        assert job.status == "done"
        assert job.result["action_kind"] == "source.poll"
        reopened = Project(workspace / "news.frisket")
        try:
            receipts = reopened.db.execute(
                "SELECT action_kind FROM receipts ORDER BY created_at, id"
            ).fetchall()
            assert [row["action_kind"] for row in receipts] == ["source.poll"]
            runs = [dict(row) for row in SourceStore(reopened).source_runs(source_id)]
            assert runs[0]["receipt_id"] == job.result["receipt_id"]
        finally:
            reopened.close()
    finally:
        queue.close()


def test_source_poll_running_reservation_replays_without_provider_call(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.engine.executor import run_action_spec

    poller = FakePoller()
    register_source_poller(poller)
    try:
        project = Project.create(tmp_path / "running-reservation.frisket", name="Run")
        try:
            source_id = SourceStore(project).add_source(
                name="Fake",
                kind="fake",
                url="https://example.com/fake",
            )
            action = _source_poll_action(
                source_id=source_id,
                idempotency_key="source_poll@sha256:running",
            )
            params_hash = typed_request_hash(typed_action_for_request(action))
            receipt = Receipt(
                receipt_id="receipt_running_source_poll",
                project_id=PROJECT_ID,
                action_id="act_running_source_poll",
                action_kind="source.poll",
                idempotency_key=action["idempotency_key"],
                params_hash=params_hash,
                status="running",
                inputs=[
                    ReceiptIO(
                        name="idempotency",
                        ref={"kind": "source_poll_idempotency_reservation"},
                    )
                ],
            )
            project.db.execute(
                "INSERT INTO receipts (id, action_kind, action_id, "
                "idempotency_key, params_hash, status, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.action_kind,
                    receipt.action_id,
                    receipt.idempotency_key,
                    receipt.params_hash,
                    receipt.status,
                    json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
                ),
            )
            project.db.commit()
            # The plain body has no running reservation: an existing receipt for the key
            # short-circuits via idempotency replay of the stored receipt (here the injected
            # "running" receipt) WITHOUT calling the provider or writing source rows/runs.
            result = run_action_spec(project, action, project_id=PROJECT_ID)
            assert result.status == "running"
            assert result.receipt_id == receipt.receipt_id
            assert poller.calls == 0
            assert _table_count(project, "source_runs") == 0
            assert _table_count(project, "source_items") == 0
        finally:
            project.close()
    finally:
        unregister_source_poller("fake")


def test_source_poll_stale_running_reservation_returns_recovery_without_poll(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.engine.executor import run_action_spec

    poller = FakePoller()
    register_source_poller(poller)
    try:
        project = Project.create(tmp_path / "stale-running.frisket", name="Run")
        try:
            source_id = SourceStore(project).add_source(
                name="Fake",
                kind="fake",
                url="https://example.com/fake",
            )
            action = _source_poll_action(
                source_id=source_id,
                idempotency_key="source_poll@sha256:stale",
            )
            params_hash = typed_request_hash(typed_action_for_request(action))
            receipt = Receipt(
                receipt_id="receipt_stale_source_poll",
                project_id=PROJECT_ID,
                action_id="act_stale_source_poll",
                action_kind="source.poll",
                idempotency_key=action["idempotency_key"],
                params_hash=params_hash,
                status="running",
                inputs=[
                    ReceiptIO(
                        name="idempotency",
                        ref={"kind": "source_poll_idempotency_reservation"},
                    )
                ],
            )
            project.db.execute(
                "INSERT INTO receipts (id, action_kind, action_id, "
                "idempotency_key, params_hash, status, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.action_kind,
                    receipt.action_id,
                    receipt.idempotency_key,
                    receipt.params_hash,
                    receipt.status,
                    json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
                    "2000-01-01 00:00:00",
                ),
            )
            project.db.commit()

            before_receipts = _table_count(project, "receipts")
            # The plain body has no stale-running clearance/recovery: an existing receipt for
            # the key replays as-is (here the injected stale "running" receipt) without polling
            # the provider, deleting the receipt, or writing source rows/runs.
            result = run_action_spec(project, action, project_id=PROJECT_ID)
            assert result.status == "running"
            assert result.receipt_id == receipt.receipt_id
            assert poller.calls == 0
            assert _table_count(project, "receipts") == before_receipts
            assert _table_count(project, "source_runs") == 0
            assert _table_count(project, "source_items") == 0
            assert (
                project.db.execute(
                    "SELECT id FROM receipts WHERE id=?",
                    (receipt.receipt_id,),
                ).fetchone()
                is not None
            )
        finally:
            project.close()
    finally:
        unregister_source_poller("fake")

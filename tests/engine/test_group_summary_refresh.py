"""Grouped refresh uses one paid execution and stable derived row identities."""

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.group_summary_action import run_typed_group_summary_action
from frisket.engine.executor.sheet_refresh_action import (
    run_typed_sheet_refresh_action,
    sheet_supports_refresh,
)
from frisket.engine.store import Project
from frisket.engine.store.staleness import sheet_freshness
from test_reduce_group_summary_executor import (
    _seed,
    _reduce_action,
    _bound_request,
    _summary_router,
)


def test_group_refresh_reuses_sheet_rows_and_model_owner(
    tmp_path, make_project, caplog
):
    caplog.set_level("DEBUG", logger="frisket.executor")
    project = make_project("group")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    assert sheet_supports_refresh(project, sheet)
    rows = project.visible_row_ids(sheet)
    bound = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet},
            "idempotency_key": "refresh-one",
        }
    )
    refresh = run_typed_sheet_refresh_action(project, "p", bound, router)
    assert refresh.status == "completed", (refresh.errors, caplog.text)
    assert refresh.outputs[0].sheet_id == sheet
    assert project.visible_row_ids(sheet) == rows
    assert refresh.run_id != original.run_id
    assert sheet_freshness(project, sheet) == "synced"
    assert (
        project.db.execute(
            "SELECT refresh_claim_token FROM sheets WHERE id=?", (sheet,)
        ).fetchone()[0]
        is None
    )
    assert len(adapter.requests) == 4
    assert len(project.db.execute("SELECT id FROM sheets").fetchall()) == 2


@pytest.mark.parametrize("upgraded", [False, True])
def test_group_refresh_preserves_manual_and_retains_result_for_reapply(
    tmp_path, upgraded
):
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "manual.frisket", name="manual")
    if upgraded:
        from tests.engine.historical_schema import restore_pre_domain_schema

        restore_pre_domain_schema(project.db)
        project.close()
        project = Project(tmp_path / "manual.frisket")
    try:
        seeded = _seed(project, tmp_path)
        router, adapter = _summary_router()
        original = run_typed_group_summary_action(
            project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
        )
        assert original.status == "completed", original.errors
        sheet = original.outputs[0].sheet_id
        row = project.visible_row_ids(sheet)[0]
        summary = next(
            int(c["id"]) for c in project.columns(sheet) if c["name"] == "summary"
        )
        project.apply_edits(
            [{"row_id": row, "column_id": summary, "value": "My correction"}]
        )
        complete = adapter.complete

        async def changed(req, client):
            assert not project.db.in_transaction
            response = await complete(req, client)
            response.content = "New model answer"
            return response

        adapter.complete = changed
        bound = typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet},
                "idempotency_key": "manual-refresh",
            }
        )
        result = run_typed_sheet_refresh_action(project, "p", bound, router)
        assert result.status == "completed", result.errors
        assert project.get_values(sheet, summary)[row] == "My correction"
        saved = project.db.execute(
            "SELECT value,outcome,publication_effect,published FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (result.run_id, row, summary),
        ).fetchone()
        assert tuple(saved) == ("New model answer", "ok", "retain", 0)
        reapplied = run_action_spec(
            project,
            {
                "action_id": "run.reapply",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source_run_id": result.run_id},
                "output_names": {"summary": "saved summary"},
                "idempotency_key": "reapply-retained",
            },
            project_id="p",
        )
        assert reapplied.status == "completed", reapplied.errors
        copied = next(
            int(c["id"]) for c in project.columns(sheet) if c["name"] == "saved summary"
        )
        assert project.get_values(sheet, copied)[row] == "New model answer"
        assert project.undo() is not None
        assert project.get_values(sheet, summary)[row] == "My correction"
        assert project.undo() is not None
        assert project.get_values(sheet, summary)[row] == "My correction"
    finally:
        project.close()


def test_empty_complete_group_refresh_hides_old_members_without_model_calls(
    tmp_path, make_project
):
    from frisket.engine.executor.actions import run_action_spec

    project = make_project("empty")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    target = original.outputs[0].sheet_id
    deleted = run_action_spec(
        project,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": seeded["sheet_id"],
                "row_ids": seeded["source_row_ids"],
            },
            "idempotency_key": "delete-sources",
        },
        project_id="p",
    )
    assert deleted.status == "completed", deleted.errors
    bound = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": target},
            "idempotency_key": "empty-refresh",
        }
    )
    result = run_typed_sheet_refresh_action(project, "p", bound, router)
    assert result.status == "completed", result.errors
    assert project.visible_row_ids(target) == []
    assert len(adapter.requests) == 2
    assert project.undo() is not None
    assert len(project.visible_row_ids(target)) == 2


def test_group_refresh_estimate_matches_gate_without_model_or_writes(
    tmp_path, make_project
):
    from decimal import Decimal
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.execution.consent_coverage import ConsentCoverage
    from frisket.server.services.action_previews import ActionPreviewService

    project = make_project("estimate")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    coverage = ConsentCoverage("test:group-refresh", Decimal("0"))

    class Workspace:
        edition = "solo"

        def executor_deps_factory(self, *args):
            return ExecutorDeps(consent_coverage=coverage)

        def get(self, project_id):
            return project

        def router_for(self, project):
            raise AssertionError("estimate cannot construct provider transport")

    body = {
        "action_id": "sheet.refresh",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet},
        "idempotency_key": "estimate-refresh",
    }
    before = tuple(project.db.iterdump())
    estimate = ActionPreviewService(Workspace()).estimate("p", body)["estimate"]
    assert estimate["cost"] > 0
    assert estimate["requires_confirmation"]
    refusal = run_typed_sheet_refresh_action(
        project,
        "p",
        typed_action_for_request(body),
        router,
        deps=ExecutorDeps(consent_coverage=coverage),
    )
    assert refusal.status == "needs_confirmation"
    assert refusal.errors[0].details["promise_set_hash"] == estimate["promise_set_hash"]
    assert tuple(project.db.iterdump()) == before
    assert len(adapter.requests) == 2


def test_group_refresh_author_exception_cannot_purchase_model_work(
    tmp_path, make_project
):
    from frisket.actions.types import SheetRefresher, RefreshedSheet
    from test_typed_sheet_refresh import _bound, _DerivedParams

    project = make_project("author")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    target = original.outputs[0].sheet_id

    def bad(params: _DerivedParams, sheets: SheetRefresher) -> RefreshedSheet:
        sheets.refresh(int(params.reference))
        raise ValueError("author failure")

    before = tuple(project.db.iterdump())
    result = run_typed_sheet_refresh_action(
        project, "p", _bound(bad, {"reference": str(target)}), router
    )
    assert result.status == "failed"
    assert tuple(project.db.iterdump()) == before
    assert len(adapter.requests) == 2


def test_group_refresh_failure_is_saved_without_erasing_manual_value(
    tmp_path, make_project
):
    from test_reduce_group_summary_executor import _failing_router

    project = make_project("failed")
    seeded = _seed(project, tmp_path)
    router, _ = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    target = original.outputs[0].sheet_id
    row = project.visible_row_ids(target)[0]
    summary = next(c["id"] for c in project.columns(target) if c["name"] == "summary")
    project.apply_edits([{"row_id": row, "column_id": summary, "value": "Manual"}])
    failure, _ = _failing_router()
    body = {
        "action_id": "sheet.refresh",
        "scope": {"kind": "project"},
        "params": {"sheet_id": target},
        "idempotency_key": "failure-refresh",
    }
    failed = run_typed_sheet_refresh_action(
        project, "p", typed_action_for_request(body), failure
    )
    assert failed.status == "failed", failed.errors
    assert failed.run_id is not None
    saved = project.db.execute(
        "SELECT outcome,publication_effect FROM results WHERE run_id=? AND row_id=?",
        (failed.run_id, row),
    ).fetchone()
    assert tuple(saved) == ("model_error", "retain")
    assert project.get_values(target, summary)[row] == "Manual"
    assert project.undo() is not None
    assert project.get_values(target, summary)[row] == "Manual"


def test_group_refresh_rechecks_original_action_in_current_edition(
    tmp_path, make_project, monkeypatch
):
    from frisket.execution import action_admission

    project = make_project("edition")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    target = original.outputs[0].sheet_id
    observed = []

    def available(kind, edition):
        observed.append((kind, edition))
        return kind != "reduce.group_summary"

    monkeypatch.setattr(action_admission, "action_available_in_edition", available)
    bound = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": target},
            "idempotency_key": "edition-refresh",
        }
    )
    before = tuple(project.db.iterdump())
    result = run_typed_sheet_refresh_action(
        project, "p", bound, router, edition="cloud"
    )
    assert result.status == "failed"
    assert result.errors[0].code == "action_unavailable_in_edition"
    assert observed == [("reduce.group_summary", "cloud")]
    assert tuple(project.db.iterdump()) == before
    assert len(adapter.requests) == 2


def test_group_results_publish_before_next_call_and_stop_keeps_completed(
    tmp_path, make_project
):
    from frisket.engine.store.runs import RunResultStore

    project = make_project("stream")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    complete = adapter.complete
    calls = 0
    outputs = {}

    async def stop_second(req, client):
        nonlocal calls
        calls += 1
        assert not project.db.in_transaction
        run = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sheet = int(run["sheet_id"])
        rows = project.visible_row_ids(sheet)
        summary = next(
            int(c["id"]) for c in project.columns(sheet) if c["name"] == "summary"
        )
        outputs.update(sheet=sheet, rows=rows, summary=summary, run=int(run["id"]))
        if calls == 2:
            values = project.get_values(sheet, summary)
            assert values[rows[0]] is not None
            assert values[rows[1]] is None
            RunResultStore(project).request_cancel(int(run["id"]))
        return await complete(req, client)

    adapter.complete = stop_second
    result = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert result.status == "cancelled", result.errors
    assert sheet_freshness(project, outputs["sheet"]) == "unknown"
    values = project.get_values(outputs["sheet"], outputs["summary"])
    assert values[outputs["rows"][0]] is not None
    assert values[outputs["rows"][1]] is None
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (outputs["run"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (outputs["run"],)
        ).fetchone()[0]
        == 2
    )
    assert project.undo() is not None
    assert project.visible_row_ids(outputs["sheet"]) == []


def test_group_refresh_failure_does_not_retire_absent_groups(tmp_path, make_project):
    from test_reduce_group_summary_executor import _failing_router

    project = make_project("partial")
    seeded = _seed(project, tmp_path)
    router, _ = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    original_rows = project.visible_row_ids(sheet)
    source_rows = project.visible_row_ids(seeded["sheet_id"])
    # Remove the sole education source group; provider failure in the
    # remaining group must not infer completion of the refreshed sheet.
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (source_rows[-1],))
    project.db.commit()
    failing_router, _ = _failing_router()
    bound = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet},
            "idempotency_key": "partial-refresh",
        }
    )
    result = run_typed_sheet_refresh_action(project, "p", bound, failing_router)
    assert result.status == "failed", result.errors
    assert project.visible_row_ids(sheet) == original_rows


def test_failed_initial_summary_then_manual_is_preserved_without_baseline(
    tmp_path, make_project
):
    from test_reduce_group_summary_executor import _failing_router

    project = make_project("no-baseline")
    seeded = _seed(project, tmp_path)
    failing, _ = _failing_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), failing
    )
    assert original.status == "failed"
    sheet = original.outputs[0].sheet_id
    row = project.visible_row_ids(sheet)[0]
    summary = next(
        int(c["id"]) for c in project.columns(sheet) if c["name"] == "summary"
    )
    project.apply_edits(
        [{"row_id": row, "column_id": summary, "value": "Manual correction"}]
    )
    success, _ = _summary_router()
    for key, router in [("failed-again", failing), ("success-later", success)]:
        bound = typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet},
                "idempotency_key": key,
            }
        )
        result = run_typed_sheet_refresh_action(project, "p", bound, router)
        assert project.get_values(sheet, summary)[row] == "Manual correction", (
            result.errors
        )
        assert (
            project.db.execute(
                "SELECT publication_effect FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                (result.run_id, row, summary),
            ).fetchone()[0]
            == "retain"
        )


def test_custom_group_refresh_restores_its_own_absent_members(tmp_path, make_project):
    from test_typed_sheet_refresh import _bound, _derived_refresh

    project = make_project("custom-return")
    seeded = _seed(project, tmp_path)
    router, _ = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    members = project.visible_row_ids(sheet)
    project.db.execute(
        "UPDATE rows SET hidden=1 WHERE sheet_id=?", (seeded["sheet_id"],)
    )
    project.db.commit()
    gone = run_typed_sheet_refresh_action(
        project,
        "p",
        _bound(_derived_refresh, {"reference": f"sheet:{sheet}"}, key="custom-empty"),
        router,
    )
    assert gone.status == "completed", gone.errors
    assert project.visible_row_ids(sheet) == []
    project.db.execute(
        "UPDATE rows SET hidden=0 WHERE sheet_id=?", (seeded["sheet_id"],)
    )
    project.db.commit()
    returned = run_typed_sheet_refresh_action(
        project,
        "p",
        _bound(_derived_refresh, {"reference": f"sheet:{sheet}"}, key="custom-return"),
        router,
    )
    assert returned.status == "completed", returned.errors
    assert project.visible_row_ids(sheet) == members


def test_hidden_group_summary_only_publishes_with_its_restore_decision(
    tmp_path, make_project
):
    from frisket.engine.executor.actions import run_action_spec

    project = make_project("hidden-group")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    row = project.visible_row_ids(sheet)[0]
    summary = next(
        int(c["id"]) for c in project.columns(sheet) if c["name"] == "summary"
    )
    deleted = run_action_spec(
        project,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet, "row_ids": [row]},
            "idempotency_key": "hide-group",
        },
        project_id="p",
    )
    assert deleted.status == "completed", deleted.errors

    def request(key):
        return typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet, "update_policy": "incoming_wins"},
                "idempotency_key": key,
            }
        )

    unchanged = run_typed_sheet_refresh_action(
        project, "p", request("same-incoming"), router
    )
    assert unchanged.status == "completed", unchanged.errors
    assert row not in project.visible_row_ids(sheet)
    assert (
        project.db.execute(
            "SELECT publication_effect FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (unchanged.run_id, row, summary),
        ).fetchone()[0]
        == "retain"
    )
    complete = adapter.complete

    async def changed(req, client):
        response = await complete(req, client)
        response.content = "Changed summary"
        return response

    adapter.complete = changed
    refreshed = run_typed_sheet_refresh_action(
        project, "p", request("changed-incoming"), router
    )
    assert refreshed.status == "completed", refreshed.errors
    assert row in project.visible_row_ids(sheet)
    assert project.get_values(sheet, summary)[row] == "Changed summary"
    assert project.undo() is not None
    assert row not in project.visible_row_ids(sheet)


def test_group_refresh_crash_recovery_excludes_only_its_pending_mask(
    tmp_path, make_project, monkeypatch
):
    from frisket.engine.store.runs import RunResultStore

    project = make_project("recovered-refresh")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    rows = project.visible_row_ids(sheet)
    summary = next(
        int(c["id"]) for c in project.columns(sheet) if c["name"] == "summary"
    )
    complete = adapter.complete

    async def changed(req, client):
        response = await complete(req, client)
        response.content = "New summary"
        return response

    adapter.complete = changed
    publish = RunResultStore.publish_result_batch
    calls = 0

    def interrupted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("process interrupted before second publication")
        return publish(self, *args, **kwargs)

    monkeypatch.setattr(RunResultStore, "publish_result_batch", interrupted)
    bound = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet},
            "idempotency_key": "recover-refresh",
        }
    )
    failed = run_typed_sheet_refresh_action(project, "p", bound, router)
    assert failed.status == "failed"
    assert project.get_values(sheet, summary)[rows[0]] == "New summary"
    project.apply_edits(
        [{"row_id": row, "column_id": summary, "value": "Later manual"} for row in rows]
    )
    resumed = run_typed_sheet_refresh_action(project, "p", bound, router)
    assert resumed.status == "completed", resumed.errors
    assert len(adapter.requests) == 4
    assert list(project.get_values(sheet, summary).values()) == [
        "Later manual",
        "Later manual",
    ]
    assert (
        project.db.execute(
            "SELECT publication_effect FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (resumed.run_id, rows[1], summary),
        ).fetchone()[0]
        == "retain"
    )


def test_undo_during_group_call_keeps_accounting_and_never_reopens_output(
    tmp_path, make_project
):
    project = make_project("undo-running")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    complete = adapter.complete
    count = 0
    run_id = None

    async def undo_second(req, client):
        nonlocal count, run_id
        count += 1
        run = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run_id = int(run["id"])
        if count == 2:
            assert project.undo() is not None
        return await complete(req, client)

    adapter.complete = undo_second
    result = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert result.status == "cancelled", result.errors
    assert (
        project.db.execute("SELECT COUNT(*) FROM sheets WHERE hidden=0").fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 2
    )
    owner = project.db.execute(
        "SELECT op.status,op.publication_closed FROM ops op JOIN runs r ON r.op_id=op.id WHERE r.id=?",
        (run_id,),
    ).fetchone()
    assert tuple(owner) == ("undone", 1)


def test_group_recovers_created_sheet_before_first_result_generation(
    tmp_path, make_project, monkeypatch
):
    from frisket.engine.executor import group_summary_publication

    project = make_project("metadata-resume")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    reconcile = group_summary_publication.reconcile_rows_into_open_change
    first = True

    def interrupted(*args, **kwargs):
        nonlocal first
        result = reconcile(*args, **kwargs)
        if first:
            first = False
            raise RuntimeError("crash during first metadata page")
        return result

    monkeypatch.setattr(
        group_summary_publication, "reconcile_rows_into_open_change", interrupted
    )
    bound = _bound_request(_reduce_action(seeded["sheet_id"]))
    failed = run_typed_group_summary_action(project, "p", bound, router)
    assert failed.status == "failed"
    assert len(adapter.requests) == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM run_output_generations").fetchone()[0]
        == 0
    )
    resumed = run_typed_group_summary_action(project, "p", bound, router)
    assert resumed.status == "completed", resumed.errors
    assert len(adapter.requests) == 2
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 2


def test_group_checkpoint_identity_does_not_use_the_display_label(
    tmp_path, make_project
):
    project = make_project("typed-groups")
    seeded = _seed(project, tmp_path)
    rows = project.visible_row_ids(seeded["sheet_id"])
    group_column = next(
        int(c["id"]) for c in project.columns(seeded["sheet_id"]) if c["name"] == "beat"
    )
    project.apply_edits(
        [
            {"row_id": row, "column_id": group_column, "value": value}
            for row, value in zip(rows, [None, "null", "Other"], strict=True)
        ]
    )
    router, adapter = _summary_router()
    original = run_typed_group_summary_action(
        project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
    )
    assert original.status == "completed", original.errors
    sheet = original.outputs[0].sheet_id
    assert len(project.visible_row_ids(sheet)) == 3
    assert len(adapter.requests) == 3
    refresh = run_typed_sheet_refresh_action(
        project,
        "p",
        typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet},
                "idempotency_key": "typed-refresh",
            }
        ),
        router,
    )
    assert refresh.status == "completed", refresh.errors
    assert len(project.visible_row_ids(sheet)) == 3
    assert len(adapter.requests) == 6


def test_recovery_preserves_both_actual_read_versions_across_source_edit(
    tmp_path, make_project, monkeypatch
):
    from frisket.engine.store.runs import RunResultStore
    from frisket.engine.store.staleness import sheet_freshness

    project = make_project("mixed-capture")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    publish = RunResultStore.publish_result_batch
    first = True

    def interrupted(self, *args, **kwargs):
        nonlocal first
        result = publish(self, *args, **kwargs)
        if first:
            first = False
            raise RuntimeError("crash after first committed group")
        return result

    monkeypatch.setattr(RunResultStore, "publish_result_batch", interrupted)
    bound = _bound_request(_reduce_action(seeded["sheet_id"]))
    failed = run_typed_group_summary_action(project, "p", bound, router)
    assert failed.status == "failed"
    assert len(adapter.requests) == 1
    source_rows = project.visible_row_ids(seeded["sheet_id"])
    story = next(
        int(c["id"])
        for c in project.columns(seeded["sheet_id"])
        if c["name"] == "story"
    )
    project.apply_edits(
        [
            {
                "row_id": source_rows[-1],
                "column_id": story,
                "value": "New source read after recovery",
            }
        ]
    )
    resumed = run_typed_group_summary_action(project, "p", bound, router)
    assert resumed.status == "completed", resumed.errors
    assert len(adapter.requests) == 2
    sheet = resumed.outputs[0].sheet_id
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM consumed_source_bundles WHERE run_id=? AND kind='aggregate'",
            (resumed.run_id,),
        ).fetchone()[0]
        == 2
    )
    assert sheet_freshness(project, sheet) == "stale"
    assert project.undo() is not None
    assert sheet_freshness(project, sheet) == "stale"


def test_group_final_receipt_failure_recovers_saved_groups_without_rebuying(
    tmp_path, make_project
):

    project = make_project("receipt-resume")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    bound = _bound_request(_reduce_action(seeded["sheet_id"]))
    project.db.execute(
        "CREATE TRIGGER fail_final_receipt BEFORE UPDATE ON receipts "
        "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'receipt write interrupted'); END"
    )
    project.db.commit()
    first = run_typed_group_summary_action(project, "p", bound, router)
    assert first.status == "failed"
    assert len(adapter.requests) == 2
    original_run = project.db.execute("SELECT id FROM runs").fetchone()[0]
    original_facts = [
        dict(row) for row in project.db.execute("SELECT * FROM model_calls")
    ]
    project.db.execute("DROP TRIGGER fail_final_receipt")
    project.db.commit()
    resumed = run_typed_group_summary_action(project, "p", bound, router)
    assert resumed.status == "completed", resumed.errors
    assert resumed.run_id == original_run
    assert len(adapter.requests) == 2
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert [
        dict(row) for row in project.db.execute("SELECT * FROM model_calls")
    ] == original_facts
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


def test_group_refresh_claim_conflict_cannot_publish_structural_work(tmp_path):
    from helpers import run_writer_authority_fixture
    from frisket.engine.store.runs import RunResultStore
    from contextlib import closing

    with closing(Project.create(tmp_path / "claim-conflict.frisket")) as project:
        seeded = _seed(project, tmp_path)
        router, adapter = _summary_router()
        original = run_typed_group_summary_action(
            project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
        )
        assert original.status == "completed", original.errors
        target = original.outputs[0].sheet_id
        summary = next(
            c["id"] for c in project.columns(target) if c["name"] == "summary"
        )
        source_columns = {
            c["name"]: c["id"] for c in project.columns(seeded["sheet_id"])
        }
        # A changed group count would publish a real metadata write if the
        # output claim were checked after reconciliation instead of before it.
        first = project.visible_row_ids(seeded["sheet_id"])[0]
        incoming = {
            name: project.get_values(seeded["sheet_id"], column)[first]
            for name, column in source_columns.items()
        }
        project.add_rows(seeded["sheet_id"], [incoming], source_columns)
        owner = project.append_change("competing-summary")
        run = RunResultStore(project).start_run(
            owner, target, "map.ask", row_ids=project.visible_row_ids(target)
        )
        run_writer_authority_fixture(project, run, output_column_ids={summary})
        before_cells = project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
        before_observations = [
            tuple(r)
            for r in project.db.execute(
                "SELECT * FROM keyed_observations ORDER BY relationship_id,row_id"
            )
        ]
        request = typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": target, "update_policy": "incoming_wins"},
                "idempotency_key": "blocked-refresh",
            }
        )
        refused = run_typed_sheet_refresh_action(project, "p", request, router)
        assert refused.status == "failed", refused
        assert "claimed" in refused.errors[0].message
        assert len(adapter.requests) == 2
        assert (
            project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
            == before_cells
        )
        assert [
            tuple(r)
            for r in project.db.execute(
                "SELECT * FROM keyed_observations ORDER BY relationship_id,row_id"
            )
        ] == before_observations
        assert (
            project.db.execute(
                "SELECT refresh_claim_token FROM sheets WHERE id=?", (target,)
            ).fetchone()[0]
            is None
        )


def test_group_refresh_holds_shared_target_lease_during_provider_work(tmp_path):
    from contextlib import closing

    with closing(Project.create(tmp_path / "refresh-lease.frisket")) as project:
        seeded = _seed(project, tmp_path)
        router, adapter = _summary_router()
        original = run_typed_group_summary_action(
            project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
        )
        assert original.status == "completed", original.errors
        sheet = original.outputs[0].sheet_id
        complete = adapter.complete
        seen = []

        async def while_owned(req, client):
            request = typed_action_for_request(
                {
                    "action_id": "sheet.refresh",
                    "scope": {"kind": "project"},
                    "params": {"sheet_id": sheet},
                    "idempotency_key": "concurrent-refresh",
                }
            )
            competing = run_typed_sheet_refresh_action(project, "p", request, router)
            assert competing.status == "failed"
            assert competing.errors[0].code == "sheet_refresh_busy"
            seen.append(True)
            return await complete(req, client)

        adapter.complete = while_owned
        request = typed_action_for_request(
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet},
                "idempotency_key": "owning-refresh",
            }
        )
        result = run_typed_sheet_refresh_action(project, "p", request, router)
        assert result.status == "completed", result.errors
        assert len(seen) == 2
        assert (
            project.db.execute(
                "SELECT refresh_claim_token FROM sheets WHERE id=?", (sheet,)
            ).fetchone()[0]
            is None
        )


def test_group_runner_declaration_failure_recovers_same_sheet_before_dispatch(
    tmp_path, make_project, monkeypatch
):
    from frisket.engine.store.result_generations import ResultGenerationStore

    project = make_project("declare-recovery")
    seeded = _seed(project, tmp_path)
    router, adapter = _summary_router()
    bound = _bound_request(_reduce_action(seeded["sheet_id"]))
    declare = ResultGenerationStore.declare
    interrupted = False

    def fail_once(self, *args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("interrupted before runner output declaration")
        return declare(self, *args, **kwargs)

    monkeypatch.setattr(ResultGenerationStore, "declare", fail_once)
    failed = run_typed_group_summary_action(project, "p", bound, router)
    assert failed.status == "failed"
    assert adapter.requests == []
    rows = [
        tuple(row)
        for row in project.db.execute("SELECT id,sheet_id FROM rows ORDER BY id")
    ]
    run_id = project.db.execute("SELECT id FROM runs").fetchone()[0]
    recovered = run_typed_group_summary_action(project, "p", bound, router)
    assert recovered.status == "completed", recovered.errors
    assert recovered.run_id == run_id
    assert len(adapter.requests) == 2
    assert [
        tuple(row)
        for row in project.db.execute("SELECT id,sheet_id FROM rows ORDER BY id")
    ] == rows
    assert project.undo() is not None
    assert (
        project.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (recovered.outputs[0].sheet_id,)
        ).fetchone()[0]
        == 1
    )

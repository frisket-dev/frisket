"""Grouped refresh uses one paid execution and stable derived row identities."""

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.group_summary_action import run_typed_group_summary_action
from frisket.engine.executor.sheet_refresh_action import (
    run_typed_sheet_refresh_action,
    sheet_supports_refresh,
)
from frisket.engine.store import Project
from test_reduce_group_summary_executor import (
    _seed,
    _reduce_action,
    _bound_request,
    _summary_router,
)


def test_group_refresh_reuses_sheet_rows_and_model_owner(tmp_path, caplog):
    caplog.set_level("DEBUG", logger="frisket.executor")
    project = Project.create(tmp_path / "group.frisket", name="group")
    try:
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
        assert len(adapter.requests) == 4
        assert len(project.db.execute("SELECT id FROM sheets").fetchall()) == 2
    finally:
        project.close()


def test_group_refresh_preserves_manual_and_retains_result_for_reapply(tmp_path):
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "manual.frisket", name="manual")
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


def test_empty_complete_group_refresh_hides_old_members_without_model_calls(tmp_path):
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "empty.frisket", name="empty")
    try:
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
    finally:
        project.close()


def test_group_refresh_estimate_matches_gate_without_model_or_writes(tmp_path):
    from decimal import Decimal
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.execution.consent_coverage import ConsentCoverage
    from frisket.server.services.action_previews import ActionPreviewService

    project = Project.create(tmp_path / "estimate.frisket", name="estimate")
    try:
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
        assert (
            refusal.errors[0].details["promise_set_hash"]
            == estimate["promise_set_hash"]
        )
        assert tuple(project.db.iterdump()) == before
        assert len(adapter.requests) == 2
    finally:
        project.close()


def test_group_refresh_author_exception_cannot_purchase_model_work(tmp_path):
    from frisket.actions.types import SheetRefresher, RefreshedSheet
    from test_typed_sheet_refresh import _bound, _DerivedParams

    project = Project.create(tmp_path / "author.frisket", name="author")
    try:
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
    finally:
        project.close()


def test_group_refresh_failure_is_saved_without_erasing_manual_value(tmp_path):
    from test_reduce_group_summary_executor import _failing_router

    project = Project.create(tmp_path / "failed.frisket", name="failed")
    try:
        seeded = _seed(project, tmp_path)
        router, _ = _summary_router()
        original = run_typed_group_summary_action(
            project, "p", _bound_request(_reduce_action(seeded["sheet_id"])), router
        )
        assert original.status == "completed", original.errors
        target = original.outputs[0].sheet_id
        row = project.visible_row_ids(target)[0]
        summary = next(
            c["id"] for c in project.columns(target) if c["name"] == "summary"
        )
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
    finally:
        project.close()


def test_group_refresh_rechecks_original_action_in_current_edition(
    tmp_path, monkeypatch
):
    from frisket.execution import action_admission

    project = Project.create(tmp_path / "edition.frisket", name="edition")
    try:
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
    finally:
        project.close()

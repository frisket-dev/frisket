"""Viewed changed aggregates use the ordinary queue; edits never scan."""

from frisket.engine.jobs import Worker
from frisket.engine.jobs.queue import SOURCE_VERIFY_KIND
from frisket.engine.store.changes import start_change
from frisket.engine.store.consumed_sources import (
    bundle_state,
    read_consumed_source,
    store_capture,
)
from frisket.server.workspace import Workspace


def test_view_requests_coalesced_job_and_undo_restores_freshness(tmp_path, monkeypatch):
    import frisket.engine.store.consumed_sources as captures

    monkeypatch.setattr(captures, "INLINE_CHECK_ROWS", 2)
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = workspace.create("Sources")["id"]
    project = workspace.get(project_id)
    try:
        sheet = project.add_sheet("source")
        column = project.add_column(sheet, "text")
        rows = project.add_rows(
            sheet, [{"text": f"v{i}"} for i in range(4)], {"text": column}
        )
        _, _, capture = read_consumed_source(project, sheet, [column], batch_size=2)
        bundles = []
        for name in ("first", "shared"):
            owner = project.append_op("test." + name, {})
            project.db.execute("BEGIN IMMEDIATE")
            start_change(project.db, owner)
            bundles.append(
                store_capture(
                    project, capture, owner_op_id=owner, output_sheet_id=sheet
                )
            )
            project.db.commit()
        assert all(bundle_state(project, bid) == "fresh" for bid in bundles)
        project.apply_edits([{"row_id": rows[0], "column_id": column, "value": "new"}])
        assert not [
            job for job in workspace.queue.list_jobs() if job.kind == SOURCE_VERIFY_KIND
        ]
        request = project._frisket_request_source_verification
        assert all(
            bundle_state(project, bid, request_verification=request) == "checking"
            for bid in bundles
        )
        checks = [
            job for job in workspace.queue.list_jobs() if job.kind == SOURCE_VERIFY_KIND
        ]
        assert len(checks) == 1
        worker = Worker(workspace.queue, workspace.registry)
        for _ in range(20):
            if workspace.queue.get(checks[0].id).status == "done":
                break
            assert worker.run_once()
        assert workspace.queue.get(checks[0].id).status == "done"
        assert all(bundle_state(project, bid) == "stale" for bid in bundles)
        project.undo()
        assert (
            bundle_state(project, bundles[0], request_verification=request)
            == "checking"
        )
        newest = max(
            (
                job
                for job in workspace.queue.list_jobs()
                if job.kind == SOURCE_VERIFY_KIND
            ),
            key=lambda job: job.id,
        )
        for _ in range(20):
            if workspace.queue.get(newest.id).status == "done":
                break
            assert worker.run_once()
        assert all(bundle_state(project, bid) == "fresh" for bid in bundles)
        project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "cancelled check"}]
        )
        assert (
            bundle_state(project, bundles[0], request_verification=request)
            == "checking"
        )
        latest = max(
            (
                job
                for job in workspace.queue.list_jobs()
                if job.kind == SOURCE_VERIFY_KIND
            ),
            key=lambda job: job.id,
        )
        assert workspace.queue.cancel(latest.id)
        assert (
            bundle_state(project, bundles[0], request_verification=request) == "unknown"
        )
        assert (
            len(
                [
                    job
                    for job in workspace.queue.list_jobs()
                    if job.kind == SOURCE_VERIFY_KIND
                ]
            )
            == 3
        )
    finally:
        project.close()

"""Visual extraction compares the positioned artifact actually consumed."""

import json

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.document_extraction_read import load_positioned_document
from frisket.engine.store.consumed_sources import positioned_source_revision
from frisket.engine.store.citations import (
    record_source_artifact,
    record_source_span,
    merge_artifact_metadata,
)
from frisket.engine.store.staleness import sheet_freshness
from test_visual_document_extraction_action import seed, request
from frisket.engine.store.transactions import write_transaction


@pytest.fixture
def positioned(make_project):
    project = make_project("positioned")
    source, column, rows, template = seed(project, count=1)
    result = run_action_spec(project, request(source, template), project_id="capture")
    assert result.status == "completed", result.errors
    sheet = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    blob = project.get_values(source, column)[rows[0]]["blob"]
    artifact = load_positioned_document(project, blob).artifact_id
    assert sheet_freshness(project, sheet) == "synced"
    yield project, sheet, blob, artifact


def _artifact(project, blob):
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        blob_hash=blob,
        media_type="image/png",
        metadata={
            "engine": "tesseract",
            "page_images": {"1": {"source_width": 100, "source_height": 100}},
        },
    )
    for text, y, x0, x1 in [
        ("NAME", 0.1, 0.1, 0.2),
        ("Replacement", 0.1, 0.35, 0.55),
        ("ARRESTED", 0.2, 0.1, 0.3),
        ("X", 0.2, 0.35, 0.4),
    ]:
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="region",
            page_start=1,
            page_end=1,
            bbox=[
                {
                    "x0": x0,
                    "y0": y,
                    "x1": x1,
                    "y1": y + 0.03,
                    "space": "page_normalized",
                }
            ],
            quote=text,
        )
    return artifact


def test_selected_artifact_replacement_and_restoration_are_reversible(positioned):
    project, sheet, blob, original = positioned
    with write_transaction(project.db):
        newer = _artifact(project, blob)
    assert load_positioned_document(project, blob).artifact_id == newer["id"]
    assert sheet_freshness(project, sheet) == "stale"
    # The existing metadata API can make a candidate unavailable; selecting
    # the original immutable artifact again restores the consumed context.
    with write_transaction(project.db):
        merge_artifact_metadata(project, newer["id"], {"engine": None})
    assert load_positioned_document(project, blob).artifact_id == original
    assert sheet_freshness(project, sheet) == "synced"


def test_new_spans_on_same_artifact_change_consumed_version(positioned):
    project, sheet, blob, artifact = positioned
    before = load_positioned_document(project, blob).document.source_fingerprint
    with write_transaction(project.db):
        record_source_span(
            project,
            artifact_id=artifact,
            span_kind="region",
            page_start=1,
            page_end=1,
            bbox=[
                {
                    "x0": 0.7,
                    "y0": 0.7,
                    "x1": 0.8,
                    "y1": 0.73,
                    "space": "page_normalized",
                }
            ],
            quote="Added source word",
        )
    assert load_positioned_document(project, blob).artifact_id == artifact
    assert load_positioned_document(project, blob).document.source_fingerprint != before
    assert sheet_freshness(project, sheet) == "stale"


def test_equal_text_replacement_span_has_a_different_contribution_identity(positioned):
    project, sheet, blob, artifact = positioned
    before = load_positioned_document(project, blob).document.source_fingerprint
    span = project.db.execute(
        "SELECT * FROM source_spans WHERE artifact_id=? ORDER BY id DESC LIMIT 1",
        (artifact,),
    ).fetchone()
    with project.transaction_scope():
        project.db.execute("DELETE FROM source_spans WHERE id=?", (span["id"],))
        replacement = record_source_span(
            project,
            artifact_id=artifact,
            span_kind="region",
            page_start=span["page_start"],
            page_end=span["page_end"],
            bbox=json.loads(span["bbox_json"]),
            quote=span["quote"],
        )
    assert replacement["id"] != span["id"]
    # Keep the public layout-content fingerprint stable; freshness additionally
    # binds the actual immutable region contributions used by that layout.
    assert load_positioned_document(project, blob).document.source_fingerprint == before
    assert sheet_freshness(project, sheet) == "stale"


def test_unrelated_artifact_only_invalidates_negative_cache_guard(
    positioned, monkeypatch
):
    from frisket.engine.executor import document_extraction_read

    project, sheet, _, _ = positioned
    compare = document_extraction_read.positioned_scope_fingerprint
    calls = []

    def observe(*args, **kwargs):
        calls.append(True)
        return compare(*args, **kwargs)

    monkeypatch.setattr(
        document_extraction_read, "positioned_scope_fingerprint", observe
    )
    before = positioned_source_revision(project)
    other = project.add_blob(b"unrelated image", filename="other.png", mime="image/png")
    with write_transaction(project.db):
        _artifact(project, other)
    assert positioned_source_revision(project) > before
    assert sheet_freshness(project, sheet) == "synced"
    assert calls == [True]
    assert sheet_freshness(project, sheet) == "synced"
    assert calls == [True]


def test_rolled_back_artifact_change_does_not_advance_committed_guard(
    positioned, monkeypatch
):
    from frisket.engine.executor import document_extraction_read

    project, sheet, blob, _ = positioned
    before = positioned_source_revision(project)
    with pytest.raises(RuntimeError, match="rollback"):
        with project.transaction_scope():
            _artifact(project, blob)
            assert positioned_source_revision(project) > before
            raise RuntimeError("rollback")
    assert positioned_source_revision(project) == before

    def unexpected(*args, **kwargs):
        raise AssertionError("unchanged source guard should reuse its verified cache")

    monkeypatch.setattr(
        document_extraction_read, "positioned_scope_fingerprint", unexpected
    )
    assert sheet_freshness(project, sheet) == "synced"


def test_costly_positioned_check_uses_existing_queue_and_new_asset_revision_key(
    tmp_path, monkeypatch
):
    from frisket.server.workspace import Workspace
    from frisket.engine.jobs import Worker
    from frisket.engine.jobs.queue import SOURCE_VERIFY_KIND
    from frisket.engine.executor import document_extraction_read

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = workspace.create("Positioned")["id"]
    project = workspace.get(project_id)
    try:
        source, column, rows, template = seed(project, count=1)
        blob = project.get_values(source, column)[rows[0]]["blob"]
        artifact = load_positioned_document(project, blob).artifact_id
        with project.transaction_scope():
            for i in range(1001):
                record_source_span(
                    project,
                    artifact_id=artifact,
                    span_kind="region",
                    page_start=1,
                    page_end=1,
                    bbox=[
                        {
                            "x0": 0.8,
                            "y0": 0.8,
                            "x1": 0.9,
                            "y1": 0.83,
                            "space": "page_normalized",
                        }
                    ],
                    quote=f"padding {i}",
                )
        template = template.model_copy(
            update={
                "reference_fingerprint": load_positioned_document(
                    project, blob
                ).document.source_fingerprint
            }
        )
        result = run_action_spec(
            project, request(source, template), project_id=project_id
        )
        assert result.status == "completed", result.errors
        sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert sheet_freshness(project, sheet) == "synced"
        with write_transaction(project.db):
            word = record_source_span(
                project,
                artifact_id=artifact,
                span_kind="region",
                page_start=1,
                page_end=1,
                bbox=[
                    {
                        "x0": 0.8,
                        "y0": 0.85,
                        "x1": 0.9,
                        "y1": 0.88,
                        "space": "page_normalized",
                    }
                ],
                quote="new source word",
            )
        assert not [
            job for job in workspace.queue.list_jobs() if job.kind == SOURCE_VERIFY_KIND
        ]
        compare = document_extraction_read.positioned_scope_fingerprint
        in_grid = True
        calls = []

        def observe(*args, **kwargs):
            assert not in_grid, (
                "costly source verification must not run in the grid read"
            )
            calls.append(True)
            return compare(*args, **kwargs)

        monkeypatch.setattr(
            document_extraction_read, "positioned_scope_fingerprint", observe
        )
        assert sheet_freshness(project, sheet) == "checking"
        assert sheet_freshness(project, sheet) == "checking"
        assert calls == []
        checks = [
            job for job in workspace.queue.list_jobs() if job.kind == SOURCE_VERIFY_KIND
        ]
        assert len(checks) == 1
        in_grid = False
        worker = Worker(workspace.queue, workspace.registry)
        for _ in range(20):
            if workspace.queue.get(checks[0].id).status == "done":
                break
            assert worker.run_once()
        assert workspace.queue.get(checks[0].id).status == "done"
        assert sheet_freshness(project, sheet) == "stale"
        with project.transaction_scope():
            project.db.execute("DELETE FROM source_spans WHERE id=?", (word["id"],))
        in_grid = True
        assert sheet_freshness(project, sheet) == "checking"
        checks = [
            job for job in workspace.queue.list_jobs() if job.kind == SOURCE_VERIFY_KIND
        ]
        assert len(checks) == 2
        in_grid = False
        newest = max(checks, key=lambda job: job.id)
        for _ in range(20):
            if workspace.queue.get(newest.id).status == "done":
                break
            assert worker.run_once()
        assert workspace.queue.get(newest.id).status == "done"
        assert sheet_freshness(project, sheet) == "synced"
    finally:
        project.close()

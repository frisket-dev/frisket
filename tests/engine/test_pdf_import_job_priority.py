from __future__ import annotations

from frisket.engine.jobs.queue import (
    BACKGROUND_PRIORITY_CLASS,
    FOREGROUND_PRIORITY_CLASS,
    PDF_IMPORT_OCR_BACKGROUND,
    PROJECT_RUN_KIND,
    SqliteJobQueue,
)


def _project_run(*, project_id: str = "pdfs", background: bool = False) -> dict:
    return {
        "project_id": project_id,
        "run_id": 1,
        "action_kind": "media.ocr",
        **({"background_priority": PDF_IMPORT_OCR_BACKGROUND} if background else {}),
    }


def test_pdf_ocr_is_low_priority_until_scoped_viewer_promotion(tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "queue.db")
    try:
        background_id = queue.enqueue(PROJECT_RUN_KIND, _project_run(background=True))
        foreground_id = queue.enqueue("echo", {})

        assert queue.get(background_id).priority_class == BACKGROUND_PRIORITY_CLASS
        assert queue.get(foreground_id).priority_class == FOREGROUND_PRIORITY_CLASS
        first = queue.claim("worker")
        assert first is not None and first.id == foreground_id
        assert queue.complete(first.id, "worker")

        assert not queue.promote_project_run(
            background_id,
            project_id="another-project",
            storage_org_id=None,
            preferred_row_id=17,
        )
        assert queue.promote_project_run(
            background_id,
            project_id="pdfs",
            storage_org_id=None,
            preferred_row_id=17,
        )
        promoted = queue.claim("ocr-worker")
        assert promoted is not None and promoted.id == background_id
        assert promoted.priority_class == FOREGROUND_PRIORITY_CLASS
        assert (
            queue.take_preferred_row_id(background_id, authority_id="stale-authority")
            is None
        )
        assert (
            queue.take_preferred_row_id(
                background_id, authority_id=str(promoted.handler_authority_id)
            )
            == 17
        )
        assert (
            queue.take_preferred_row_id(
                background_id, authority_id=str(promoted.handler_authority_id)
            )
            is None
        )
    finally:
        queue.close()

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.server.services.sheet_grid import _sheet_data_payload
from frisket.engine.store import Project
from frisket.engine.store.transcript_status import compute_transcript_statuses


PROJECT_ID = "project-transcript-status-signal"
WORKFLOW_ID = "transcript-status-signal-workflow"


def _transcribe_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    columns = input_columns if input_columns is not None else ["media"]
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.transcribe",
        "scope": scope,
        "params": {
            "source": columns[0] if len(columns) == 1 else columns,
            "engine": "faster_whisper",
        },
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "idempotency_key": idempotency_key,
    }


def _seed_media_project(
    tmp_path: Path, *, label: str = "signal"
) -> tuple[Project, int, list[int], list[str]]:
    project = Project.create(tmp_path / f"{label}.frisket", name="Transcript status")
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blobs = [
        project.add_blob(
            b"RIFF0000WAVEfmt " + tag.encode("ascii"),
            filename=f"{tag}.wav",
            mime="audio/wav",
            source_url=f"https://cdn.example/{tag}.wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for tag in ("ep1", "ep2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(
                    blobs[0],
                    mime="audio/wav",
                    filename="ep1.wav",
                ),
            },
            {
                "title": "Episode 2",
                "media": media_cell(
                    blobs[1],
                    mime="audio/wav",
                    filename="ep2.wav",
                ),
            },
        ],
        cols,
    )
    return project, sheet_id, row_ids, blobs


def _media_column_id(project: Project, sheet_id: int, name: str = "media") -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=?", (sheet_id, name)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _payload_status(project: Project, sheet_id: int, column_id: int) -> Any:
    cols = project.columns(sheet_id)
    payload = _sheet_data_payload(
        project,
        sheet_id,
        cols,
        [
            c["id"]
            for c in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
            )
        ],
        total=0,
    )
    match = next(c for c in payload["columns"] if c["id"] == column_id)
    return match["transcript_status"]


def _all_ok(blobs: list[str], row_ids: list[int]):
    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        digest = Path(path).name
        label = "one" if digest == blobs[0] else "two"
        return {
            "text": f"episode {label} transcript",
            "segments": [{"start": 0.0, "end": 0.5, "text": f"episode {label}"}],
            "language": "en",
            "duration": 0.5,
        }

    return fake_faster_whisper


def _all_fail():
    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, path, spec
        raise RuntimeError("ASR provider unavailable")

    return fake_faster_whisper


def _first_ok_second_fails(blobs: list[str]):
    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        digest = Path(path).name
        if digest == blobs[1]:
            raise RuntimeError("ASR provider unavailable")
        return {
            "text": "episode one transcript",
            "segments": [{"start": 0.0, "end": 0.5, "text": "episode one"}],
            "language": "en",
            "duration": 0.5,
        }

    return fake_faster_whisper


def test_missing_when_no_transcribe_ever_ran(tmp_path: Path) -> None:
    project, sheet_id, _row_ids, _blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "missing"
        assert _payload_status(project, sheet_id, media_id) == "missing"
    finally:
        project.close()


def test_partial_when_some_rows_lack_a_successful_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, row_ids, blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _first_ok_second_fails(blobs),
        )
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:partial",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "partial"
        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "partial"
        assert _payload_status(project, sheet_id, media_id) == "partial"
    finally:
        project.close()


def test_zero_success_run_reads_as_missing_not_partial_or_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that fails on every target row hides the column it created
    (map_runner.py's all-rows-failed-no-columns-v1 zero-success rollback) —
    the status must read identically to "no transcribe ever ran", not
    surface as complete/partial from a column that was never populated."""
    project, sheet_id, row_ids, _blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter, "transcribe", _all_fail()
        )
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:zero-success",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "failed"
        transcript_column = project.db.execute(
            "SELECT hidden FROM columns WHERE sheet_id=? AND name='transcript'",
            (sheet_id,),
        ).fetchone()
        assert transcript_column is not None
        assert transcript_column["hidden"] == 1  # the auto-hide precedent

        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "missing"
        assert _payload_status(project, sheet_id, media_id) == "missing"
    finally:
        project.close()


def test_complete_hidden_after_a_full_success_the_user_later_hides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, row_ids, blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _all_ok(blobs, row_ids),
        )
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:visible-complete",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"
        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "complete_visible"
        assert _payload_status(project, sheet_id, media_id) == "complete_visible"

        transcript_column_id = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='transcript'",
            (sheet_id,),
        ).fetchone()["id"]
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (transcript_column_id,)
        )
        project.db.commit()

        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "complete_hidden"
        assert _payload_status(project, sheet_id, media_id) == "complete_hidden"
    finally:
        project.close()


def test_multi_transcribe_newest_run_wins_over_an_older_full_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First run: both rows succeed (complete_visible). Second (newest) run
    re-targets the SAME column but only row 1 succeeds. The signal must
    reflect the newest run's own outcome (partial), never merge in the
    earlier run's full success."""
    project, sheet_id, row_ids, blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _all_ok(blobs, row_ids),
        )
        first = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:multi-first",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        assert (
            compute_transcript_statuses(project, sheet_id, project.columns(sheet_id))[
                media_id
            ]
            == "complete_visible"
        )

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _first_ok_second_fails(blobs),
        )
        second = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:multi-second",
            )
            | {"replace_existing": True},
            project_id=PROJECT_ID,
        )
        assert second.status == "partial"
        assert second.run_id != first.run_id

        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "partial"
        assert _payload_status(project, sheet_id, media_id) == "partial"
    finally:
        project.close()


def test_rename_proofing_output_column_identified_by_run_not_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript OUTPUT column is identified via
    its exact run-output generation binding and transcript type (never by name —
    SS1.3), so
    renaming it after a completed run must not desync ``complete_visible``.
    Separately, the response dict itself is genuinely id-keyed (integer
    column ids, not names), and an unrelated column rename must not disturb
    the media column's entry."""
    project, sheet_id, row_ids, blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _all_ok(blobs, row_ids),
        )
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:rename-proof",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"

        # Rename an unrelated column and the transcript OUTPUT column. Both
        # are id-irrelevant to the media column's own status key.
        project.db.execute(
            "UPDATE columns SET name='episode_title' WHERE sheet_id=? AND name='title'",
            (sheet_id,),
        )
        transcript_column_id = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='transcript'",
            (sheet_id,),
        ).fetchone()["id"]
        project.db.execute(
            "UPDATE columns SET name='transcript_text' WHERE id=?",
            (transcript_column_id,),
        )
        project.db.commit()

        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert all(isinstance(cid, int) for cid in statuses)
        assert statuses[media_id] == "complete_visible"
        assert _payload_status(project, sheet_id, media_id) == "complete_visible"
    finally:
        project.close()


def test_rename_proofing_renamed_media_source_resolves_safely_to_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving ``ops.spec.input_columns[0]`` (a column NAME) against the
    CURRENT column list is the same live lookup the executor itself performs
    (SS1.2) — renaming the media column after its transcribe run means the
    stored name no longer matches any current column, the same class of
    resolution gap the executor already has. This must degrade SAFELY (the
    id-keyed status reads "missing", never a stale/wrong association or a
    crash), not silently attribute a different column's run to it."""
    project, sheet_id, row_ids, blobs = _seed_media_project(tmp_path)
    try:
        media_id = _media_column_id(project, sheet_id)
        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            _all_ok(blobs, row_ids),
        )
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="transcript_status@sha256:rename-source",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"

        project.db.execute(
            "UPDATE columns SET name='audio_source' WHERE id=?", (media_id,)
        )
        project.db.commit()

        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert statuses[media_id] == "missing"
        assert _payload_status(project, sheet_id, media_id) == "missing"
    finally:
        project.close()


def test_transcript_status_absent_for_non_media_columns(tmp_path: Path) -> None:
    project, sheet_id, _row_ids, _blobs = _seed_media_project(tmp_path)
    try:
        title_id = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='title'", (sheet_id,)
        ).fetchone()["id"]
        statuses = compute_transcript_statuses(
            project, sheet_id, project.columns(sheet_id)
        )
        assert title_id not in statuses
        assert _payload_status(project, sheet_id, title_id) is None
    finally:
        project.close()

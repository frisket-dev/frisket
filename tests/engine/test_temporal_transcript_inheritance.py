from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.engine.executor.temporal_transcripts import (
    persist_projected_transcript_evidence,
    project_transcript,
    resolve_compatible_transcripts,
    resolve_timestamped_transcript,
    revalidate_transcript,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    resolve_timeline,
    write_rate1_timeline_segment,
)
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.runs import RunResultStore
from helpers import initialize_test_source_cells, write_claimed_test_results


def _add_timestamped_transcript(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    artifact_id: int,
    name: str,
    text: str,
    span_values: list[tuple[int, int, str]],
    type_name: str = "timestamped_transcript",
    marked: bool = True,
    language: str | None = None,
):
    column_id = project.add_column(sheet_id, name, type_name, ai_generated=True)
    op_id = project.append_op(
        "media.transcribe",
        {"kind": "media.transcribe", "params": {"output_name": name}},
        label=f"transcribe {name}",
    )
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        params={"output_name": name},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": column_id, "value": text}],
    )
    runs.finish_run(run_id)
    runs.point_column_at_run(op_id, column_id, run_id)

    spans = []
    for index, (start, end, quote) in enumerate(span_values):
        span = record_source_span(
            project,
            artifact_id=artifact_id,
            span_kind="temporal",
            start_ms=start,
            end_ms=end,
            quote=quote,
            selector={"segment_index": index},
        )
        spans.append({"span_id": span["id"], "rank": index})
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    metadata = None
    if marked:
        metadata = {
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
        }
        if language is not None:
            metadata["language"] = language
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=spans,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        run_id=run_id,
        op_id=op_id,
        link_role="media_transcribe_temporal",
        producer={"action_kind": "media.transcribe"},
        metadata=metadata,
    )
    return column_id, link


def _seed_source(project: Project):
    sheet_id = project.add_sheet("Media")
    media_column_id = project.add_column(sheet_id, "video", "video")
    blob_hash = project.add_blob(
        b"source video",
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 10.0}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": "source.mp4",
                    "mime": "video/mp4",
                },
            }
        ],
        {"video": media_column_id},
    )[0]
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column_id,
    )
    transcript_column_id, link = _add_timestamped_transcript(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        artifact_id=lease.anchor.artifact_id,
        name="transcript",
        text="First sentence. Second sentence.",
        span_values=[
            (1000, 3000, "First sentence."),
            (3000, 6000, "Second sentence."),
        ],
        language="en",
    )
    source = SimpleNamespace(
        sheet_id=sheet_id,
        row_id=row_id,
        lease=lease,
    )
    return source, transcript_column_id, link


def test_resolve_project_persist_and_staleness(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "transcript-inheritance.frisket")
    try:
        source, transcript_column_id, source_link = _seed_source(project)
        resolved_items, warnings = resolve_compatible_transcripts(project, source)
        assert warnings == ()
        assert len(resolved_items) == 1
        resolved = resolved_items[0]
        assert resolved.evidence_link_stable_id == source_link["stable_id"]
        assert resolved.language == "en"

        projection = project_transcript(
            resolved, source_start_ms=2000, source_end_ms=5000
        )
        assert projection.text.count("partial speech") == 2
        assert [(item.start_ms, item.end_ms) for item in projection.segments] == [
            (0, 1000),
            (1000, 3000),
        ]

        output_column_id = project.add_column(
            source.sheet_id, "clip transcript", "timestamped_transcript"
        )
        initialize_test_source_cells(
            project,
            [(source.row_id, output_column_id, projection.text)],
        )
        derived_blob = project.add_blob(
            b"derived video", filename="clip.mp4", mime="video/mp4"
        )
        derived = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=derived_blob,
            duration_ms=3000,
        )
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=int(derived["id"]),
            source_artifact_id=source.lease.anchor.artifact_id,
            source_start_ms=2000,
            source_end_ms=5000,
            precision="exact",
        )
        op_id = project.append_op("test", {}, label="test")
        project.db.execute("BEGIN IMMEDIATE")
        evidence = persist_projected_transcript_evidence(
            project,
            transcript=resolved,
            projection=projection,
            derived_artifact_id=derived["id"],
            output_sheet_id=source.sheet_id,
            output_row_id=source.row_id,
            transcript_column_id=output_column_id,
            receipt_id="receipt-test",
            op_id=op_id,
        )
        project.db.commit()
        assert evidence is not None and evidence["span_count"] == 2
        projected = resolve_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            column_id=output_column_id,
            artifact_id=int(derived["id"]),
        )
        assert projected is not None
        assert projected.language == "en"
        derived_spans = project.db.execute(
            "SELECT start_ms, end_ms, metadata FROM source_spans "
            "WHERE artifact_id=? ORDER BY start_ms",
            (derived["id"],),
        ).fetchall()
        assert [(row["start_ms"], row["end_ms"]) for row in derived_spans] == [
            (0, 1000),
            (1000, 3000),
        ]

        project.apply_edits(
            [
                {
                    "row_id": source.row_id,
                    "column_id": transcript_column_id,
                    "value": "edited transcript",
                }
            ]
        )
        with pytest.raises(TimelineError) as exc:
            revalidate_transcript(project, resolved)
        assert exc.value.code == "stale_input"
    finally:
        project.close()


def test_multiple_live_transcripts_are_all_resolved_in_column_order(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "ambiguous-transcript.frisket")
    try:
        source, _transcript_column_id, _link = _seed_source(project)
        _add_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            artifact_id=source.lease.anchor.artifact_id,
            name="other transcript",
            text="another live run",
            span_values=[(0, 1000, "another live run")],
        )

        resolved, warnings = resolve_compatible_transcripts(project, source)
        assert warnings == ()
        assert [item.transcript_column_name for item in resolved] == [
            "transcript",
            "other transcript",
        ]
    finally:
        project.close()


def test_root_compatible_sibling_transcript_is_rebased_to_source_clock(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "sibling-transcript.frisket")
    try:
        source, _transcript_column_id, _link = _seed_source(project)
        sibling_blob = project.add_blob(
            b"sibling clip",
            filename="sibling.mp4",
            mime="video/mp4",
        )
        sibling = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=sibling_blob,
            duration_ms=2_000,
        )
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=int(sibling["id"]),
            source_artifact_id=source.lease.anchor.artifact_id,
            source_start_ms=4_000,
            source_end_ms=6_000,
            precision="exact",
        )
        _add_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            artifact_id=int(sibling["id"]),
            name="sibling transcript",
            text="Sibling words.",
            span_values=[(0, 2_000, "Sibling words.")],
        )

        resolved, warnings = resolve_compatible_transcripts(project, source)
        assert warnings == ()
        sibling_transcript = next(
            item
            for item in resolved
            if item.transcript_column_name == "sibling transcript"
        )
        assert sibling_transcript.source_to_transcript_offset_ms == -4_000

        projection = project_transcript(
            sibling_transcript,
            source_start_ms=3_000,
            source_end_ms=5_000,
        )
        assert [(item.start_ms, item.end_ms) for item in projection.segments] == [
            (1_000, 2_000)
        ]
        assert [
            (item.source_start_ms, item.source_end_ms) for item in projection.segments
        ] == [(0, 1_000)]
    finally:
        project.close()


def test_nonempty_typed_transcript_with_invalid_evidence_produces_plain_warning(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transcript-warning.frisket")
    try:
        source, _transcript_column_id, _link = _seed_source(project)
        _add_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            artifact_id=source.lease.anchor.artifact_id,
            name="legacy transcript",
            text="Not V1 evidence.",
            span_values=[(0, 1_000, "Not V1 evidence.")],
            marked=False,
        )

        resolved, warnings = resolve_compatible_transcripts(project, source)
        assert [item.transcript_column_name for item in resolved] == ["transcript"]
        assert warnings == (
            'Transcript "legacy transcript" was not inherited because its current '
            "timestamp evidence is missing, stale, or ambiguous.",
        )
    finally:
        project.close()


def test_legacy_or_edited_cells_do_not_gain_timestamp_semantics(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "timestamped-transcript-cutover.frisket")
    try:
        source, transcript_column_id, _link = _seed_source(project)
        legacy_column_id, _legacy_link = _add_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            artifact_id=source.lease.anchor.artifact_id,
            name="retyped legacy transcript",
            text="old evidence is not a semantic transcript",
            span_values=[(0, 1000, "old evidence is not a semantic transcript")],
            marked=False,
        )
        assert (
            resolve_timestamped_transcript(
                project,
                sheet_id=source.sheet_id,
                row_id=source.row_id,
                column_id=legacy_column_id,
            )
            is None
        )

        project.apply_edits(
            [
                {
                    "row_id": source.row_id,
                    "column_id": transcript_column_id,
                    "value": "edited even if the words happen to match",
                }
            ]
        )
        assert (
            resolve_timestamped_transcript(
                project,
                sheet_id=source.sheet_id,
                row_id=source.row_id,
                column_id=transcript_column_id,
            )
            is None
        )
    finally:
        project.close()


@pytest.mark.parametrize("invalid_run_id", [None, 0, -1, ""])
def test_marked_transcript_evidence_requires_positive_run_identity(
    tmp_path: Path,
    invalid_run_id: object,
) -> None:
    project = Project.create(tmp_path / "timestamped-transcript-run-identity.frisket")
    try:
        source, transcript_column_id, link = _seed_source(project)
        # Model a host/hand-created link that carries the clean-cutover marker
        # and valid spans/value identity but omits a real producing run.
        project.db.execute(
            "UPDATE evidence_links SET run_id=? WHERE id=?",
            (invalid_run_id, link["id"]),
        )
        project.db.commit()

        assert (
            resolve_timestamped_transcript(
                project,
                sheet_id=source.sheet_id,
                row_id=source.row_id,
                column_id=transcript_column_id,
            )
            is None
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    "words",
    [
        {"word": "not-a-list", "start_ms": 1000, "end_ms": 1200},
        [{"word": "", "start_ms": 1000, "end_ms": 1200}],
        [{"word": "outside", "start_ms": 500, "end_ms": 1200}],
        [{"word": "bool", "start_ms": True, "end_ms": 1200}],
        [{"word": "reversed", "start_ms": 1500, "end_ms": 1400}],
    ],
)
def test_timestamped_transcript_rejects_malformed_or_out_of_segment_words(
    tmp_path: Path,
    words: object,
) -> None:
    project = Project.create(tmp_path / "invalid-transcript-words.frisket")
    try:
        source, transcript_column_id, link = _seed_source(project)
        span_id = project.db.execute(
            "SELECT span_id FROM evidence_link_spans WHERE link_id=? ORDER BY rank LIMIT 1",
            (link["id"],),
        ).fetchone()[0]
        project.db.execute(
            "UPDATE source_spans SET selector_json=? WHERE id=?",
            (json.dumps({"segment_index": 0, "words": words}), span_id),
        )
        project.db.commit()

        assert (
            resolve_timestamped_transcript(
                project,
                sheet_id=source.sheet_id,
                row_id=source.row_id,
                column_id=transcript_column_id,
            )
            is None
        )
    finally:
        project.close()


def test_transcript_revalidation_does_not_treat_a_second_column_as_ambiguity(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transcript-ambiguity-race.frisket")
    try:
        source, _transcript_column_id, _link = _seed_source(project)
        resolved_items, warnings = resolve_compatible_transcripts(project, source)
        assert warnings == ()
        assert len(resolved_items) == 1
        resolved = resolved_items[0]

        _add_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            artifact_id=source.lease.anchor.artifact_id,
            name="other transcript",
            text="another live run",
            span_values=[(0, 1000, "another live run")],
        )

        revalidate_transcript(project, resolved)
    finally:
        project.close()


def test_hidden_transcript_column_is_not_inheritable(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "hidden-transcript.frisket")
    try:
        source, transcript_column_id, _link = _seed_source(project)
        resolved_items, warnings = resolve_compatible_transcripts(project, source)
        assert warnings == ()
        assert len(resolved_items) == 1
        resolved = resolved_items[0]

        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (transcript_column_id,)
        )
        project.db.commit()

        assert resolve_compatible_transcripts(project, source) == ((), ())
        with pytest.raises(TimelineError) as exc:
            revalidate_transcript(project, resolved)
        assert exc.value.code == "stale_input"
    finally:
        project.close()

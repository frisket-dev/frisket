from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.engine.executor.temporal_materialization import normalize_selection
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    ensure_artifact_timeline,
    ensure_media_timeline,
    ensure_transcript_timeline,
    map_range_to_root,
    media_timeline_fingerprint,
    ordered_segments_hash,
    resolve_artifact_timeline,
    resolve_timeline,
    timeline_path_to_root,
    validate_timeline_anchor,
    write_rate1_timeline_segment,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.media_blobs import owned_media_metadata_document


def _artifact(
    project: Project,
    name: str,
    duration_ms: int,
    *,
    source_sheet_id: int | None = None,
    source_row_id: int | None = None,
    source_column_id: int | None = None,
) -> dict:
    blob_hash = project.add_blob(
        f"bytes:{name}".encode(),
        filename=f"{name}.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": duration_ms / 1000}
        ),
    )
    return record_source_artifact(
        project,
        artifact_kind="av",
        media_type="video/mp4",
        blob_hash=blob_hash,
        filename=f"{name}.mp4",
        duration_ms=duration_ms,
        source_sheet_id=source_sheet_id,
        source_row_id=source_row_id,
        source_column_id=source_column_id,
    )


def _assert_code(exc: pytest.ExceptionInfo[TimelineError], code: str) -> None:
    assert exc.value.code == code


@pytest.mark.parametrize("helper", ["transcript", "rate1"])
@pytest.mark.parametrize("caller_owned", [False, True])
@pytest.mark.parametrize(
    "interruption", [asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
def test_timeline_interruption_preserves_transaction_ownership(
    tmp_path: Path, helper: str, caller_owned: bool, interruption: type[BaseException]
) -> None:
    project = Project.create(tmp_path / "timeline-interruption.frisket")
    try:
        if helper == "transcript":
            artifact = record_source_artifact(
                project, artifact_kind="text", media_type="text/plain"
            )
            perform = ensure_transcript_timeline
            kwargs = {
                "artifact_id": artifact["id"],
                "duration_ms": 1000,
                "segments": [{"start_ms": 0, "end_ms": 1000, "text": "one"}],
            }
            prefix = "UPDATE source_artifacts"
        else:
            source = _artifact(project, "source", 10_000)
            derived = _artifact(project, "derived", 1000)
            perform = write_rate1_timeline_segment
            kwargs = {
                "derived_artifact_id": derived["id"],
                "source_artifact_id": source["id"],
                "source_start_ms": 1000,
                "source_end_ms": 2000,
                "precision": "exact",
            }
            prefix = "INSERT INTO artifact_timeline_segments"

        assert not project.db.in_transaction
        before = tuple(project.db.iterdump())
        if caller_owned:
            project.db.execute("BEGIN IMMEDIATE")
        failure = interruption("interrupted after real SQL write")
        writes = []

        class InterruptAfterWrite:
            def __getattr__(self, name):
                return getattr(project.db, name)

            def execute(self, sql, *args):
                result = project.db.execute(sql, *args)
                if sql.startswith(prefix):
                    writes.append(True)
                    assert project.db.in_transaction
                    assert tuple(project.db.iterdump()) != before
                    raise failure
                return result

        with pytest.raises(interruption) as caught:
            perform(SimpleNamespace(db=InterruptAfterWrite()), **kwargs)
        assert caught.value is failure
        assert writes == [True]
        assert project.db.in_transaction is caller_owned
        if caller_owned:
            # The helper neither commits nor rolls back its caller's work.
            assert tuple(project.db.iterdump()) != before
            project.db.rollback()
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


def test_timeline_schema_is_created_on_a_fresh_bundle(tmp_path: Path) -> None:
    """``Project.create`` is the only thing that puts this schema on a bundle:
    there is no open-time replay to repair a missing table any more, and a
    bundle that does not match ``schema.py`` is refused rather than fixed."""
    bundle = tmp_path / "timeline-schema.frisket"
    project = Project.create(bundle)
    try:
        table_sql = project.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='artifact_timeline_segments'"
        ).fetchone()["sql"]
        assert "UNIQUE (derived_artifact_id, ordinal)" in table_sql
        assert "CHECK (derived_artifact_id <> source_artifact_id)" in table_sql
        indexes = {
            row["name"]
            for row in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_artifact_timeline_source_time" in indexes
    finally:
        project.close()


def test_media_fingerprint_is_canonical_and_excludes_mutable_metadata(
    tmp_path: Path,
) -> None:
    assert (
        media_timeline_fingerprint(blob_hash="0123456789abcdef", duration_ms=60_000)
        == "sha256:2f0e1d946239092b57fac9363eadd21a1720a8b82e4a06e02d03333e26bd635f"
    )
    assert (
        ordered_segments_hash([{"start_ms": 0, "end_ms": 1000, "text": "hello"}])
        == "sha256:ee7815b364832c36e6a18b8a2b5d963f9751ce8a935336156feb2ff377e0dfd5"
    )

    project = Project.create(tmp_path / "timeline-fingerprint.frisket")
    try:
        artifact = _artifact(project, "source", 60_000)
        first = ensure_artifact_timeline(project, artifact["id"])
        project.db.execute(
            "UPDATE source_artifacts SET title=?, filename=?, source_url=? WHERE id=?",
            ("New title", "renamed.mp4", "https://example.test/new", artifact["id"]),
        )
        project.db.commit()
        assert resolve_artifact_timeline(project, artifact["id"]).fingerprint == (
            first.fingerprint
        )

        project.db.execute(
            "UPDATE source_artifacts SET duration_ms=60001 WHERE id=?",
            (artifact["id"],),
        )
        project.db.commit()
        changed = resolve_artifact_timeline(project, artifact["id"])
        assert changed.fingerprint != first.fingerprint
        with pytest.raises(TimelineError) as exc:
            validate_timeline_anchor(project, first.wire_value())
        _assert_code(exc, "timeline_stale")
    finally:
        project.close()


def test_resolve_timeline_adopts_legacy_artifact_and_reuses_identity(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-resolve.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "video", "video")
        blob_hash = project.add_blob(
            b"video bytes",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 12.5}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "video": {
                        "blob": blob_hash,
                        "mime": "video/mp4",
                        "filename": "source.mp4",
                    }
                }
            ],
            {"video": column_id},
        )[0]
        legacy = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=blob_hash,
            filename="source.mp4",
            duration_ms=12_500,
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=column_id,
            metadata={"producer": "pre-timeline-transcribe"},
        )

        first = resolve_timeline(
            project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
        )
        second = resolve_timeline(
            project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
        )
        assert first.anchor.artifact_id == legacy["id"] == second.anchor.artifact_id
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 1
        )
        assert first.anchor.duration_ms == 12_500
        assert first.anchor.wire_value() == {
            "artifact_stable_id": legacy["stable_id"],
            "fingerprint": first.anchor.fingerprint,
            "duration_ms": 12_500,
        }
        metadata = json.loads(
            project.db.execute(
                "SELECT metadata FROM source_artifacts WHERE id=?", (legacy["id"],)
            ).fetchone()["metadata"]
        )
        assert metadata == {"producer": "pre-timeline-transcribe"}
        assert first.current_value_ref["kind"] == "source_cell"
        assert first.source_value_hash == second.source_value_hash

        assert validate_timeline_anchor(project, first.anchor.wire_value()) == (
            first.anchor
        )
        with pytest.raises(TimelineError) as exc:
            validate_timeline_anchor(
                project,
                {**first.anchor.wire_value(), "duration_ms": 12_501},
            )
        _assert_code(exc, "timeline_stale")
        with pytest.raises(TimelineError) as exc:
            validate_timeline_anchor(
                project,
                {
                    **first.anchor.wire_value(),
                    "fingerprint": "sha256:" + "0" * 64,
                },
            )
        _assert_code(exc, "timeline_stale")
    finally:
        project.close()


@pytest.mark.parametrize(
    (
        "column_type",
        "blob_mime",
        "cell_mime",
        "metadata_kind",
        "expected_kind",
    ),
    [
        ("audio", "video/mp4", "video/mp4", "video", "audio"),
        ("video", "audio/mpeg", "audio/mpeg", "audio", "video"),
        ("file", "audio/mpeg", "audio/mpeg", "file", "audio"),
        (
            "file",
            "application/octet-stream",
            "application/octet-stream",
            "video",
            "video",
        ),
        ("file", "audio/mpeg", "application/octet-stream", "file", "audio"),
    ],
)
def test_resolve_timeline_carries_unambiguous_media_kind(
    tmp_path: Path,
    column_type: str,
    blob_mime: str,
    cell_mime: str,
    metadata_kind: str,
    expected_kind: str,
) -> None:
    project = Project.create(
        tmp_path / f"timeline-media-kind-{column_type}-{expected_kind}.frisket"
    )
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "media", column_type)
        blob_hash = project.add_blob(
            b"media bytes",
            filename="source.bin",
            mime=blob_mime,
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 1.0, "kind": metadata_kind}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "media": {
                        "blob": blob_hash,
                        "mime": cell_mime,
                        "kind": metadata_kind,
                    }
                }
            ],
            {"media": column_id},
        )[0]

        lease = resolve_timeline(
            project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
        )

        assert lease.media_kind == expected_kind
    finally:
        project.close()


def test_resolve_timeline_rejects_duration_only_opaque_file(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "timeline-opaque-file.frisket")
    try:
        sheet_id = project.add_sheet("Files")
        column_id = project.add_column(sheet_id, "file", "file")
        blob_hash = project.add_blob(
            b"opaque bytes",
            filename="source.bin",
            mime="application/octet-stream",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 1.0, "kind": "file"}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "file": {
                        "blob": blob_hash,
                        "mime": "application/octet-stream",
                        "duration_ms": 1_000,
                    }
                }
            ],
            {"file": column_id},
        )[0]

        with pytest.raises(TimelineError) as exc:
            resolve_timeline(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                duration_ms=1_000,
            )
        _assert_code(exc, "timeline_not_found")
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_resolve_timeline_rejects_conflicting_file_media_metadata(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-conflicting-kind.frisket")
    try:
        sheet_id = project.add_sheet("Files")
        column_id = project.add_column(sheet_id, "file", "file")
        blob_hash = project.add_blob(
            b"media bytes",
            filename="source.bin",
            mime="application/octet-stream",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 1.0, "kind": "video"}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "file": {
                        "blob": blob_hash,
                        "mime": "audio/mpeg",
                    }
                }
            ],
            {"file": column_id},
        )[0]

        with pytest.raises(TimelineError) as exc:
            resolve_timeline(
                project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
            )
        _assert_code(exc, "timeline_stale")
    finally:
        project.close()


def test_concurrent_first_media_ensure_is_atomic_and_survives_reopen(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "timeline-concurrent.frisket"
    seed = Project.create(bundle)
    try:
        blob_hash = seed.add_blob(
            b"one immutable timeline",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 12.5}
            ),
        )
    finally:
        seed.close()

    first = Project(bundle)
    second = Project(bundle)
    first_anchor = None
    second_anchor = None
    try:
        # The first ensure participates in its caller's transaction.  A second
        # connection cannot observe or create around that uncommitted revision.
        first.db.execute("BEGIN IMMEDIATE")
        first_anchor = ensure_media_timeline(
            first,
            blob_hash=blob_hash,
            duration_ms=12_500,
            media_type="video/mp4",
            filename="source.mp4",
        )
        assert first.db.in_transaction
        observer = sqlite3.connect(bundle / "project.db")
        try:
            assert (
                observer.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
                == 0
            )
        finally:
            observer.close()

        attempted_write_lock = threading.Event()

        def competing_ensure():
            connection = second.db

            def trace(statement: str) -> None:
                if statement.strip().upper() == "BEGIN IMMEDIATE":
                    attempted_write_lock.set()

            connection.set_trace_callback(trace)
            try:
                return ensure_media_timeline(
                    second,
                    blob_hash=blob_hash,
                    duration_ms=12_500,
                    media_type="video/mp4",
                    filename="source.mp4",
                )
            finally:
                connection.set_trace_callback(None)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(competing_ensure)
            try:
                assert attempted_write_lock.wait(timeout=2)
                assert not future.done()
            finally:
                if first.db.in_transaction:
                    first.db.commit()
            second_anchor = future.result(timeout=5)

        assert second_anchor == first_anchor
        assert (
            first.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
        )
    finally:
        if first.db.in_transaction:
            first.db.rollback()
        first.close()
        second.close()

    reopened = Project(bundle)
    try:
        assert first_anchor is not None
        assert second_anchor is not None
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 1
        )
        assert (
            resolve_artifact_timeline(reopened, first_anchor.artifact_id)
            == first_anchor
        )
    finally:
        reopened.close()


def test_media_ensure_rejects_duplicate_stable_id_without_partial_commit(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-stable-id.frisket")
    try:
        first_blob = project.add_blob(b"first", "first.mp4", "video/mp4")
        second_blob = project.add_blob(b"second", "second.mp4", "video/mp4")
        stable_id = "source_artifact:00000000-0000-4000-8000-000000000001"
        claimed = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            stable_id=stable_id,
            blob_hash=first_blob,
            duration_ms=1_000,
        )

        with pytest.raises(TimelineError) as exc:
            ensure_media_timeline(
                project,
                blob_hash=second_blob,
                duration_ms=1_000,
                media_type="video/mp4",
                stable_id=stable_id,
            )
        _assert_code(exc, "timeline_stale")
        assert not project.db.in_transaction
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 1
        )
        claimed_metadata = json.loads(
            project.db.execute(
                "SELECT metadata FROM source_artifacts WHERE id=?", (claimed["id"],)
            ).fetchone()["metadata"]
        )
        assert "timeline" not in claimed_metadata

        created = ensure_media_timeline(
            project,
            blob_hash=second_blob,
            duration_ms=1_000,
            media_type="video/mp4",
        )
        assert created.artifact_id != claimed["id"]
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 2
        )
    finally:
        project.close()


def test_generic_artifact_metadata_merge_cannot_replace_timeline_identity(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.evidence import merge_artifact_metadata

    project = Project.create(tmp_path / "timeline-metadata-guard.frisket")
    try:
        artifact = _artifact(project, "source", 10_000)
        anchor = ensure_artifact_timeline(project, artifact["id"])
        with pytest.raises(ValueError, match="reserved"):
            merge_artifact_metadata(
                project,
                artifact["id"],
                {"timeline": {"fingerprint": "sha256:" + "0" * 64}},
            )
        assert resolve_artifact_timeline(project, artifact["id"]) == anchor

        merged = merge_artifact_metadata(
            project, artifact["id"], {"description": "safe descriptive change"}
        )
        assert merged["metadata"]["description"] == "safe descriptive change"
        assert resolve_artifact_timeline(project, artifact["id"]) == anchor
    finally:
        project.close()


def test_resolve_timeline_requires_known_duration(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "timeline-duration.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "video", "video")
        blob_hash = project.add_blob(b"video", "source.mp4", "video/mp4")
        row_id = project.add_rows(
            sheet_id,
            [{"video": {"blob": blob_hash, "mime": "video/mp4"}}],
            {"video": column_id},
        )[0]
        with pytest.raises(TimelineError) as exc:
            resolve_timeline(
                project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
            )
        _assert_code(exc, "timeline_duration_required")
    finally:
        project.close()


def test_rate1_writer_is_exact_complete_and_single_assignment(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-write.frisket")
    try:
        source = _artifact(project, "source", 120_000)
        derived = _artifact(project, "clip", 30_000)
        segment = write_rate1_timeline_segment(
            project,
            derived_artifact_id=derived["id"],
            source_artifact_id=source["id"],
            source_start_ms=40_000,
            source_end_ms=70_000,
            precision="frame_accurate",
            params={"alignment_error_ms": 0, "profile": "accurate-v1"},
        )
        assert (
            segment.ordinal,
            segment.derived_start_ms,
            segment.derived_end_ms,
            segment.rate_num,
            segment.rate_den,
        ) == (0, 0, 30_000, 1, 1)
        assert segment.source_start_ms == 40_000
        assert segment.source_end_ms == 70_000
        assert segment.params == {
            "alignment_error_ms": 0,
            "profile": "accurate-v1",
        }
        assert resolve_artifact_timeline(project, derived["id"]).kind == "derived"

        with pytest.raises(TimelineError) as exc:
            write_rate1_timeline_segment(
                project,
                derived_artifact_id=derived["id"],
                source_artifact_id=source["id"],
                source_start_ms=40_000,
                source_end_ms=70_000,
                precision="frame_accurate",
            )
        _assert_code(exc, "ambiguous_time_mapping")
    finally:
        project.close()


def test_rate1_writer_rejects_fuzzy_length_out_of_bounds_and_self_links(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-write-reject.frisket")
    try:
        source = _artifact(project, "source", 20_000)
        mismatch = _artifact(project, "mismatch", 10_001)
        with pytest.raises(TimelineError) as exc:
            write_rate1_timeline_segment(
                project,
                derived_artifact_id=mismatch["id"],
                source_artifact_id=source["id"],
                source_start_ms=0,
                source_end_ms=10_000,
                precision="exact",
            )
        _assert_code(exc, "ambiguous_time_mapping")

        outside = _artifact(project, "outside", 10_000)
        with pytest.raises(TimelineError) as exc:
            write_rate1_timeline_segment(
                project,
                derived_artifact_id=outside["id"],
                source_artifact_id=source["id"],
                source_start_ms=15_000,
                source_end_ms=25_000,
                precision="exact",
            )
        _assert_code(exc, "range_out_of_bounds")

        with pytest.raises(TimelineError) as exc:
            write_rate1_timeline_segment(
                project,
                derived_artifact_id=source["id"],
                source_artifact_id=source["id"],
                source_start_ms=0,
                source_end_ms=20_000,
                precision="exact",
            )
        _assert_code(exc, "ambiguous_time_mapping")
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_clip_of_clip_path_composes_exact_integer_offsets(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "timeline-chain.frisket")
    try:
        root = _artifact(project, "root", 120_000)
        clip = _artifact(project, "clip", 60_000)
        excerpt = _artifact(project, "excerpt", 10_000)
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=clip["id"],
            source_artifact_id=root["id"],
            source_start_ms=30_000,
            source_end_ms=90_000,
            precision="frame_accurate",
        )
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=excerpt["id"],
            source_artifact_id=clip["id"],
            source_start_ms=5_000,
            source_end_ms=15_000,
            precision="frame_accurate",
        )

        path = timeline_path_to_root(project, excerpt["id"])
        assert [item.source_artifact_id for item in path] == [clip["id"], root["id"]]
        resolved = map_range_to_root(
            project,
            artifact_id=excerpt["id"],
            start_ms=2_000,
            end_ms=8_000,
        )
        assert resolved.root_artifact_id == root["id"]
        assert (resolved.root_start_ms, resolved.root_end_ms) == (37_000, 43_000)
    finally:
        project.close()


def _equivalent_root_clips(
    project: Project,
) -> tuple[dict, dict, dict, dict]:
    root_blob = project.add_blob(
        b"shared immutable root",
        filename="root.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": 10.0}
        ),
    )
    roots = [
        record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=root_blob,
            filename=f"root-{index}.mp4",
            duration_ms=10_000,
        )
        for index in range(2)
    ]
    clips = [_artifact(project, f"clip-{index}", 4_000) for index in range(2)]
    for root, clip in zip(roots, clips, strict=True):
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=clip["id"],
            source_artifact_id=root["id"],
            source_start_ms=1_000,
            source_end_ms=5_000,
            precision="exact",
        )
    return roots[0], roots[1], clips[0], clips[1]


def test_mapped_range_exposes_equivalent_root_fingerprint_across_artifact_ids(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-equivalent-roots.frisket")
    try:
        first_root, second_root, first_clip, second_clip = _equivalent_root_clips(
            project
        )

        first = map_range_to_root(
            project, artifact_id=first_clip["id"], start_ms=0, end_ms=4_000
        )
        second = map_range_to_root(
            project, artifact_id=second_clip["id"], start_ms=0, end_ms=4_000
        )

        assert first_root["id"] != second_root["id"]
        assert first.root_artifact_id != second.root_artifact_id
        assert first.root_artifact_fingerprint == second.root_artifact_fingerprint
        assert first.root_artifact_fingerprint.startswith("sha256:")
    finally:
        project.close()


@pytest.mark.parametrize(
    ("selection_type", "value_body", "expected_ranges"),
    [
        (
            "timeline_range",
            {
                "schema_version": "frisket.timeline_range.v1",
                "item": {
                    "id": "range-1",
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "metadata": {},
                },
            },
            [(1_000, 2_000)],
        ),
        (
            "timeline_point",
            {
                "schema_version": "frisket.timeline_point.v1",
                "item": {"id": "point-1", "at_ms": 2_000, "metadata": {}},
            },
            [(0, 2_000), (2_000, 4_000)],
        ),
    ],
)
def test_selection_mapping_accepts_equivalent_root_fingerprints(
    tmp_path: Path,
    selection_type: str,
    value_body: dict,
    expected_ranges: list[tuple[int, int]],
) -> None:
    project = Project.create(
        tmp_path / f"timeline-selection-equivalence-{selection_type}.frisket"
    )
    try:
        _, _, typed_clip, target_clip = _equivalent_root_clips(project)
        typed_anchor = resolve_artifact_timeline(project, typed_clip["id"])
        target_anchor = resolve_artifact_timeline(project, target_clip["id"])
        assert typed_anchor.fingerprint != target_anchor.fingerprint
        value = {**value_body, "timeline": typed_anchor.wire_value()}

        ranges, warnings = normalize_selection(
            project,
            value,
            selection_type=selection_type,
            source_anchor=target_anchor,
            purpose="extract" if selection_type == "timeline_range" else "split",
        )

        assert [(item.start_ms, item.end_ms) for item in ranges] == expected_ranges
        assert warnings == []
    finally:
        project.close()


def test_traversal_rejects_cycles_multi_segments_and_non_rate1_corruption(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-corrupt.frisket")
    try:
        first = _artifact(project, "first", 10_000)
        second = _artifact(project, "second", 10_000)
        project.db.executemany(
            "INSERT INTO artifact_timeline_segments ("
            "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
            "source_artifact_id, source_start_ms, source_end_ms, rate_num, rate_den, "
            "precision) VALUES (?, 0, 0, 10000, ?, 0, 10000, 1, 1, 'exact')",
            [(first["id"], second["id"]), (second["id"], first["id"])],
        )
        project.db.commit()
        with pytest.raises(TimelineError) as exc:
            timeline_path_to_root(project, first["id"])
        _assert_code(exc, "ambiguous_time_mapping")

        project.db.execute("DELETE FROM artifact_timeline_segments")
        project.db.executemany(
            "INSERT INTO artifact_timeline_segments ("
            "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
            "source_artifact_id, source_start_ms, source_end_ms, rate_num, rate_den, "
            "precision) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'exact')",
            [
                (first["id"], 0, 0, 5_000, second["id"], 0, 5_000),
                (first["id"], 1, 5_000, 10_000, second["id"], 5_000, 10_000),
            ],
        )
        project.db.commit()
        with pytest.raises(TimelineError) as exc:
            timeline_path_to_root(project, first["id"])
        _assert_code(exc, "ambiguous_time_mapping")

        project.db.execute("DELETE FROM artifact_timeline_segments")
        project.db.execute(
            "INSERT INTO artifact_timeline_segments ("
            "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
            "source_artifact_id, source_start_ms, source_end_ms, rate_num, rate_den, "
            "precision) VALUES (?, 0, 0, 10000, ?, 0, 10000, 2, 1, 'exact')",
            (first["id"], second["id"]),
        )
        project.db.commit()
        with pytest.raises(TimelineError) as exc:
            timeline_path_to_root(project, first["id"])
        _assert_code(exc, "ambiguous_time_mapping")
    finally:
        project.close()


def test_sql_constraints_reject_self_links_and_unknown_precision(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "timeline-sql.frisket")
    try:
        source = _artifact(project, "source", 10_000)
        with pytest.raises(sqlite3.IntegrityError):
            project.db.execute(
                "INSERT INTO artifact_timeline_segments ("
                "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
                "source_artifact_id, source_start_ms, source_end_ms, precision"
                ") VALUES (?, 0, 0, 10000, ?, 0, 10000, 'exact')",
                (source["id"], source["id"]),
            )
        project.db.rollback()

        derived = _artifact(project, "derived", 10_000)
        with pytest.raises(sqlite3.IntegrityError):
            project.db.execute(
                "INSERT INTO artifact_timeline_segments ("
                "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
                "source_artifact_id, source_start_ms, source_end_ms, precision"
                ") VALUES (?, 0, 0, 10000, ?, 0, 10000, 'approximate')",
                (derived["id"], source["id"]),
            )
        project.db.rollback()
    finally:
        project.close()


def test_standalone_transcript_identity_is_finalized_once(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "timeline-transcript.frisket")
    try:
        transcript = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            title="Transcript",
        )
        segments = [
            {"segment_index": 0, "start_ms": 0, "end_ms": 1_000, "text": "One"},
            {
                "segment_index": 1,
                "start_ms": 1_000,
                "end_ms": 2_000,
                "text": "Two",
            },
        ]
        first = ensure_transcript_timeline(
            project,
            artifact_id=transcript["id"],
            duration_ms=2_000,
            segments=segments,
        )
        second = resolve_artifact_timeline(project, transcript["id"])
        assert first == second
        assert first.kind == "transcript"

        with pytest.raises(TimelineError) as exc:
            ensure_transcript_timeline(
                project,
                artifact_id=transcript["id"],
                duration_ms=2_000,
                segments=[*segments, {"segment_index": 2, "start_ms": 2_000}],
            )
        _assert_code(exc, "timeline_stale")

        media = _artifact(project, "synchronized", 2_000)
        with pytest.raises(TimelineError) as exc:
            ensure_transcript_timeline(
                project,
                artifact_id=media["id"],
                duration_ms=2_000,
                segments=segments,
            )
        _assert_code(exc, "timeline_mismatch")
    finally:
        project.close()

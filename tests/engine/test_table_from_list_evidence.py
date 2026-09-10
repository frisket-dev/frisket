from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_row_evidence, resolve_evidence_viewer
from runner_test_helpers import run_action_with_exact_confirmation
from tests.engine.extract_typed_chain_helpers import typed_extract_request


PROJECT_ID = "project-table-from-list-evidence"


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=json.loads(json.dumps(self.reply)),
            tokens_in=97,
            tokens_out=43,
            cost=0.008,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _seed_transcript_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(
        tmp_path / "table-from-list-evidence.frisket", name="Moments Table"
    )
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blob = project.add_blob(
        b"RIFF0000WAVEfmt moments-table",
        filename="episode.wav",
        mime="audio/wav",
        source_url="https://cdn.example/episode.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 12.0, "kind": "audio"}
        ),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(blob, mime="audio/wav", filename="episode.wav"),
            }
        ],
        cols,
    )
    return {"project": project, "sheet_id": sheet_id, "row_ids": row_ids, "blob": blob}


_TRANSCRIPT_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "welcome to the show"},
    {"start": 2.0, "end": 4.0, "text": "today we discuss the election"},
    {"start": 4.0, "end": 6.0, "text": "some say the vote was rigged"},
    {"start": 6.0, "end": 8.0, "text": "others question the integrity of the count"},
    {"start": 8.0, "end": 10.0, "text": "officials deny any wrongdoing"},
    {"start": 10.0, "end": 12.0, "text": "thanks for listening"},
]


def _fake_faster_whisper(blob_hash: str):
    async def fake(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        assert Path(path).name == blob_hash
        return {
            "text": " ".join(seg["text"] for seg in _TRANSCRIPT_SEGMENTS),
            "segments": _TRANSCRIPT_SEGMENTS,
            "language": "en",
            "duration": _TRANSCRIPT_SEGMENTS[-1]["end"],
        }

    return fake


def _transcribe_action(*, sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "faster_whisper",
        },
        "idempotency_key": "media_transcribe@sha256:table-from-list",
    }


def _extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["transcript"],
        instruction="List every moment questioning election integrity.",
        fields=[
            {
                "name": "moments",
                "type": "list",
                "items": {"type": "string"},
                "description": "Every moment questioning election integrity.",
            }
        ],
        grounding={
            "enabled": True,
            "allowed_methods": ["exact_quote"],
        },
        source_document_columns=["transcript"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:table-from-list",
    )


def _extract_reply() -> dict[str, Any]:
    return {
        "moments": {
            "value": [
                "some say the vote was rigged",
                "others question the integrity of the count",
                "officials deny any wrongdoing",
            ],
            "evidence": [
                [{"segment_indices": [2]}],
                [{"segment_indices": [3]}],
                [{"segment_indices": [4]}],
            ],
            "warnings": [],
        }
    }


def _derive_action(*, sheet_id: int, column_id: int, run_id: int) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Moments",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": "moments",
                "schema": "moments_list",
            },
            "item_schema": {"type": "string"},
            "columns": [{"name": "moment", "path": "$", "type": "text"}],
        },
        "idempotency_key": "derive_table_from_list@sha256:moments",
    }


def _run_to_extracted_moments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        _fake_faster_whisper(seeded["blob"]),
    )
    transcribed = run_action_with_exact_confirmation(
        project,
        _transcribe_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert transcribed.status == "completed", transcribed.errors

    router, adapter = _stub_router(_extract_reply())
    extracted = run_action_with_exact_confirmation(
        project,
        _extract_action(seeded["sheet_id"]),
        project_id=PROJECT_ID,
        router=router,
    )
    assert extracted.status == "completed", extracted.errors
    assert len(adapter.requests) == 1
    seeded["extracted"] = extracted
    seeded["moments_column_id"] = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='moments'",
            (seeded["sheet_id"],),
        ).fetchone()["id"]
    )
    return seeded


def test_moments_table_row_citation_opens_at_its_item_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    try:
        derived = run_action_with_exact_confirmation(
            project,
            _derive_action(
                sheet_id=seeded["sheet_id"],
                column_id=seeded["moments_column_id"],
                run_id=seeded["extracted"].run_id,
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        moment_column_id = next(
            output.column_id
            for output in derived.outputs
            if output.kind == "column" and output.name == "moment"
        )
        rows = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert len(rows) == 3
        moments = project.get_values(child_sheet_id, moment_column_id)

        expected_by_moment = {
            "some say the vote was rigged": (4000, 6000),
            "others question the integrity of the count": (6000, 8000),
            "officials deny any wrongdoing": (8000, 10000),
        }

        seen = set()
        for row in rows:
            row_id = int(row["id"])
            moment_text = moments[row_id]
            assert moment_text in expected_by_moment

            # RED CHECK: each derived row's OWN citation opens the viewer at
            # ITS source item's span, not an orphan and not another row's.
            evidence = list_row_evidence(
                project, sheet_id=child_sheet_id, row_id=row_id, project_id=PROJECT_ID
            )
            assert len(evidence["links"]) == 1
            viewer = resolve_evidence_viewer(
                project, evidence["links"][0]["stable_id"], project_id=PROJECT_ID
            )
            spans = viewer["artifacts"][0]["spans"]
            assert len(spans) == 1
            span = spans[0]
            assert span["span_kind"] == "temporal"
            assert span["quote"] == moment_text
            expected_start, expected_end = expected_by_moment[moment_text]
            assert span["selector"]["start_ms"] == expected_start
            assert span["selector"]["end_ms"] == expected_end
            seen.add(moment_text)

        assert seen == set(expected_by_moment)
    finally:
        project.close()


def test_derive_table_from_list_replay_does_not_duplicate_row_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    try:
        action = _derive_action(
            sheet_id=seeded["sheet_id"],
            column_id=seeded["moments_column_id"],
            run_id=seeded["extracted"].run_id,
        )
        first = run_action_with_exact_confirmation(
            project, action, project_id=PROJECT_ID
        )
        assert first.status == "completed", first.errors
        before = int(
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
        )

        replay = run_action_with_exact_confirmation(
            project, action, project_id=PROJECT_ID
        )
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        after = int(
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
        )
        assert after == before
    finally:
        project.close()


@pytest.mark.parametrize("source_kind", ["named_result", "column"])
def test_refresh_preserves_renamed_item_grounding_and_retires_previous_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor import sheet_refresh_action as sheet_refresh

    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    try:
        action = _derive_action(
            sheet_id=seeded["sheet_id"],
            column_id=seeded["moments_column_id"],
            run_id=seeded["extracted"].run_id,
        )
        logical_name = "moment"
        if source_kind == "column":
            action["params"] = {
                "source": {
                    "kind": "column",
                    "sheet_id": seeded["sheet_id"],
                    "column_id": seeded["moments_column_id"],
                }
            }
            logical_name = "value"
        action["output_names"] = {logical_name: "Quotation"}
        created = run_action_spec(project, action, project_id=PROJECT_ID)
        assert created.status == "completed", created.errors
        child_id = next(
            output.sheet_id for output in created.outputs if output.kind == "sheet"
        )
        columns_before = [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, name, type, ai_generated, hidden, format FROM columns WHERE sheet_id=? ORDER BY position",
                (child_id,),
            )
        ]
        assert [column[1] for column in columns_before] == ["Quotation"]
        column_id = columns_before[0][0]
        quotes = _extract_reply()["moments"]["value"]

        def active_links():
            values = project.get_values(child_id, column_id)
            assert list(values.values()) == quotes
            links = []
            for index, (row_id, quote) in enumerate(values.items()):
                evidence = list_row_evidence(project, sheet_id=child_id, row_id=row_id)
                assert len(evidence["links"]) == 1
                link = evidence["links"][0]
                viewer = resolve_evidence_viewer(
                    project, link["stable_id"], project_id=PROJECT_ID
                )
                span = viewer["artifacts"][0]["spans"][0]
                assert span["quote"] == quote
                assert span["selector"]["start_ms"] == 4000 + index * 2000
                assert span["selector"]["end_ms"] == 6000 + index * 2000
                links.append(link["stable_id"])
            return links

        original_links = active_links()
        # The original derive's applicable undo/redo keeps its row grounding.
        assert project.undo() == created.op_ids[0]
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (child_id,)
            ).fetchone()[0]
            == 1
        )
        assert project.redo() == created.op_ids[0]
        assert active_links() == original_links

        def refresh(key):
            return run_action_spec(
                project,
                {
                    "action_id": "sheet.refresh",
                    "scope": {"kind": "project"},
                    "params": {"sheet_id": child_id},
                    "idempotency_key": key,
                },
                project_id=PROJECT_ID,
            )

        for index in range(2):
            previous_links = active_links()
            refreshed = refresh(f"grounded-refresh-{index}")
            assert refreshed.status == "completed", refreshed.errors
            fresh_links = active_links()
            assert set(fresh_links).isdisjoint(previous_links)
            for link_id in previous_links:
                stale = project.db.execute(
                    "SELECT status, stale_reason FROM evidence_links WHERE stable_id=?",
                    (link_id,),
                ).fetchone()
                assert tuple(stale) == ("stale", "sheet_refreshed")
            assert [
                tuple(row)
                for row in project.db.execute(
                    "SELECT id, name, type, ai_generated, hidden, format FROM columns WHERE sheet_id=? ORDER BY position",
                    (child_id,),
                )
            ] == columns_before
            replay = refresh(f"grounded-refresh-{index}")
            assert replay.status == "completed", replay.errors
            assert replay.receipt_id == refreshed.receipt_id
            assert active_links() == fresh_links

        # Evidence-copy failure rolls back both row replacement and the stale sweep.
        table_keys = {
            "sheets": "id",
            "rows": "id",
            "cells": "row_id, column_id",
            "ops": "id",
            "receipts": "id",
            "evidence_links": "id",
            "evidence_link_spans": "link_id, span_id",
        }

        def snapshot(table):
            return [
                tuple(row)
                for row in project.db.execute(
                    f"SELECT * FROM {table} ORDER BY {table_keys[table]}"
                )
            ]

        before = {table: snapshot(table) for table in table_keys}
        real_copy = sheet_refresh.propagate_list_item_evidence

        def fail_after_copy(*args, **kwargs):
            real_copy(*args, **kwargs)
            raise RuntimeError("late evidence failure")

        monkeypatch.setattr(
            sheet_refresh, "propagate_list_item_evidence", fail_after_copy
        )
        refused = refresh("grounded-refresh-failed")
        assert refused.status == "failed"
        assert refused.errors[0].code == "project_write_failed"
        for table, old_rows in before.items():
            assert snapshot(table) == old_rows
        assert active_links() == fresh_links
    finally:
        project.close()


def test_ungrounded_list_source_propagates_no_row_evidence(
    tmp_path: Path,
) -> None:
    """A list source with no per-item evidence links (e.g. a plain JSON
    column, no extract-list-item-grounding-v1 write) is a silent no-op -- the
    derived rows materialize normally, just with nothing to cite."""

    project = Project.create(
        tmp_path / "table-from-list-ungrounded.frisket", name="Ungrounded"
    )
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "items", type="json")
        row_ids = project.add_rows(
            sheet_id, [{"items": ["a", "b"]}], {"items": column_id}
        )
        row_id = row_ids[0]
        action = {
            "action_id": "derive.table_from_list",
            "scope": {"kind": "project"},
            "sheet_name": "Items",
            "params": {
                "source": {
                    "kind": "column",
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                },
            },
            "idempotency_key": "derive_table_from_list@sha256:ungrounded",
        }
        result = run_action_with_exact_confirmation(
            project, action, project_id=PROJECT_ID
        )
        assert result.status == "completed", result.errors
        child_sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        rows = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (child_sheet_id,)
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            evidence = list_row_evidence(
                project, sheet_id=child_sheet_id, row_id=int(row["id"])
            )
            assert evidence["links"] == []
        del row_id
    finally:
        project.close()


@pytest.mark.parametrize("source_kind", ["column", "named_result"])
@pytest.mark.parametrize("refresh_existing", [False, True])
def test_manual_list_edit_cannot_borrow_an_unrelated_item_citation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    refresh_existing: bool,
) -> None:
    from frisket.engine.executor import run_action_spec

    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project = seeded["project"]
    try:
        original = _extract_reply()["moments"]["value"]
        replacement = "A completely different unsupported claim"

        def replace_first_item():
            project.apply_edits(
                [
                    {
                        "row_id": seeded["row_ids"][0],
                        "column_id": seeded["moments_column_id"],
                        "value": [replacement, *original[1:]],
                    }
                ]
            )

        if not refresh_existing:
            replace_first_item()
        request = _derive_action(
            sheet_id=seeded["sheet_id"],
            column_id=seeded["moments_column_id"],
            run_id=seeded["extracted"].run_id,
        )
        if source_kind == "column":
            request["params"] = {
                "source": {
                    "kind": "column",
                    "sheet_id": seeded["sheet_id"],
                    "column_id": seeded["moments_column_id"],
                }
            }
        created = run_action_spec(project, request, project_id=PROJECT_ID)
        assert created.status == "completed", created.errors
        sheet_id = next(
            output.sheet_id for output in created.outputs if output.kind == "sheet"
        )
        column_id = next(
            output.column_id for output in created.outputs if output.kind == "column"
        )
        if refresh_existing:
            replace_first_item()
            refreshed = run_action_spec(
                project,
                {
                    "action_id": "sheet.refresh",
                    "scope": {"kind": "project"},
                    "params": {"sheet_id": sheet_id},
                    "idempotency_key": "edited-item-refresh",
                },
                project_id=PROJECT_ID,
            )
            assert refreshed.status == "completed", refreshed.errors
        values = project.get_values(sheet_id, column_id)
        assert list(values.values()) == (
            [replacement, *original[1:]] if source_kind == "column" else original
        )
        for index, (row_id, quote) in enumerate(values.items()):
            evidence = list_row_evidence(project, sheet_id=sheet_id, row_id=row_id)
            if source_kind == "column" and index == 0:
                assert evidence["links"] == []
                continue
            assert len(evidence["links"]) == 1
            viewer = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
            assert viewer["artifacts"][0]["spans"][0]["quote"] == quote
    finally:
        project.close()


@pytest.mark.parametrize("source_kind", ["column", "named_result"])
def test_item_grounding_matches_the_admitted_source_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    from frisket.engine.executor import run_action_spec

    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project = seeded["project"]
    try:
        source_run = seeded["extracted"].run_id
        links = project.db.execute(
            "SELECT id, metadata FROM evidence_links WHERE row_id=? AND column_id=? AND status='active'",
            (seeded["row_ids"][0], seeded["moments_column_id"]),
        ).fetchall()
        first_link = next(
            link
            for link in links
            if json.loads(link["metadata"]).get("item_index") == 0
        )
        # Same value and item index from another run cannot support this source.
        project.db.execute(
            "UPDATE evidence_links SET run_id=? WHERE id=?",
            (source_run + 1000, first_link["id"]),
        )
        project.db.commit()
        request = _derive_action(
            sheet_id=seeded["sheet_id"],
            column_id=seeded["moments_column_id"],
            run_id=source_run,
        )
        if source_kind == "column":
            request["params"] = {
                "source": {
                    "kind": "column",
                    "sheet_id": seeded["sheet_id"],
                    "column_id": seeded["moments_column_id"],
                }
            }
        created = run_action_spec(project, request, project_id=PROJECT_ID)
        assert created.status == "completed", created.errors
        sheet_id = next(
            output.sheet_id for output in created.outputs if output.kind == "sheet"
        )
        for index, row_id in enumerate(project.visible_row_ids(sheet_id)):
            evidence = list_row_evidence(project, sheet_id=sheet_id, row_id=row_id)
            assert len(evidence["links"]) == (0 if index == 0 else 1)
    finally:
        project.close()


def test_item_grounding_publishes_the_link_spans_captured_during_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.list_table_read import AdmittedListTableReader

    seeded = _run_to_extracted_moments(tmp_path, monkeypatch)
    project = seeded["project"]
    try:
        read = AdmittedListTableReader.read

        def read_then_change_link(reader, source, *, item_schema=None):
            items = read(reader, source, item_schema=item_schema)
            captured = [
                reader.item_associations[item.source]["evidence_link"] for item in items
            ]
            project.db.execute(
                "UPDATE evidence_link_spans SET span_id=? WHERE link_id=?",
                (captured[1]["spans"][0]["span_id"], captured[0]["id"]),
            )
            project.db.commit()
            return items

        monkeypatch.setattr(AdmittedListTableReader, "read", read_then_change_link)
        created = run_action_spec(
            project,
            _derive_action(
                sheet_id=seeded["sheet_id"],
                column_id=seeded["moments_column_id"],
                run_id=seeded["extracted"].run_id,
            ),
            project_id=PROJECT_ID,
        )
        assert created.status == "completed", created.errors
        sheet_id = next(
            output.sheet_id for output in created.outputs if output.kind == "sheet"
        )
        for row_id, quote in zip(
            project.visible_row_ids(sheet_id),
            _extract_reply()["moments"]["value"],
            strict=True,
        ):
            evidence = list_row_evidence(project, sheet_id=sheet_id, row_id=row_id)
            assert len(evidence["links"]) == 1
            viewer = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
            assert viewer["artifacts"][0]["spans"][0]["quote"] == quote
    finally:
        project.close()

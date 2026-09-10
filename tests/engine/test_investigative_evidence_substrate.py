from __future__ import annotations

import json
import uuid
from pathlib import Path

from frisket.engine.store.evidence import (
    list_cell_evidence,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    resolve_evidence_viewer,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _table_names(project: Project) -> set[str]:
    return {
        str(row["name"])
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _assert_prefixed_uuid(value: str, prefix: str) -> None:
    assert value.startswith(prefix)
    uuid.UUID(value.removeprefix(prefix))


def _seed_generated_cell(project: Project) -> tuple[int, int, int, int, int, dict]:
    sheet_id = project.add_sheet("Documents")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    output_column_id = project.add_column(
        sheet_id, "Contract value", "text", ai_generated=True
    )
    row_id = project.add_rows(
        sheet_id,
        [{"Source": "contract.pdf"}],
        {"Source": source_column_id},
    )[0]
    op_id = project.append_op(
        "map.extract",
        {
            "schema_version": "frisket.action.v2",
            "kind": "map.extract",
            "params": {"output_column": "Contract value"},
        },
        label="extract contract value",
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.extract",
        model="provider/model",
        params={"output_column": "Contract value"},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": "$1,250,000",
                "confidence": 0.92,
            }
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, output_column_id, run_id)
    _values, refs = project.get_values_with_refs(
        sheet_id, output_column_id, row_ids=[row_id]
    )
    return sheet_id, row_id, output_column_id, op_id, run_id, refs[row_id]


def test_investigative_evidence_substrate_contract(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "evidence.frisket", name="Evidence")
    try:
        sheet_id, row_id, column_id, op_id, run_id, current_ref = _seed_generated_cell(
            project
        )
        pdf_hash = project.add_blob(
            b"%PDF-1.4 public contract", "contract.pdf", "application/pdf"
        )
        page_image_hash = project.add_blob(
            b"fake png bytes", "contract-p3.png", "image/png"
        )
        html_hash = project.add_blob(
            b"<html><body><p id='award'>Award value was $1,250,000.</p></body></html>",
            "minutes.html",
            "text/html",
            source_url="https://example.test/minutes",
        )
        audio_hash = project.add_blob(b"audio bytes", "hearing.mp3", "audio/mpeg")
        video_hash = project.add_blob(b"video bytes", "hearing.mp4", "video/mp4")
        unused_hash = project.add_blob(
            b"unused temporary preview", "unused.txt", "text/plain"
        )

        pdf_artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            blob_hash=pdf_hash,
            title="Contract",
            filename="contract.pdf",
            page_count=9,
            metadata={
                "page_images": {
                    "3": {
                        "blob_hash": page_image_hash,
                        "width": 1700,
                        "height": 2200,
                    }
                },
                "text_pages": {
                    "3": "The total contract value is $1,250,000 payable in March."
                },
            },
        )
        html_artifact = record_source_artifact(
            project,
            artifact_kind="capture",
            media_type="text/html",
            blob_hash=html_hash,
            source_url="https://example.test/minutes",
            canonical_url="https://example.test/minutes",
            title="Board minutes",
            filename="minutes.html",
        )
        audio_artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="audio/mpeg",
            blob_hash=audio_hash,
            filename="hearing.mp3",
            duration_ms=180_000,
        )
        video_artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="video/mp4",
            blob_hash=video_hash,
            filename="hearing.mp4",
            duration_ms=300_000,
        )
        table_artifact = record_source_artifact(
            project,
            artifact_kind="external_record",
            media_type="application/vnd.frisket.table+json",
            title="Extracted bid table",
            external_ref={"system": "fixture", "record_id": "bid-table-7"},
        )

        spans = [
            record_source_span(
                project,
                artifact_id=pdf_artifact["id"],
                span_kind="whole",
                snippet="Contract document",
            ),
            record_source_span(
                project,
                artifact_id=pdf_artifact["id"],
                span_kind="region",
                page_start=3,
                page_end=3,
                bbox=[
                    {
                        "space": "page_normalized",
                        "x0": 0.57,
                        "y0": 0.40,
                        "x1": 0.73,
                        "y1": 0.45,
                    }
                ],
                quote="$1,250,000",
                metadata={
                    "raw": {
                        "provider_space": "pixels",
                        "x0": 969,
                        "y0": 880,
                        "x1": 1241,
                        "y1": 990,
                    }
                },
            ),
            record_source_span(
                project,
                artifact_id=pdf_artifact["id"],
                span_kind="text",
                page_start=3,
                page_end=3,
                quote="$1,250,000",
                snippet="total contract value is $1,250,000",
            ),
            record_source_span(
                project,
                artifact_id=html_artifact["id"],
                span_kind="html",
                selector={"css": "#award", "quote_context": "p#award"},
                quote="Award value was $1,250,000.",
            ),
            record_source_span(
                project,
                artifact_id=audio_artifact["id"],
                span_kind="temporal",
                start_ms=81_234,
                end_ms=93_410,
                quote="the contract value was one point two five million",
            ),
            record_source_span(
                project,
                artifact_id=video_artifact["id"],
                span_kind="temporal",
                start_ms=120_000,
                end_ms=132_000,
                bbox=[
                    {
                        "space": "frame_normalized",
                        "x0": 0.12,
                        "y0": 0.22,
                        "x1": 0.48,
                        "y1": 0.66,
                    }
                ],
                selector={"representative_frame_ms": 124_500},
                quote="slide showing $1.25M",
            ),
            record_source_span(
                project,
                artifact_id=table_artifact["id"],
                span_kind="table",
                selector={
                    "table_index": 0,
                    "row_index": 2,
                    "column_name": "amount",
                    "cell": "C3",
                },
                snippet="amount=$1,250,000",
            ),
        ]

        link = record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=current_ref,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            op_id=op_id,
            receipt_id="receipt-map-extract",
            link_role="primary_support",
            confidence=0.91,
            producer={
                "action_kind": "map.extract",
                "model": "provider/model",
                "grounding_method": "model_bbox",
            },
            metadata={"warnings": ["cross_artifact_support"]},
            spans=[
                {
                    "span_id": span["id"],
                    "rank": index,
                    "span_role": "support",
                    "required": index == 1,
                }
                for index, span in enumerate(spans)
            ],
        )
        unrelated_same_cell = record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                **current_ref,
                "run_id": run_id + 999,
                "kind": "run_result",
            },
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            run_id=run_id + 999,
            op_id=op_id,
            link_role="support",
            spans=[{"span_id": spans[0]["id"]}],
        )

        assert {
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        }.issubset(_table_names(project))
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 5
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0] == 7
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_link_spans").fetchone()[0]
            == 8
        )
        _assert_prefixed_uuid(pdf_artifact["stable_id"], "source_artifact:")
        _assert_prefixed_uuid(spans[1]["stable_id"], "source_span:")
        _assert_prefixed_uuid(link["stable_id"], "evidence_link:")

        cell_payload = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            project_id="project-evidence",
        )
        assert cell_payload["schema_version"] == "frisket.cell_evidence.v1"
        assert cell_payload["current_value_ref"] == current_ref
        assert cell_payload["stale_count"] == 0
        assert [item["stable_id"] for item in cell_payload["links"]] == [
            link["stable_id"]
        ]
        assert cell_payload["links"][0]["viewer_href"].endswith(
            f"/evidence/links/{link['stable_id']}/viewer"
        )

        viewer = resolve_evidence_viewer(
            project, link["stable_id"], project_id="project-evidence"
        )
        assert viewer["schema_version"] == "frisket.evidence_viewer.v1"
        assert viewer["link"]["stable_id"] == link["stable_id"]
        assert viewer["link"]["status"] == "active"
        assert viewer["link"]["producer"]["grounding_method"] == "model_bbox"
        assert viewer["warnings"] == ["cross_artifact_support"]
        artifacts = {item["stable_id"]: item for item in viewer["artifacts"]}
        assert set(artifacts) == {
            pdf_artifact["stable_id"],
            html_artifact["stable_id"],
            audio_artifact["stable_id"],
            video_artifact["stable_id"],
            table_artifact["stable_id"],
        }
        pdf_payload = artifacts[pdf_artifact["stable_id"]]
        assert "path" not in json.dumps(pdf_payload)
        assert pdf_payload["artifact_ref"] == {
            "kind": "source_artifact",
            "stable_id": pdf_artifact["stable_id"],
            "artifact_kind": "file",
            "media_type": "application/pdf",
            "blob": {
                "hash": pdf_hash,
                "url": f"/api/projects/project-evidence/blobs/{pdf_hash}",
                "filename": "contract.pdf",
            },
            "source_url": None,
            "external_ref": {},
        }
        region = next(
            span for span in pdf_payload["spans"] if span["span_kind"] == "region"
        )
        assert region["status"] == "active"
        assert region["selector"]["page_start"] == 3
        assert region["selector"]["page_end"] == 3
        assert region["selector"]["bbox"][0]["space"] == "page_normalized"
        assert region["snippet"] == "$1,250,000"
        assert region["raw"]["provider_space"] == "pixels"
        text = next(
            span for span in pdf_payload["spans"] if span["span_kind"] == "text"
        )
        assert "char_start" not in text["selector"]
        assert text["snippet"] == "total contract value is $1,250,000"
        assert pdf_payload["pages"][0]["page"] == 3
        assert pdf_payload["pages"][0]["image"]["blob_hash"] == page_image_hash

        html = artifacts[html_artifact["stable_id"]]["spans"][0]
        assert html["selector"]["html"]["css"] == "#award"
        assert html["snippet"] == "Award value was $1,250,000."
        audio = artifacts[audio_artifact["stable_id"]]["spans"][0]
        assert audio["selector"]["start_ms"] == 81_234
        assert audio["selector"]["end_ms"] == 93_410
        video = artifacts[video_artifact["stable_id"]]["spans"][0]
        assert video["selector"]["start_ms"] == 120_000
        assert video["selector"]["bbox"][0]["space"] == "frame_normalized"
        assert video["selector"]["temporal"]["representative_frame_ms"] == 124_500
        table = artifacts[table_artifact["stable_id"]]["spans"][0]
        assert table["selector"]["table"] == {
            "table_index": 0,
            "row_index": 2,
            "column_name": "amount",
            "cell": "C3",
        }
        coarse = next(
            span for span in pdf_payload["spans"] if span["span_kind"] == "whole"
        )
        assert coarse["selector"]["kind"] == "whole"

        gc_preview = project.gc_blobs(dry_run=True)
        assert gc_preview["hashes"] == [unused_hash]
        project.gc_blobs()
        remaining_hashes = {
            str(row["hash"]) for row in project.db.execute("SELECT hash FROM blobs")
        }
        assert unused_hash not in remaining_hashes
        assert {
            pdf_hash,
            page_image_hash,
            html_hash,
            audio_hash,
            video_hash,
        }.issubset(remaining_hashes)

        edit_result = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "idempotency_key": "manual-edit-evidence-stales@v1",
                "params": {
                    "edits": [
                        {
                            "row_id": row_id,
                            "column_id": column_id,
                            "value": "manually corrected value",
                        }
                    ]
                },
            },
            project_id="project-evidence",
        )
        assert edit_result.status == "completed"

        link_rows = project.db.execute(
            "SELECT stable_id, status, stale_reason, stale_at "
            "FROM evidence_links ORDER BY id"
        ).fetchall()
        statuses = {row["stable_id"]: dict(row) for row in link_rows}
        assert statuses[link["stable_id"]]["status"] == "stale"
        assert statuses[link["stable_id"]]["stale_reason"] == "manual_cell_edit"
        assert statuses[link["stable_id"]]["stale_at"]
        assert statuses[unrelated_same_cell["stable_id"]]["status"] == "active"
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 2
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_link_spans").fetchone()[0]
            == 8
        )

        active_after_edit = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            project_id="project-evidence",
        )
        assert active_after_edit["links"] == []
        assert active_after_edit["stale_count"] == 1
        stale_after_edit = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            include_stale=True,
            project_id="project-evidence",
        )
        assert [item["status"] for item in stale_after_edit["links"]] == ["stale"]

        stale_viewer = resolve_evidence_viewer(
            project, link["stable_id"], project_id="project-evidence"
        )
        assert stale_viewer["link"]["status"] == "stale"
        assert stale_viewer["link"]["stale_reason"] == "manual_cell_edit"
        assert (
            sum(len(artifact["spans"]) for artifact in stale_viewer["artifacts"]) == 7
        )
    finally:
        project.close()

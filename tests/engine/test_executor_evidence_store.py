from __future__ import annotations

from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    EVIDENCE_VIEWER_SCHEMA_VERSION,
    list_cell_evidence,
    mark_evidence_stale_for_cell_refs,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    resolve_evidence_viewer,
)
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def test_evidence_store_records_lists_resolves_and_stales_cell_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "evidence-store.frisket", name="Evidence")
    sheet_id = project.add_sheet("Docs")
    source_col = project.add_column(sheet_id, "source", type="file")
    output_col = project.add_column(sheet_id, "answer", type="text", ai_generated=True)
    row_id = project.add_rows(
        sheet_id, [{"source": "contract.pdf"}], {"source": source_col}
    )[0]
    op_id = project.append_op("map.extract", {"kind": "map.extract"})
    run_store = RunResultStore(project)
    run_id = run_store.start_run(
        op_id,
        sheet_id,
        "map.extract",
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": output_col, "value": "$1,250,000"}],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, output_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, output_col, row_ids=[row_id])
    current_ref = refs[row_id]

    blob_hash = project.add_blob(
        b"%PDF-1.4 contract",
        filename="contract.pdf",
        mime="application/pdf",
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=blob_hash,
        filename="contract.pdf",
        page_count=3,
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        page_start=2,
        quote="$1,250,000",
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=current_ref,
        spans=[{"span_id": span["id"]}],
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=output_col,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-1",
    )

    listed = list_cell_evidence(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=output_col,
        project_id="project-evidence",
    )
    assert listed["schema_version"] == "frisket.cell_evidence.v1"
    assert listed["stale_count"] == 0
    assert listed["links"][0]["stable_id"] == link["stable_id"]
    assert (
        "/api/projects/project-evidence/evidence/links/"
        in listed["links"][0]["viewer_href"]
    )

    viewer = resolve_evidence_viewer(
        project,
        link["stable_id"],
        project_id="project-evidence",
    )
    assert viewer["schema_version"] == EVIDENCE_VIEWER_SCHEMA_VERSION
    assert viewer["artifacts"][0]["filename"] == "contract.pdf"
    assert viewer["artifacts"][0]["spans"][0]["quote"] == "$1,250,000"

    stale = mark_evidence_stale_for_cell_refs(
        project,
        [{"row_id": row_id, "column_id": output_col, "current_value_ref": current_ref}],
    )
    assert stale == [link["stable_id"]]
    listed_stale = list_cell_evidence(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=output_col,
        include_stale=True,
    )
    assert listed_stale["stale_count"] == 1
    assert listed_stale["links"][0]["status"] == "stale"

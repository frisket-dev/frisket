from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.server.services.sheet_grid import _sheet_data_payload


def test_grid_source_labels_use_retained_metadata_for_loaded_cells(tmp_path):
    project = Project.create(tmp_path / "source-links.frisket", name="Sources")
    try:
        sheet = project.add_sheet("Children")
        source = project.add_column(sheet, "source", type="link")
        external = project.add_column(sheet, "website", type="link")
        rows = project.add_rows(
            sheet,
            [
                {
                    "source": "/p/project/evidence/split%3Aone?page=3",
                    "website": "https://example.com",
                }
            ],
            {"source": source, "website": external},
        )
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            filename="Original.pdf",
            page_count=8,
        )
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page_range",
            page_start=3,
            page_end=5,
        )
        record_evidence_link(
            project,
            stable_id="split:one",
            subject_kind="cell_value",
            subject_ref={"kind": "source_cell", "row_id": rows[0], "column_id": source},
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=rows[0],
            column_id=source,
            link_role="source_provenance",
        )
        payload = _sheet_data_payload(
            project, sheet, project.columns(sheet), rows, total=1
        )
        assert payload["rows"][0]["meta"][str(source)]["source_link"] == {
            "label": "Original.pdf · pp 3–5"
        }
        assert "source_link" not in payload["rows"][0]["meta"][str(external)]
        assert (
            payload["rows"][0]["cells"][str(source)]
            == "/p/project/evidence/split%3Aone?page=3"
        )
        projected = _sheet_data_payload(
            project, sheet, [project.get_column(external)], rows, total=1
        )
        assert str(source) not in projected["rows"][0]["meta"]
    finally:
        project.close()

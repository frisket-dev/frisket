import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName, type WireColumn } from './helpers';

// EvidencePageView + RegionOverlay (EvidenceViewer.tsx:246-286) are the viewer
// half of the bounding-box chain and read `artifact.pages[].image` /
// `.regions[].bbox` exactly in the shape
// `store/evidence.py:686-742`'s `_page_payloads` emits. This spec seeds the
// backend shape directly (the same `record_source_artifact` ->
// `record_source_span` -> `record_evidence_link` producer pattern
// `executor/action_families/media/ocr.py::_write_media_ocr_evidence` uses,
// mirrored the way evidence-viewer-temporal-seek.spec.ts seeds a transcribed
// row) rather than running a real OCR engine.
//
// Page images are served over the EXISTING generic blob route
// (`GET /api/projects/{pid}/blobs/{digest}`, `server/routes/project_blobs.py`)
// -- `_blob_url` in `store/evidence.py` already points there, and OCR's
// `_persist_page_image` already calls `project.add_blob`, so no new serving
// route was needed for this task.

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

// A minimal valid 1x1 PNG (matches tests/engine/test_ocr_grounding_spans.py
// PNG_1X1) -- real image bytes so the blob route's mime lookup + the <img>
// element both resolve; the overlay's position math is percentage-based
// (normalizedRegionStyle, EvidenceViewer.tsx:516-523) so it does not depend on
// decoded pixel dimensions.
const PNG_1X1_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';

type SeededGroundedDoc = {
  sheetId: number;
  groundedRowId: number;
  fallbackRowId: number;
  textColumnId: number;
  groundedLinkStableId: string;
  fallbackLinkStableId: string;
};

// The region span's normalized bbox -- asserted against the overlay's inline
// style percentages below (left/top/width/height = x0/y0/(x1-x0)/(y1-y0)).
const REGION_BBOX = { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.4 };

function seedGroundedPdfRows(pid: string): SeededGroundedDoc {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import base64
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, png_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Docs")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    text_column_id = project.add_column(sheet_id, "Text", "text", ai_generated=True)
    png = base64.b64decode(png_b64)

    # Both rows share ONE op/run for the Text column: columns.current_run_id
    # is a single column-level pointer (store/runs.py::point_column_at_run),
    # so pointing it at a second per-row run would orphan the first row's
    # value (it would resolve to origin=missing). One run with both rows'
    # results, written before point_column_at_run runs once, mirrors how a
    # real media.ocr batch action covers a row selection in one run.
    grounded_row_id = project.add_rows(sheet_id, [{"Source": "doc.pdf"}], {"Source": source_column_id})[0]
    fallback_row_id = project.add_rows(sheet_id, [{"Source": "scan.pdf"}], {"Source": source_column_id})[0]
    op_id = project.append_op(
        "media.ocr",
        {"schema_version": "frisket.action.v2", "kind": "media.ocr", "params": {"output_name": "Text"}},
        label="ocr docs",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "media.ocr", model="local/rapidocr",
        params={"output_name": "Text"}, total_rows=2, row_ids=[grounded_row_id, fallback_row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {"row_id": grounded_row_id, "column_id": text_column_id, "value": "page one heading page two invoice total 42", "confidence": 0.9, "justification": "ocr"},
            {"row_id": fallback_row_id, "column_id": text_column_id, "value": "unrenderable scan text", "confidence": 0.9, "justification": "ocr"},
        ],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, text_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, text_column_id, row_ids=[grounded_row_id, fallback_row_id])
    text_ref = refs[grounded_row_id]
    ftext_ref = refs[fallback_row_id]
    fop_id, frun_id = op_id, run_id

    # --- grounded row: two-page PDF, page 1 image-only, page 2 image + region ---
    doc_blob = project.add_blob(png, "doc.pdf", "application/pdf", metadata={"pages": 2, "kind": "pdf"})
    page1_blob = project.add_blob(png, None, "image/png", metadata={"kind": "image", "width": 10, "height": 10, "page_image": True})
    page2_blob = project.add_blob(png, None, "image/png", metadata={"kind": "image", "width": 10, "height": 10, "page_image": True})
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=doc_blob,
        filename="doc.pdf",
        page_count=2,
        source_sheet_id=sheet_id,
        source_row_id=grounded_row_id,
        source_column_id=source_column_id,
        metadata={
            "engine": "rapidocr",
            "output_name": "Text",
            "dpi": 200,
            "page_images": {
                "1": {"blob_hash": page1_blob, "width": 10, "height": 10, "downscaled": False},
                "2": {"blob_hash": page2_blob, "width": 10, "height": 10, "downscaled": False},
            },
        },
    )
    page1_span = record_source_span(
        project, artifact_id=artifact["id"], span_kind="page_range",
        page_start=1, page_end=1, snippet="page one heading",
        metadata={"engine": "rapidocr"},
    )
    page2_span = record_source_span(
        project, artifact_id=artifact["id"], span_kind="page_range",
        page_start=2, page_end=2, snippet="invoice total 42",
        metadata={"engine": "rapidocr"},
    )
    region_span = record_source_span(
        project, artifact_id=artifact["id"], span_kind="region",
        page_start=2, page_end=2,
        bbox=[{"space": "page_normalized", "x0": ${REGION_BBOX.x0}, "y0": ${REGION_BBOX.y0}, "x1": ${REGION_BBOX.x1}, "y1": ${REGION_BBOX.y1}}],
        quote="invoice total 42", snippet="invoice total 42",
        selector={"engine": "rapidocr", "score": 0.97},
        metadata={"raw": {"text": "invoice total 42"}},
    )
    grounded_link = record_evidence_link(
        project, subject_kind="cell", subject_ref=text_ref,
        spans=[
            {"span_id": page1_span["id"], "rank": 0},
            {"span_id": page2_span["id"], "rank": 1},
            {"span_id": region_span["id"], "rank": 2},
        ],
        sheet_id=sheet_id, row_id=grounded_row_id, column_id=text_column_id,
        run_id=run_id, op_id=op_id, receipt_id="receipt-ocr-grounded",
        link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr", "engine": "rapidocr"},
    )

    # --- fallback row: same shape, but metadata carries NO page_images (a
    # page whose OCR-render step never persisted a blob -- e.g. the render
    # step failed honestly) though it DOES carry text_pages, so the fallback
    # exercises both branches: no image AND the page text still renders.
    fallback_blob = project.add_blob(png, "scan.pdf", "application/pdf", metadata={"pages": 1, "kind": "pdf"})
    fallback_artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=fallback_blob,
        filename="scan.pdf",
        page_count=1,
        source_sheet_id=sheet_id,
        source_row_id=fallback_row_id,
        source_column_id=source_column_id,
        metadata={
            "engine": "rapidocr",
            "output_name": "Text",
            "dpi": 200,
            "page_images": {},
            "text_pages": {"1": "unrenderable scan text"},
        },
    )
    fallback_page_span = record_source_span(
        project, artifact_id=fallback_artifact["id"], span_kind="page_range",
        page_start=1, page_end=1, snippet="unrenderable scan text",
        metadata={"engine": "rapidocr"},
    )
    fallback_link = record_evidence_link(
        project, subject_kind="cell", subject_ref=ftext_ref,
        spans=[{"span_id": fallback_page_span["id"], "rank": 0}],
        sheet_id=sheet_id, row_id=fallback_row_id, column_id=text_column_id,
        run_id=frun_id, op_id=fop_id, receipt_id="receipt-ocr-fallback",
        link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr", "engine": "rapidocr"},
    )

    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "groundedRowId": grounded_row_id,
        "fallbackRowId": fallback_row_id,
        "textColumnId": text_column_id,
        "groundedLinkStableId": grounded_link["stable_id"],
        "fallbackLinkStableId": fallback_link["stable_id"],
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, PNG_1X1_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededGroundedDoc;
}

async function openTextEvidence(page: Page, columns: WireColumn[], rowIndex: number) {
  await dblclickCell(page, columns, 'Text', rowIndex);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  const evidenceSection = rowDrawer
    .getByTestId('workbench-contribution-frisket-core-row-inspector-section-evidence')
    .filter({ has: page.getByTestId('cell-evidence-active-Text') });
  await expect(evidenceSection).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-Text').click();
  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  return viewer;
}

test('grounded OCR PDF row: page images render (not the text fallback), the region highlight overlays at the right normalized position, and clicking its span navigates to that page', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('evidence-viewer-region-overlay'));
  const seeded = seedGroundedPdfRows(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTextEvidence(page, columns, 0);
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.groundedLinkStableId);

  // Both pages render as real page images, not the no-image text fallback.
  const pages = viewer.getByTestId('evidence-page');
  await expect(pages).toHaveCount(2);
  await expect(viewer.getByTestId('evidence-page-warning')).toHaveCount(0);
  const page1 = pages.filter({ hasText: 'Page 1' });
  const page2 = pages.filter({ hasText: 'Page 2' });
  await expect(page1.getByTestId('evidence-page-image')).toBeVisible();
  await expect(page2.getByTestId('evidence-page-image')).toBeVisible();

  // The region's highlight overlay sits on page 2 at the seeded normalized
  // bbox -- asserted directly off the overlay's inline style percentages
  // (EvidenceViewer.tsx normalizedRegionStyle: left/top/width/height =
  // x0/y0/(x1-x0)/(y1-y0) as `${n * 100}%`).
  const highlight = page2.getByTestId('evidence-region-highlight');
  await expect(highlight).toHaveCount(1);
  // Read the inline style attribute directly (not getComputedStyle, which
  // resolves percentages to pixels) -- normalizedRegionStyle sets these as
  // literal `${n * 100}%` strings (EvidenceViewer.tsx:516-523).
  const style = await highlight.evaluate((el) => ({
    left: el.style.left,
    top: el.style.top,
    width: el.style.width,
    height: el.style.height,
  }));
  expect(style.left).toBe(`${REGION_BBOX.x0 * 100}%`);
  expect(style.top).toBe(`${REGION_BBOX.y0 * 100}%`);
  expect(style.width).toBe(`${(REGION_BBOX.x1 - REGION_BBOX.x0) * 100}%`);
  expect(style.height).toBe(`${(REGION_BBOX.y1 - REGION_BBOX.y0) * 100}%`);
  await expect(page1.getByTestId('evidence-region-highlight')).toHaveCount(0);

  // Page 2 starts out of view (stacked below page 1 in the scroll pane).
  await expect(page2).not.toBeInViewport();

  // Clicking the region's span card (right pane) navigates the left pane to
  // that span's page.
  const regionSpanCard = viewer.getByTestId('evidence-span-region');
  await expect(regionSpanCard).toHaveCount(1);
  await regionSpanCard.click();
  await expect(page2).toBeInViewport();
});

test('OCR PDF row with no persisted page image still falls back gracefully', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('evidence-viewer-region-fallback'));
  const seeded = seedGroundedPdfRows(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTextEvidence(page, columns, 1);
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.fallbackLinkStableId);

  await expect(viewer.getByTestId('evidence-page-image')).toHaveCount(0);
  const fallback = viewer.getByTestId('evidence-page-warning');
  await expect(fallback).toBeVisible();
  await expect(fallback).toContainText('Page 1 has no rendered image');
  await expect(fallback).toContainText('unrenderable scan text');
});

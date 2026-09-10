// A citation chip's onOpen sets answersView.activeLinkId (the LOCAL scratch
// slice, not the global chromeStore.EvidenceViewerState), which drives the
// docked `<EvidenceViewer mode="pane">` in the Grounded Answers view's right
// pane. EvidenceViewer.tsx's `mode="pane"` docking, region overlay, and
// temporal-seek mechanics are reused unchanged. This spec seeds one
// region-grounded (PDF page + bbox) row and
// one temporal-grounded (transcript + AV seek) row and drives them through the
// ANSWERS view's
// pane, not the row-drawer modal — proving the docking, not re-proving the
// viewer's own rendering (that's the evidence-viewer-* specs' job).

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, openProject, uniqueName } from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();
const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav')).toString('base64');

// A minimal valid 1x1 PNG (matches evidence-viewer-region-overlay.spec.ts's
// PNG_1X1 / tests/engine/test_ocr_grounding_spans.py).
const PNG_1X1_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';

const REGION_BBOX = { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.4 };

interface SeededRegionSheet {
  sheetId: number;
  rowId: number;
  linkStableId: string;
}

function seedRegionGroundedSheet(pid: string): SeededRegionSheet {
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

    row_id = project.add_rows(sheet_id, [{"Source": "doc.pdf"}], {"Source": source_column_id})[0]
    op_id = project.append_op(
        "media.ocr",
        {"schema_version": "frisket.action.v2", "kind": "media.ocr", "params": {"output_name": "Text"}},
        label="ocr docs",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "media.ocr", model="local/rapidocr",
        params={"output_name": "Text"}, total_rows=1, row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": text_column_id, "value": "invoice total 42", "confidence": 0.9, "justification": "ocr"}],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, text_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, text_column_id, row_ids=[row_id])
    text_ref = refs[row_id]

    doc_blob = project.add_blob(png, "doc.pdf", "application/pdf", metadata={"pages": 1, "kind": "pdf"})
    page_blob = project.add_blob(png, None, "image/png", metadata={"kind": "image", "width": 10, "height": 10, "page_image": True})
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=doc_blob,
        filename="doc.pdf",
        page_count=1,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=source_column_id,
        metadata={
            "engine": "rapidocr",
            "output_name": "Text",
            "page_images": {"1": {"blob_hash": page_blob, "width": 10, "height": 10, "downscaled": False}},
        },
    )
    page_span = record_source_span(
        project, artifact_id=artifact["id"], span_kind="page_range",
        page_start=1, page_end=1, snippet="invoice total 42",
        metadata={"engine": "rapidocr"},
    )
    region_span = record_source_span(
        project, artifact_id=artifact["id"], span_kind="region",
        page_start=1, page_end=1,
        bbox=[{"space": "page_normalized", "x0": ${REGION_BBOX.x0}, "y0": ${REGION_BBOX.y0}, "x1": ${REGION_BBOX.x1}, "y1": ${REGION_BBOX.y1}}],
        quote="invoice total 42", snippet="invoice total 42",
        selector={"engine": "rapidocr", "score": 0.97},
        metadata={"raw": {"text": "invoice total 42"}},
    )
    link = record_evidence_link(
        project, subject_kind="cell", subject_ref=text_ref,
        spans=[
            {"span_id": page_span["id"], "rank": 0},
            {"span_id": region_span["id"], "rank": 1},
        ],
        sheet_id=sheet_id, row_id=row_id, column_id=text_column_id,
        run_id=run_id, op_id=op_id, receipt_id="receipt-ocr-grounded",
        link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr", "engine": "rapidocr"},
    )
    project.db.commit()
    print(json.dumps({"sheetId": sheet_id, "rowId": row_id, "linkStableId": link["stable_id"]}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, PNG_1X1_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededRegionSheet;
}

interface SeededTemporalSheet {
  sheetId: number;
  rowId: number;
  linkStableId: string;
}

function seedTemporalGroundedSheet(pid: string): SeededTemporalSheet {
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

workspace, pid, media_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Hearings")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    transcript_column_id = project.add_column(sheet_id, "Transcript", "text", ai_generated=True)
    row_id = project.add_rows(sheet_id, [{"Source": "hearing.wav"}], {"Source": source_column_id})[0]
    op_id = project.append_op(
        "media.transcribe",
        {"schema_version": "frisket.action.v2", "kind": "media.transcribe", "params": {"output_column": "Transcript"}},
        label="transcribe hearing",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "media.transcribe", model="local/faster_whisper",
        params={"output_column": "Transcript"}, total_rows=1, row_ids=[row_id],
    )
    transcript_text = "PANEL DISCUSSION OPENING THE MERIDIAN REPORT CLOSING REMARKS"
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": transcript_column_id, "value": transcript_text, "confidence": 0.88, "justification": "local transcription"}],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, transcript_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, transcript_column_id, row_ids=[row_id])
    transcript_ref = refs[row_id]

    media_bytes = base64.b64decode(media_b64)
    blob_hash = project.add_blob(media_bytes, "hearing.wav", "audio/wav")
    artifact = record_source_artifact(
        project, artifact_kind="av", media_type="audio/wav", blob_hash=blob_hash,
        filename="hearing.wav", duration_ms=2000,
        source_sheet_id=sheet_id, source_row_id=row_id, source_column_id=source_column_id,
        metadata={"engine": "faster_whisper", "output_name": "transcript"},
    )
    span0 = record_source_span(
        project, artifact_id=artifact["id"], span_kind="temporal",
        start_ms=0, end_ms=900, quote="PANEL DISCUSSION OPENING", selector={"segment_index": 0},
    )
    span1 = record_source_span(
        project, artifact_id=artifact["id"], span_kind="temporal",
        start_ms=1000, end_ms=1900, quote="THE MERIDIAN REPORT", selector={"segment_index": 1},
    )
    link = record_evidence_link(
        project, subject_kind="cell_value", subject_ref=transcript_ref,
        sheet_id=sheet_id, row_id=row_id, column_id=transcript_column_id,
        run_id=run_id, op_id=op_id, receipt_id="receipt-transcribe",
        link_role="primary_support", confidence=0.88,
        producer={"action_kind": "media.transcribe", "model": "local/faster_whisper", "grounding_method": "temporal_segment"},
        spans=[
            {"span_id": span0["id"], "rank": 0, "span_role": "support", "required": True},
            {"span_id": span1["id"], "rank": 1, "span_role": "support", "required": False},
        ],
    )
    project.db.commit()
    print(json.dumps({"sheetId": sheet_id, "rowId": row_id, "linkStableId": link["stable_id"]}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, AUDIO_FIXTURE_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededTemporalSheet;
}

async function openAnswersView(page: Page): Promise<void> {
  await page.getByTestId('view-switch-answers').click();
  await expect(page.getByTestId('grounded-answers-view')).toBeVisible();
}

test('region-grounded citation: the DOCKED pane (not the modal) renders the page image + region highlight, auto-defaulted on open', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('answers-docked-region'));
  const seeded = seedRegionGroundedSheet(pid);
  await openProject(page, pid, seeded.sheetId);
  await openAnswersView(page);

  const pane = page.getByTestId('answers-evidence-pane');
  // Default-link resolution: the active row's first citation
  // opens WITHOUT any click.
  const viewer = pane.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  // RETARGETED (evidence-pane-reading-surface-v1): the Answers docked pane
  // now collapses the ref-dump aside (link summary + span cards) by default
  // -- see EvidenceViewer.tsx's `defaultShowDetails` -- so the export-ref
  // assertion below needs an explicit "Show details" first. Deliberate: the
  // pane's default view is now the reading surface, not the raw ref dump.
  await viewer.getByTestId('evidence-details-toggle').click();
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.linkStableId);
  await expect(page.getByTestId('evidence-viewer-backdrop')).toHaveCount(0);

  const highlight = viewer.getByTestId('evidence-region-highlight');
  await expect(highlight).toHaveCount(1);
  const style = await highlight.evaluate((el) => ({
    left: (el as HTMLElement).style.left,
    top: (el as HTMLElement).style.top,
  }));
  expect(style.left).toBe(`${REGION_BBOX.x0 * 100}%`);
  expect(style.top).toBe(`${REGION_BBOX.y0 * 100}%`);

  // Clicking the row's citation chip re-opens the SAME link in the SAME
  // docked pane (the LOCAL activeLinkId path, not the global event).
  const chip = page.getByTestId('answers-cell').first().getByTestId('answers-citation-chip');
  await chip.click();
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.linkStableId);

  // Closing the pane returns to the empty-state placeholder.
  await viewer.getByRole('button', { name: 'Close evidence viewer' }).click();
  await expect(page.getByTestId('answers-evidence-empty')).toBeVisible();
});

test('temporal-grounded citation: the docked pane plays audio + click-to-seeks the segment list, global evidence host stays undisturbed', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('answers-docked-temporal'));
  const seeded = seedTemporalGroundedSheet(pid);
  await openProject(page, pid, seeded.sheetId);
  await openAnswersView(page);

  const pane = page.getByTestId('answers-evidence-pane');
  const viewer = pane.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  // RETARGETED (evidence-pane-reading-surface-v1): see the region-grounded
  // test above -- the docked pane's ref-dump aside is collapsed by default.
  await viewer.getByTestId('evidence-details-toggle').click();
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.linkStableId);

  const player = viewer.getByTestId('evidence-audio');
  await expect(player).toBeVisible();
  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(2);
  await segments.nth(1).click();
  await expect(player).toHaveAttribute('data-seek-seconds', '1');
  await expect
    .poll(() => player.evaluate((el) => (el as HTMLMediaElement).currentTime))
    .toBeGreaterThan(0);

  // The global evidence host (row-drawer / mainView-split path) is
  // untouched -- no modal backdrop, no mainView split, exactly one viewer
  // instance on the page (the docked one).
  await expect(page.getByTestId('evidence-viewer-backdrop')).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('evidence-viewer')).toHaveCount(1);
});

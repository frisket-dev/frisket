// A citation chip's onOpen sets answersView.activeLinkId (the LOCAL scratch
// slice, not the global chromeStore.CitationViewerState), which drives the
// docked `<CitationViewer mode="pane">` in the Grounded Answers view's right
// pane. This spec seeds one temporal-grounded (transcript + AV seek) row and
// drives it through the ANSWERS view's pane, not the row-drawer modal —
// proving the docking, not re-proving the viewer's own rendering.

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { REPO_ROOT, createProject, openProject, uniqueName } from './helpers';
import { testPython } from '../support/testPython';

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav')).toString('base64');

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

from frisket.engine.store.citations import record_citation, record_source_artifact, record_source_span
from frisket.engine.store.transactions import write_transaction
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, media_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Hearings")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    transcript_column_id = project.add_column(sheet_id, "Transcript", "text", generated=True)
    row_id = project.add_rows(sheet_id, [{"Source": "hearing.wav"}], {"Source": source_column_id})[0]
    change_id = project.append_change(
        "media.transcribe",
        {"schema_version": "frisket.action.v2", "kind": "media.transcribe", "params": {"generated_column": "Transcript"}},
        label="transcribe hearing",
    )
    run_id = run_store.start_run(
        change_id, sheet_id, "media.transcribe", model="local/faster_whisper",
        params={"generated_column": "Transcript"}, total_rows=1, row_ids=[row_id],
    )
    transcript_text = "PANEL DISCUSSION OPENING THE MERIDIAN REPORT CLOSING REMARKS"
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": transcript_column_id, "value": transcript_text, "confidence": 0.88, "justification": "local transcription"}],
    )
    run_store.finish_run(run_id)
    with write_transaction(project.db):
        run_store.point_column_at_run(change_id, transcript_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, transcript_column_id, row_ids=[row_id])
    transcript_ref = refs[row_id]

    media_bytes = base64.b64decode(media_b64)
    blob_hash = project.add_blob(media_bytes, "hearing.wav", "audio/wav")
    with write_transaction(project.db):
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
        link = record_citation(
            project, subject_kind="cell_value", subject_ref=transcript_ref,
            sheet_id=sheet_id, row_id=row_id, column_id=transcript_column_id,
            run_id=run_id, change_id=change_id, receipt_id="receipt-transcribe",
            link_role="primary_support", confidence=0.88,
            producer={"action_kind": "media.transcribe", "model": "local/faster_whisper", "grounding_method": "temporal_segment"},
            spans=[
                {"span_id": span0["id"], "rank": 0, "span_role": "support", "required": True},
                {"span_id": span1["id"], "rank": 1, "span_role": "support", "required": False},
            ],
        )
    print(json.dumps({"sheetId": sheet_id, "rowId": row_id, "linkStableId": link["stable_id"]}))
finally:
    project.close()
`;
  const output = execFileSync(testPython(), ['-c', script, workspace, pid, AUDIO_FIXTURE_B64], {
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
  // The docked pane collapses its link-detail aside by default.
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

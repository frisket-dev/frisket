import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName, type WireColumn } from './helpers';

// The server contract
// (guard, padding, duration cap) is covered by
// tests/engine/test_provenance_download_clip.py; this spec exercises the actual
// click-to-download path in the evidence viewer against the live stack --
// the viewer affordance sits beside the seek button on a temporal span and
// downloads a real ffmpeg-cut clip of the span's own locally-resolvable blob
// (no parent-video lineage traversal -- that's a separate gated task).

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav')).toString('base64');

type SeededRow = {
  sheetId: number;
  rowId: number;
  transcriptColumnId: number;
};

function seedTranscribedRow(pid: string): SeededRow {
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
        {
            "schema_version": "frisket.action.v2",
            "kind": "media.transcribe",
            "params": {"output_column": "Transcript"},
        },
        label="transcribe hearing",
    )
    run_id = run_store.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        model="local/faster_whisper",
        params={"output_column": "Transcript"},
        total_rows=1,
        row_ids=[row_id],
    )
    transcript_text = "PANEL DISCUSSION OPENING THE MERIDIAN REPORT"
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": transcript_column_id,
                "value": transcript_text,
                "confidence": 0.88,
                "justification": "local transcription",
            }
        ],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, transcript_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, transcript_column_id, row_ids=[row_id])
    transcript_ref = refs[row_id]

    # Real 2.0s audio bytes (matches evidence-viewer-temporal-seek.spec.ts's
    # fixture note) -- ffmpeg needs playable bytes, not a fake placeholder.
    media_bytes = base64.b64decode(media_b64)
    blob_hash = project.add_blob(media_bytes, "hearing.wav", "audio/wav")

    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="audio/wav",
        blob_hash=blob_hash,
        filename="hearing.wav",
        duration_ms=2000,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=source_column_id,
        metadata={"engine": "faster_whisper", "output_name": "transcript"},
    )
    span0 = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=0,
        end_ms=900,
        quote="PANEL DISCUSSION OPENING",
        selector={"segment_index": 0},
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=transcript_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=transcript_column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-transcribe",
        link_role="primary_support",
        confidence=0.88,
        producer={
            "action_kind": "media.transcribe",
            "model": "local/faster_whisper",
            "grounding_method": "temporal_segment",
        },
        spans=[{"span_id": span0["id"], "rank": 0, "span_role": "support", "required": True}],
    )
    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "rowId": row_id,
        "transcriptColumnId": transcript_column_id,
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, AUDIO_FIXTURE_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededRow;
}

async function openTranscriptEvidence(page: Page, columns: WireColumn[], rowIndex = 0) {
  await dblclickCell(page, columns, 'Transcript', rowIndex);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  const evidenceSection = rowDrawer
    .getByTestId('workbench-contribution-frisket-core-row-inspector-section-evidence')
    .filter({ has: page.getByTestId('cell-evidence-active-Transcript') });
  await expect(evidenceSection).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-Transcript').click();
  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  return viewer;
}

test('temporal span: the download-clip affordance streams a real ffmpeg-cut clip', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('provenance-download-clip'));
  const seeded = seedTranscribedRow(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  // Beside the seek button, not instead of it.
  const seekButton = viewer.getByTestId('evidence-temporal-segment').first();
  await expect(seekButton).toBeVisible();
  const downloadButton = viewer.getByTestId('evidence-temporal-download-clip').first();
  await expect(downloadButton).toBeVisible();

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    downloadButton.click(),
  ]);

  // span [0,900]ms + default 500ms padding, clamped to the 2000ms artifact
  // duration on the end side only -> [0,1400]ms -> "0m00s-0m01s".
  expect(download.suggestedFilename()).toBe('hearing_0m00s-0m01s.wav');
  const downloadPath = await download.path();
  expect(downloadPath).toBeTruthy();
  const bytes = readFileSync(downloadPath as string);
  expect(bytes.length).toBeGreaterThan(0);
  expect(bytes.subarray(0, 4).toString('ascii')).toBe('RIFF');
  expect(bytes.subarray(8, 12).toString('ascii')).toBe('WAVE');
});

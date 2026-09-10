import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName, type WireColumn } from './helpers';

// A temporal citation whose source blob carries
// `webpage_url` (yt-dlp capture metadata, ytdlp.py:302-308 -- the field is
// `yt_dlp_id`, there is no `video_id`) gains an "open at timestamp" link on
// the origin platform beside the seek button. Platform-agnostic where
// webpage_url exists (YouTube gets a composed `t=` offset; any other
// webpage_url still gets a plain link); graceful absence (no link, no
// clutter) when the blob carries no webpage_url at all.

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

function seedRow(pid: string, blobMetadataPython: string): SeededRow {
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

    media_bytes = base64.b64decode(media_b64)
    blob_metadata = ${blobMetadataPython}
    blob_hash = project.add_blob(media_bytes, "hearing.wav", "audio/wav", metadata=blob_metadata or None)

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
        start_ms=1000,
        end_ms=1900,
        quote="THE MERIDIAN REPORT",
        selector={"segment_index": 1},
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

test('YouTube-sourced blob: temporal span gets an open-at-timestamp deep link beside the seek button', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('provenance-deep-link-youtube'));
  const seeded = seedRow(
    pid,
    `{
        "yt_dlp_id": "dQw4w9WgXcQ",
        "title": "Hearing on the Meridian Report",
        "duration_seconds": 2.0,
        "extractor": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }`,
  );
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const segment = viewer.getByTestId('evidence-temporal-segment').first();
  await expect(segment).toBeVisible();
  const deepLink = viewer.getByTestId('evidence-temporal-deep-link').first();
  await expect(deepLink).toBeVisible();
  // start_ms=1000 -> t=1s; the youtube extractor composes the watch URL + offset.
  await expect(deepLink).toHaveAttribute('href', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1s');
  await expect(deepLink).toHaveAttribute('target', '_blank');
});

test('non-YouTube webpage_url: deep link still renders, without a fabricated timestamp param', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('provenance-deep-link-other'));
  const seeded = seedRow(
    pid,
    `{
        "title": "Committee Livestream",
        "duration_seconds": 2.0,
        "extractor": "Generic",
        "webpage_url": "https://example-news.test/hearings/meridian-report",
    }`,
  );
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const deepLink = viewer.getByTestId('evidence-temporal-deep-link').first();
  await expect(deepLink).toBeVisible();
  await expect(deepLink).toHaveAttribute('href', 'https://example-news.test/hearings/meridian-report');
});

test('local file with no capture metadata: no deep link renders (graceful absence, no clutter)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('provenance-deep-link-absent'));
  const seeded = seedRow(pid, '{}');
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  await expect(viewer.getByTestId('evidence-temporal-segment')).toHaveCount(1);
  await expect(viewer.getByTestId('evidence-temporal-deep-link')).toHaveCount(0);
});

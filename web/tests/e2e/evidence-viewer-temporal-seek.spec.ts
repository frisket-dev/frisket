import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName, type WireColumn } from './helpers';

// The evidence viewer's temporal path has a click-to-seek mechanic lifted from
// MediaPlayerPeek.tsx:22-40 + transcriptAlign.ts: users can say
// 'this happened from 3m30s-4m10s' and be able to see the transcript and
// also click a button to hear it on the ONE evidence-viewer surface.
//
// Seeds a transcribed row the way media.transcribe's evidence writer does
// (src/frisket/engine/executor/action_families/media/transcribe.py:380-509): one
// `av` source artifact (real tiny-audio.wav / tiny-video.mp4 bytes, both
// 2.0s), three `temporal` spans (two ordinary, one pre-clamped to the
// artifact's duration_ms the way the writer's overrun clamp leaves it —
// `metadata.warnings: ["temporal_span_clamped_to_artifact_duration_ms"]`).

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav')).toString('base64');
const VIDEO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-video.mp4')).toString('base64');

type SeededTranscribedRow = {
  sheetId: number;
  rowId: number;
  transcriptColumnId: number;
  linkStableId: string;
};

function seedTranscribedRow(pid: string, kind: 'audio' | 'video'): SeededTranscribedRow {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const mediaB64 = kind === 'audio' ? AUDIO_FIXTURE_B64 : VIDEO_FIXTURE_B64;
  const script = String.raw`
import base64
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, kind, media_b64 = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Hearings")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    transcript_column_id = project.add_column(sheet_id, "Transcript", "text", ai_generated=True)
    filename = "hearing.wav" if kind == "audio" else "hearing.mp4"
    mime = "audio/wav" if kind == "audio" else "video/mp4"
    row_id = project.add_rows(sheet_id, [{"Source": filename}], {"Source": source_column_id})[0]
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
    transcript_text = "PANEL DISCUSSION OPENING THE MERIDIAN REPORT CLOSING REMARKS"
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
    blob_hash = project.add_blob(media_bytes, filename, mime)

    # duration_ms=2000 matches the real fixture (both tiny-audio.wav and
    # tiny-video.mp4 are exactly 2.0s) so seeking within the clip is real.
    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type=mime,
        blob_hash=blob_hash,
        filename=filename,
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
    span1 = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1000,
        end_ms=1900,
        quote="THE MERIDIAN REPORT",
        selector={"segment_index": 1},
    )
    # Pre-clamped the way the transcribe writer's overrun clamp
    # (transcribe.py:485-495) leaves a span: end_ms clamped to duration_ms,
    # raw unclamped value kept in metadata.raw, warning flagged.
    span2 = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1800,
        end_ms=2000,
        quote="CLOSING REMARKS",
        selector={"segment_index": 2},
        metadata={
            "raw": {"raw_end_ms": 2600},
            "warnings": ["temporal_span_clamped_to_artifact_duration_ms"],
        },
    )
    spans = [span0, span1, span2]
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
        spans=[
            {"span_id": s["id"], "rank": i, "span_role": "support", "required": i == 0}
            for i, s in enumerate(spans)
        ],
    )
    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "rowId": row_id,
        "transcriptColumnId": transcript_column_id,
        "linkStableId": link["stable_id"],
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, kind, mediaB64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededTranscribedRow;
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

test('transcribed audio row: evidence viewer shows player + captions + clickable segment list, seeks within tolerance, clamped span warns', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('evidence-viewer-seek-audio'));
  const seeded = seedTranscribedRow(pid, 'audio');
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.linkStableId);

  const player = viewer.getByTestId('evidence-audio');
  await expect(player).toBeVisible();
  await expect(viewer.getByTestId('evidence-video')).toHaveCount(0);
  // Captions track still renders (the pre-existing VTT path must not regress).
  await expect(player.locator('track[kind="captions"]')).toHaveCount(1);

  // The clickable segment list beside the player: quote + m:ss per span.
  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(3);
  await expect(segments.nth(0)).toContainText('0:00');
  await expect(segments.nth(0)).toContainText('PANEL DISCUSSION OPENING');
  await expect(segments.nth(1)).toContainText('0:01');
  await expect(segments.nth(1)).toContainText('THE MERIDIAN REPORT');

  // Click segment index 1 (start 1000ms) -> seeks the player to 1s.
  await segments.nth(1).click();
  await expect(player).toHaveAttribute('data-seek-seconds', '1');
  // The real seek ran (the MediaPlayerPeek.tsx:22-40 mechanic): currentTime
  // advanced off zero, within tolerance of the requested target.
  await expect
    .poll(() => player.evaluate((el) => (el as HTMLMediaElement).currentTime))
    .toBeGreaterThan(0);
  const currentTime = await player.evaluate((el) => (el as HTMLMediaElement).currentTime);
  expect(Math.abs(currentTime - 1)).toBeLessThan(0.5);

  // The clamped span (segment index 2) still surfaces its warning via
  // SpanCard (metadata.warnings), unchanged by the seek addition.
  const clampedSpanCard = viewer.getByTestId('evidence-span-temporal').filter({ hasText: 'CLOSING REMARKS' });
  await expect(clampedSpanCard).toContainText('temporal_span_clamped_to_artifact_duration_ms');
});

test('transcribed video row: evidence viewer renders <video> not <audio>, with the same click-to-seek segment list', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('evidence-viewer-seek-video'));
  const seeded = seedTranscribedRow(pid, 'video');
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const player = viewer.getByTestId('evidence-video');
  await expect(player).toBeVisible();
  await expect(viewer.getByTestId('evidence-audio')).toHaveCount(0);

  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(3);
  await segments.nth(1).click();
  await expect(player).toHaveAttribute('data-seek-seconds', '1');
  await expect
    .poll(() => player.evaluate((el) => (el as HTMLMediaElement).currentTime))
    .toBeGreaterThan(0);
});

// A citation evidence link SCOPED to just the cited segments (the shape a
// transcript-grounded map.extract field records: only the cited spans are
// marked required, see store/evidence.py citation_temporal_runs) -- the viewer
// auto-seeks to the first cited segment on open and marks the covered range,
// with no click and no new plumbing between CitationChip and the viewer.
function seedScopedTranscriptRow(pid: string): SeededTranscribedRow {
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
    transcript_text = "COMMITTEE VOTE ON THE AMENDMENT CLOSING REMARKS"
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
    # Neither span starts at 0ms -- proves genuine auto-seek, not "already there".
    span0 = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1000,
        end_ms=1400,
        quote="COMMITTEE VOTE ON THE AMENDMENT",
        selector={"segment_index": 4},
    )
    span1 = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1500,
        end_ms=1900,
        quote="CLOSING REMARKS",
        selector={"segment_index": 5},
    )
    spans = [span0, span1]
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=transcript_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=transcript_column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-citation",
        link_role="citation",
        producer={"action_kind": "map.extract"},
        spans=[
            {"span_id": s["id"], "rank": i, "span_role": "citation", "required": True}
            for i, s in enumerate(spans)
        ],
    )
    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "rowId": row_id,
        "transcriptColumnId": transcript_column_id,
        "linkStableId": link["stable_id"],
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, AUDIO_FIXTURE_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededTranscribedRow;
}

test('citation-scoped evidence link: viewer auto-seeks to the first answering segment on open and marks the range, no click needed', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('evidence-viewer-autoseek'));
  const seeded = seedScopedTranscriptRow(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const player = viewer.getByTestId('evidence-audio');
  await expect(player).toBeVisible();

  // Auto-seek WITHOUT any click: neither span starts at 0ms, so a seek to 1s
  // proves the viewer actually sought segment 4's start, not just defaulted.
  await expect(player).toHaveAttribute('data-seek-seconds', '1');
  await expect
    .poll(() => player.evaluate((el) => (el as HTMLMediaElement).currentTime))
    .toBeGreaterThan(0);
  const currentTime = await player.evaluate((el) => (el as HTMLMediaElement).currentTime);
  expect(Math.abs(currentTime - 1)).toBeLessThan(0.5);

  // The first (answering) segment is marked active on open, and the covered
  // range is legible without clicking anything.
  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(2);
  await expect(segments.nth(0)).toHaveAttribute('data-active', 'true');
  await expect(segments.nth(1)).not.toHaveAttribute('data-active');
  await expect(viewer.getByTestId('evidence-temporal-range')).toContainText('0:01');
});

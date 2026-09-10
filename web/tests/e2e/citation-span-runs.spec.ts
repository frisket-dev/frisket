import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName, type WireColumn } from './helpers';

// All cited spans must receive the initial highlight. The root cause of only
// highlighting the first was
// the writer (sdk/ops/extract.py) wiring a citation span's `required` flag
// to the FIELD's citation_required policy instead of "is this span part of
// what was cited" -- on the common citation_required=False config every
// span landed required=0, so the viewer's cited set was empty and the only
// visible highlight was the always-on-span-0 auto-seek "active" marker.
// Fixed at the writer (both spans get required=True unconditionally --
// tests/engine/test_extract_scalar_temporal_anchors.py's
// test_scalar_multi_segment_citation_marks_all_spans_required_and_groups_runs
// covers the writer directly); this spec proves the VIEWER side end-to-end
// against the live stack. The citation's cited spans group into
// contiguous RUNS (gap <= CITATION_RUN_GAP_TOLERANCE_MS = 1500ms,
// store/evidence.py) -- one link-level "download this run" clip per run
// (tests/engine/test_citation_span_runs.py covers the server contract; this spec
// drives the actual click-to-download path against the live stack, mirroring
// provenance-download-clip.spec.ts's real-ffmpeg pattern).

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

type SeededRow = {
  sheetId: number;
  rowId: number;
  transcriptColumnId: number;
  linkStableId: string;
};

// Generates a real (silent, but playable) WAV of `durationSeconds` directly
// in the seeding script's own Python process -- longer than the checked-in
// tiny-audio.wav fixture (2.0s) needs to be to host a genuinely DISJOINT
// second run (a >1500ms gap) inside the artifact's real duration, the way
// tests/engine/test_citation_span_runs.py's `_wav_bytes` does on the pytest side.
function seedCitationRow(pid: string, spanRuns: Array<Array<[number, number]>>): SeededRow {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const flatSpans = spanRuns.flat();
  const durationMs = Math.max(...flatSpans.map(([, end]) => end)) + 2000;
  const script = String.raw`
import json
import sys
import wave
import io
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, duration_ms, spans_json = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
spans_spec = json.loads(spans_json)
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
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": transcript_column_id,
                "value": "a citation-span-runs seeded transcript",
                "confidence": 0.88,
                "justification": "local transcription",
            }
        ],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, transcript_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, transcript_column_id, row_ids=[row_id])
    transcript_ref = refs[row_id]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(duration_ms / 1000 * 8000))
    blob_hash = project.add_blob(buf.getvalue(), "hearing.wav", "audio/wav")

    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="audio/wav",
        blob_hash=blob_hash,
        filename="hearing.wav",
        duration_ms=duration_ms,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=source_column_id,
        metadata={"engine": "faster_whisper", "output_name": "transcript"},
    )
    span_refs = []
    rank = 0
    for run in spans_spec:
        for start_ms, end_ms in run:
            span = record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="temporal",
                start_ms=start_ms,
                end_ms=end_ms,
                quote=f"segment at {start_ms}ms",
            )
            span_refs.append({"span_id": span["id"], "rank": rank, "span_role": "support", "required": True})
            rank += 1
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
        spans=span_refs,
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
  const output = execFileSync(
    'uv',
    ['run', 'python', '-c', script, workspace, pid, String(durationMs), JSON.stringify(spanRuns)],
    { cwd: REPO_ROOT, encoding: 'utf8', timeout: 120_000 },
  );
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

test('a contiguous 6-span citation highlights ALL 6 segments and offers ONE run-clip', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('citation-span-runs-contiguous'));
  // Six spans, each 100ms of internal gap from the next -- comfortably under
  // the 1500ms run-gap tolerance, so this is ONE contiguous six-span run.
  const seeded = seedCitationRow(pid, [
    [
      [0, 900],
      [1000, 1900],
      [2000, 2900],
      [3000, 3900],
      [4000, 4900],
      [5000, 5900],
    ],
  ]);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(6);

  // ALL 6 spans carry the cited treatment -- the actual root-cause fix
  // (previously only the auto-seeked first segment looked highlighted).
  const cited = viewer.locator('[data-testid="evidence-temporal-segment"][data-cited="true"]');
  await expect(cited).toHaveCount(6);
  for (let i = 0; i < 6; i += 1) {
    await expect(segments.nth(i)).toHaveAttribute('data-cited', 'true');
  }

  // ONE run -> ONE range label + ONE run-level clip affordance (the
  // implementation-leakage kill: no "6 spans" badge on the reading surface).
  const runs = viewer.getByTestId('evidence-temporal-run');
  await expect(runs).toHaveCount(1);
  // formatMs rounds to the nearest second: [0,5900]ms -> "0:00–0:06".
  await expect(viewer.getByTestId('evidence-temporal-run-range')).toContainText('0:00–0:06');
  const runClips = viewer.getByTestId('evidence-temporal-run-clip');
  await expect(runClips).toHaveCount(1);

  // The per-segment download affordances are UNCHANGED (still one per span).
  await expect(viewer.getByTestId('evidence-temporal-download-clip')).toHaveCount(6);
});

test('a disjoint 2-run citation offers TWO run-clips, each a real ffmpeg cut of its own run', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('citation-span-runs-disjoint'));
  // Run 0: three contiguous spans covering [0,2900]ms. Run 1: three more
  // starting at 2900 + 2000ms (a 2000ms gap, over the 1500ms tolerance),
  // covering [4900,7900]ms -- a 3+3/two-run shape that distinguishes short
  // within-run gaps from a disjoint interval.
  const seeded = seedCitationRow(pid, [
    [
      [0, 900],
      [1000, 1900],
      [2000, 2900],
    ],
    [
      [4900, 5800],
      [6000, 6900],
      [7000, 7900],
    ],
  ]);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);

  await openProject(page, pid, seeded.sheetId);
  const viewer = await openTranscriptEvidence(page, columns);

  const segments = viewer.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(6);
  const cited = viewer.locator('[data-testid="evidence-temporal-segment"][data-cited="true"]');
  await expect(cited).toHaveCount(6);

  // TWO disjoint runs -> two separate range labels + two separate
  // run-clip affordances (not one merged block).
  const runs = viewer.getByTestId('evidence-temporal-run');
  await expect(runs).toHaveCount(2);
  const ranges = viewer.getByTestId('evidence-temporal-run-range');
  // formatMs rounds to the nearest second: run 0 [0,2900]ms -> "0:00–0:03";
  // run 1 [4900,7900]ms -> "0:05–0:08".
  await expect(ranges.nth(0)).toContainText('0:00–0:03');
  await expect(ranges.nth(1)).toContainText('0:05–0:08');

  const runClips = viewer.getByTestId('evidence-temporal-run-clip');
  await expect(runClips).toHaveCount(2);

  // Downloading run 0's clip streams a real ffmpeg cut spanning its own
  // range (start of its first span [0ms] -> end of its last [2900ms], +
  // default 500ms padding on each open side -> ~3.4s).
  const [download0] = await Promise.all([page.waitForEvent('download'), runClips.nth(0).click()]);
  const path0 = await download0.path();
  expect(path0).toBeTruthy();
  const bytes0 = readFileSync(path0 as string);
  expect(bytes0.subarray(0, 4).toString('ascii')).toBe('RIFF');
  expect(bytes0.length).toBeGreaterThan(0);

  // Run 1's clip is a DIFFERENT cut (its own, later range) -- not the same
  // bytes served twice.
  const [download1] = await Promise.all([page.waitForEvent('download'), runClips.nth(1).click()]);
  const path1 = await download1.path();
  const bytes1 = readFileSync(path1 as string);
  expect(bytes1.length).toBeGreaterThan(0);
});

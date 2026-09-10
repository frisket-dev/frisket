// The evidence pane scopes the transcript to the selected video and presents
// the extraction, citation, and video preview in order. The transcript remains
// segmented, with the relevant segments highlighted.
//
// ROOT CAUSE (proven by this seed): a citation link can bind temporal spans
// across MANY source artifacts (the transcribe grounding binds one span per
// segment; a cross-row / shared-artifact citation reaches several videos). The
// docked EvidenceViewer rendered EVERY such artifact's segment list — every
// video's transcript concatenated in one pane. The refinement (a) scopes the
// docked viewer to the SELECTED row's own source artifact via scopeRowId, and
// (b) composes the pane into three parts — the extraction value(s), the
// citation quote, then the media preview + segmented transcript with the cited
// segment(s) highlighted.
//
// This seeds two rows, each an AV row with its OWN transcript, and each row's
// Finding-cell citation link binds BOTH videos' segments (the concatenation
// bug). Selecting row 1 must scope the pane to video 1's segments only (video 2
// text absent) and vice-versa; the cited (required) segment is highlighted.
//
// The extraction value and chips now live in the
// MIDDLE pane, which that task scoped to the selected row too — repeating it
// inside the docked evidence pane became pure duplication once both panes
// read the same selected row, so it was dropped from the evidence pane
// (AnswersView.tsx's composition note). This spec's part-1 assertions were
// re-pointed at the middle pane's `answers-cell`; the pane's citation +
// segment-scoping assertions (parts 2/3 below) are UNCHANGED and must stay
// green — they are what this task actually proves.

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

interface SeededSheet {
  sheetId: number;
  rowA: number;
  rowB: number;
  linkA: string;
  linkB: string;
}

function seedTwoTranscriptRows(pid: string): SeededSheet {
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
    source_col = project.add_column(sheet_id, "Source", "file")
    finding_col = project.add_column(sheet_id, "Finding", "text", ai_generated=True)
    row_a = project.add_rows(sheet_id, [{"Source": "hearingA.wav"}], {"Source": source_col})[0]
    row_b = project.add_rows(sheet_id, [{"Source": "hearingB.wav"}], {"Source": source_col})[0]

    op_id = project.append_op(
        "map.extract",
        {"schema_version": "frisket.action.v2", "kind": "map.extract", "params": {"output_name": "Finding"}},
        label="extract findings",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "map.extract", model="local/stub",
        params={"output_name": "Finding"}, total_rows=2, row_ids=[row_a, row_b],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {"row_id": row_a, "column_id": finding_col, "value": "Budget approved", "confidence": 0.9, "justification": "x"},
            {"row_id": row_b, "column_id": finding_col, "value": "Motion denied", "confidence": 0.9, "justification": "x"},
        ],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, finding_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, finding_col, row_ids=[row_a, row_b])

    media_bytes = base64.b64decode(media_b64)

    def make_artifact(row_id, filename, quotes):
        blob_hash = project.add_blob(media_bytes, filename, "audio/wav")
        artifact = record_source_artifact(
            project, artifact_kind="av", media_type="audio/wav", blob_hash=blob_hash,
            filename=filename, duration_ms=2000,
            source_sheet_id=sheet_id, source_row_id=row_id, source_column_id=source_col,
            metadata={"engine": "faster_whisper", "output_name": "transcript"},
        )
        spans = []
        for idx, quote in enumerate(quotes):
            spans.append(record_source_span(
                project, artifact_id=artifact["id"], span_kind="temporal",
                start_ms=idx * 1000, end_ms=idx * 1000 + 900, quote=quote,
                selector={"segment_index": idx},
            ))
        return spans

    # Two distinct transcripts. Segment index 1 is the "relevant" (cited) one.
    spans_a = make_artifact(row_a, "hearingA.wav", ["ALPHA OPENING STATEMENT", "ALPHA THE BUDGET WAS APPROVED"])
    spans_b = make_artifact(row_b, "hearingB.wav", ["BRAVO PROCEDURAL MATTERS", "BRAVO THE MOTION WAS DENIED"])

    # Each row's Finding-cell link binds BOTH videos' spans (the concatenation
    # bug). Rank 0 = that row's own cited segment, so the link snippet (first by
    # rank) is the citation quote; the other row's spans are the foreign noise
    # scopeRowId must drop.
    link_a = record_evidence_link(
        project, subject_kind="cell_value", subject_ref=refs[row_a],
        sheet_id=sheet_id, row_id=row_a, column_id=finding_col,
        run_id=run_id, op_id=op_id, receipt_id="receipt-a",
        link_role="primary_support", confidence=0.9,
        producer={"action_kind": "map.extract", "grounding_method": "segment_indices"},
        spans=[
            {"span_id": spans_a[1]["id"], "rank": 0, "span_role": "support", "required": True},
            {"span_id": spans_a[0]["id"], "rank": 1, "span_role": "support", "required": False},
            {"span_id": spans_b[0]["id"], "rank": 2, "span_role": "support", "required": False},
            {"span_id": spans_b[1]["id"], "rank": 3, "span_role": "support", "required": False},
        ],
    )
    link_b = record_evidence_link(
        project, subject_kind="cell_value", subject_ref=refs[row_b],
        sheet_id=sheet_id, row_id=row_b, column_id=finding_col,
        run_id=run_id, op_id=op_id, receipt_id="receipt-b",
        link_role="primary_support", confidence=0.9,
        producer={"action_kind": "map.extract", "grounding_method": "segment_indices"},
        spans=[
            {"span_id": spans_b[1]["id"], "rank": 0, "span_role": "support", "required": True},
            {"span_id": spans_b[0]["id"], "rank": 1, "span_role": "support", "required": False},
            {"span_id": spans_a[0]["id"], "rank": 2, "span_role": "support", "required": False},
            {"span_id": spans_a[1]["id"], "rank": 3, "span_role": "support", "required": False},
        ],
    )
    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id, "rowA": row_a, "rowB": row_b,
        "linkA": link_a["stable_id"], "linkB": link_b["stable_id"],
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, AUDIO_FIXTURE_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededSheet;
}

async function openAnswersView(page: Page): Promise<void> {
  await page.getByTestId('view-switch-answers').click();
  await expect(page.getByTestId('grounded-answers-view')).toBeVisible();
}

test('the docked pane scopes the segmented transcript to the selected row and highlights the cited segment', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('answers-refine'));
  const seeded = seedTwoTranscriptRows(pid);
  await openProject(page, pid, seeded.sheetId);
  await openAnswersView(page);

  const pane = page.getByTestId('answers-evidence-pane');

  // Part 1 — the extraction value(s) for the selected (first) row, up top —
  // now the middle pane's row-scoped cell (answers-middle-pane-row-scope-v1).
  const middleCell = page.getByTestId('answers-cell');
  await expect(middleCell).toHaveCount(1);
  await expect(middleCell.getByTestId('answers-cell-content')).toContainText('Budget approved');

  // Part 2 — the citation quote (the cited/required segment's quote, which is
  // the link's rank-0 span).
  const citation = pane.getByTestId('answers-evidence-citation');
  await expect(citation).toBeVisible();
  await expect(citation).toContainText('BUDGET');

  // Part 3 — the segmented transcript, SCOPED to row A's own video: exactly
  // its two segments, and NONE of video B's ("BRAVO") text, even though the
  // link binds both videos' spans.
  const segments = pane.getByTestId('evidence-temporal-segment');
  await expect(segments).toHaveCount(2);
  await expect(pane.getByTestId('evidence-temporal-segments')).not.toContainText('BRAVO');
  await expect(pane.getByTestId('evidence-temporal-segments')).toContainText('ALPHA');

  // The cited (relevant) segment is highlighted (data-cited), and it is the
  // budget one — not merely the auto-seeked first row.
  const cited = pane.getByTestId('evidence-temporal-segment').and(page.locator('[data-cited="true"]'));
  await expect(cited).toHaveCount(1);
  await expect(cited).toContainText('BUDGET');

  // Selecting row B swaps the WHOLE reading to row B: its extraction, its
  // citation, and its transcript ("BRAVO") — with row A's ("ALPHA") absent.
  await page
    .getByTestId('answers-row-item')
    .and(page.locator(`[data-row-id="${seeded.rowB}"]`))
    .click();

  await expect(middleCell).toHaveCount(1);
  await expect(middleCell.getByTestId('answers-cell-content')).toContainText('Motion denied');
  await expect(citation).toContainText('MOTION');
  await expect(pane.getByTestId('evidence-temporal-segment')).toHaveCount(2);
  await expect(pane.getByTestId('evidence-temporal-segments')).toContainText('BRAVO');
  await expect(pane.getByTestId('evidence-temporal-segments')).not.toContainText('ALPHA');

  const citedB = pane.getByTestId('evidence-temporal-segment').and(page.locator('[data-cited="true"]'));
  await expect(citedB).toHaveCount(1);
  await expect(citedB).toContainText('MOTION');

  // The global evidence host is undisturbed — the docked pane is the only
  // viewer, no modal backdrop.
  await expect(page.getByTestId('evidence-viewer-backdrop')).toHaveCount(0);
  await expect(page.getByTestId('evidence-viewer')).toHaveCount(1);
});

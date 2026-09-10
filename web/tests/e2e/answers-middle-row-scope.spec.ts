// The middle pane includes only the extract result for the row selected in the
// left rail. Earlier behavior rendered all extractions.
//
// answers-view-shell-v1 / answers-view-docked-evidence-v1
// originally read the middle pane as an all-rows list (every loaded row's
// value for the chosen column). That reading is deliberately superseded
// here: the middle pane now renders ONLY the left rail's active row — its
// chosen-column value + citation chip(s) — and swaps wholesale when a
// different row is selected. Those two specs' own middle-pane assertions
// were re-pointed at the single scoped cell (see their comments); this spec
// owns proving the row-scoping and the swap-on-select behavior itself.
//
// COMPOSITION note (see also answers-view-evidence-refinement.spec.ts): the
// right-hand docked evidence pane's former "extraction" section (the value +
// chips, part 1 of that task's three-part reading) is now REDUNDANT with
// this middle pane's scoped cell — both would show the exact same value for
// the exact same selected row — so it was dropped from the evidence pane.
// The middle pane is the single place the extraction value renders; the
// evidence pane starts directly at the citation.
//
// This deliberately does NOT stack multiple cited columns' values in the
// middle pane for the selected row: the column picker already chooses which column's
// value shows, and `useColumnEvidence` only fetches links for that one
// column, so stacking would require fetching evidence for every cited
// column just to display them here. Keeping the single-column contract is
// the simpler, already-supported behavior.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, openProject, uniqueName } from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

interface SeededSheet {
  sheetId: number;
  rowIds: number[];
  summaryValues: string[];
}

function seedThreeRowSheet(pid: string): SeededSheet {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid = sys.argv[1], sys.argv[2]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Docs")
    name_col = project.add_column(sheet_id, "Name", "text")
    source_col = project.add_column(sheet_id, "Source", "file")
    summary_col = project.add_column(sheet_id, "Summary", "text", ai_generated=True)
    names = ["Acme Corp", "Beta LLC", "Gamma Inc"]
    row_ids = project.add_rows(
        sheet_id,
        [{"Name": n, "Source": f"{n}.pdf"} for n in names],
        {"Name": name_col, "Source": source_col},
    )

    op_id = project.append_op(
        "map.extract",
        {"schema_version": "frisket.action.v2", "kind": "map.extract", "params": {"output_column": "Summary"}},
        label="extract Summary",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "map.extract", model="provider/model",
        params={"output_column": "Summary"}, total_rows=len(row_ids), row_ids=row_ids,
    )
    summary_values = [
        "Revenue grew 12% year over year.",
        "Headcount doubled to 400 employees.",
        "Expanded into three new markets.",
    ]
    write_claimed_test_results(project, run_id, [
        {"row_id": rid, "column_id": summary_col, "value": v, "confidence": 0.9}
        for rid, v in zip(row_ids, summary_values)
    ])
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, summary_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, summary_col, row_ids=row_ids)
    artifact = record_source_artifact(
        project, artifact_kind="file", media_type="application/pdf", title="Contract", filename="doc.pdf",
    )
    for rid, v in zip(row_ids, summary_values):
        span = record_source_span(
            project, artifact_id=artifact["id"], span_kind="region", page_start=1, page_end=1, quote=v, snippet=v,
        )
        record_evidence_link(
            project, subject_kind="cell_value", subject_ref=refs[rid],
            sheet_id=sheet_id, row_id=rid, column_id=summary_col, run_id=run_id, op_id=op_id,
            link_role="primary_support", confidence=0.9, spans=[{"span_id": span["id"], "rank": 0}],
        )

    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "rowIds": row_ids,
        "summaryValues": summary_values,
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid], {
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

test('the middle pane renders only the left-rail-selected row, swapping wholesale on selection change', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('answers-row-scope'));
  const seeded = seedThreeRowSheet(pid);
  await openProject(page, pid, seeded.sheetId);
  await openAnswersView(page);

  const middleColumn = page.getByTestId('answers-column');
  const cells = page.getByTestId('answers-cell');

  // No row explicitly selected yet -- defaults to the first row (Acme Corp).
  // Exactly ONE cell renders, and none of the OTHER rows' values appear
  // anywhere in the middle pane, even though all three rows are loaded for
  // the left rail.
  await expect(cells).toHaveCount(1);
  await expect(cells.first()).toContainText(seeded.summaryValues[0]);
  await expect(middleColumn).not.toContainText(seeded.summaryValues[1]);
  await expect(middleColumn).not.toContainText(seeded.summaryValues[2]);

  // Selecting a different row via the left rail swaps the middle pane's
  // content wholesale -- still exactly one cell, now the selected row's.
  await page.getByTestId('answers-row-item').nth(2).click();
  await expect(cells).toHaveCount(1);
  await expect(cells.first()).toContainText(seeded.summaryValues[2]);
  await expect(cells.first()).toHaveAttribute('data-row-id', String(seeded.rowIds[2]));
  await expect(middleColumn).not.toContainText(seeded.summaryValues[0]);
  await expect(middleColumn).not.toContainText(seeded.summaryValues[1]);

  // And again to the middle row, proving this isn't a one-shot default.
  await page.getByTestId('answers-row-item').nth(1).click();
  await expect(cells).toHaveCount(1);
  await expect(cells.first()).toContainText(seeded.summaryValues[1]);
  await expect(middleColumn).not.toContainText(seeded.summaryValues[0]);
  await expect(middleColumn).not.toContainText(seeded.summaryValues[2]);

  // The scoped cell's own citation chip still opens the docked evidence pane
  // for THIS row (the middle pane's scoping doesn't break the existing
  // docked-evidence wiring, answers-view-docked-evidence-v1).
  await cells.first().getByTestId('answers-citation-chip').click();
  await expect(page.getByTestId('answers-evidence-pane').getByTestId('evidence-viewer')).toBeVisible();
});

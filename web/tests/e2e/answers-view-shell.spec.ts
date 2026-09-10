// The Grounded Answers reading view is a WorkViewKind 'answers' that replaces the grid
// (like Document) with three panes -- left row list (rowTitle,
// selectionStore-synced), middle the chosen cited column's per-row values
// with citation chips, right the docked EvidenceViewer pane (wired in
// answers-view-docked-evidence-v1). This spec pins the SHELL: registration/
// availability (data-keyed to SheetMeta.citedColumnIds), the three-pane
// render, the column picker (mirrors DocumentView's Title select), and
// bidirectional selection sync with the grid -- the same shape
// workbench-ia-document-view.spec.ts pins for Document.
//
// Seeds two cited columns (map.extract-shaped writes + evidence links, the
// same producer pattern test_answers_view_column_evidence_batch.py's
// _seed_two_generated_rows uses) directly via the store, rather than running
// a real extract action.
//
// The middle pane used to render one `answers-cell` per loaded row (an all-rows list).
// It now renders exactly ONE `answers-cell` — the left rail's active row —
// per the row-scoped reading contract. This spec's middle-pane assertions use
// that single scoped cell; answers-middle-row-scope.spec.ts
// owns proving the swap-on-select behavior itself.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, selectRow, uniqueName } from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

interface SeededAnswersSheet {
  sheetId: number;
  nameColumnId: number;
  summaryColumnId: number;
  valueColumnId: number;
  rowIds: number[];
  summaryValues: string[];
  valueValues: string[];
}

function seedAnswersSheet(pid: string): SeededAnswersSheet {
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
    value_col = project.add_column(sheet_id, "Contract value", "text", ai_generated=True)
    names = ["Acme Corp", "Beta LLC", "Gamma Inc"]
    row_ids = project.add_rows(
        sheet_id,
        [{"Name": n, "Source": f"{n}.pdf"} for n in names],
        {"Name": name_col, "Source": source_col},
    )

    def write_column(col_id, output_name, values):
        op_id = project.append_op(
            "map.extract",
            {"schema_version": "frisket.action.v2", "kind": "map.extract", "params": {"output_column": output_name}},
            label=f"extract {output_name}",
        )
        run_id = run_store.start_run(
            op_id, sheet_id, "map.extract", model="provider/model",
            params={"output_column": output_name}, total_rows=len(row_ids), row_ids=row_ids,
        )
        write_claimed_test_results(project, run_id, [
            {"row_id": rid, "column_id": col_id, "value": v, "confidence": 0.9}
            for rid, v in zip(row_ids, values)
        ])
        run_store.finish_run(run_id)
        run_store.point_column_at_run(op_id, col_id, run_id)
        _values, refs = project.get_values_with_refs(sheet_id, col_id, row_ids=row_ids)
        artifact = record_source_artifact(
            project, artifact_kind="file", media_type="application/pdf", title="Contract", filename="doc.pdf",
        )
        for rid, v in zip(row_ids, values):
            span = record_source_span(
                project, artifact_id=artifact["id"], span_kind="region", page_start=1, page_end=1, quote=v, snippet=v,
            )
            record_evidence_link(
                project, subject_kind="cell_value", subject_ref=refs[rid],
                sheet_id=sheet_id, row_id=rid, column_id=col_id, run_id=run_id, op_id=op_id,
                link_role="primary_support", confidence=0.9, spans=[{"span_id": span["id"], "rank": 0}],
            )

    summary_values = [
        "Revenue grew 12% year over year.",
        "Headcount doubled to 400 employees.",
        "Expanded into three new markets.",
    ]
    value_values = ["$1,250,000", "$980,000", "$2,410,000"]
    write_column(summary_col, "Summary", summary_values)
    write_column(value_col, "Contract value", value_values)

    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "nameColumnId": name_col,
        "summaryColumnId": summary_col,
        "valueColumnId": value_col,
        "rowIds": row_ids,
        "summaryValues": summary_values,
        "valueValues": value_values,
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededAnswersSheet;
}

async function openAnswersView(page: Page): Promise<void> {
  await page.getByTestId('view-switch-answers').click();
  await expect(page.getByTestId('grounded-answers-view')).toBeVisible();
}

test('the Answers segment is data-keyed to cited columns and replaces the grid with three panes', async ({
  page,
}) => {
  // A plain CSV sheet (no evidence links) must NOT surface the segment.
  const plainPid = await createProject(page.request, uniqueName('answers-view-plain'));
  const plainSheet = await importCsv(page.request, plainPid, 'cities.csv', 'city\nParis\nBerlin\n');
  await openProject(page, plainPid, plainSheet);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('view-switch-answers')).toHaveCount(0);

  // A sheet with cited columns surfaces the segment.
  const pid = await createProject(page.request, uniqueName('answers-view-shell'));
  const seeded = seedAnswersSheet(pid);
  await openProject(page, pid, seeded.sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('view-switch-answers')).toBeVisible();
  await expect(page.getByTestId('view-switch-answers')).toHaveAttribute('aria-pressed', 'false');

  await openAnswersView(page);
  await expect(page.getByTestId('view-switch-answers')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('grid')).toHaveCount(0);

  // Left: rows, titled via rowTitle (the sheet's first column, "Name").
  const rowItems = page.getByTestId('answers-row-item');
  await expect(rowItems).toHaveCount(3);
  await expect(rowItems.nth(0)).toContainText('Acme Corp');
  await expect(rowItems.nth(1)).toContainText('Beta LLC');
  await expect(rowItems.nth(2)).toContainText('Gamma Inc');

  // Middle: the DEFAULT chosen column is the first CITED column in sheet
  // column order ("Summary" precedes "Contract value") — SCOPED to the
  // active row only (no row selected yet, so it defaults to the first row,
  // "Acme Corp"): exactly one `answers-cell`, never the other loaded rows'.
  const picker = page.getByTestId('answers-column-picker');
  await expect(picker).toBeVisible();
  await expect(picker).toHaveValue(String(seeded.summaryColumnId));
  const cells = page.getByTestId('answers-cell');
  await expect(cells).toHaveCount(1);
  await expect(cells.nth(0)).toContainText(seeded.summaryValues[0]);
  await expect(cells.nth(0).getByTestId('answers-citation-chip')).toHaveCount(1);
  await expect(page.getByTestId('answers-column')).not.toContainText(seeded.summaryValues[1]);
  await expect(page.getByTestId('answers-column')).not.toContainText(seeded.summaryValues[2]);

  // Right: the docked evidence pane frame is present (empty-state placeholder
  // until a chip is clicked or a default resolves — docked-evidence-v1 wires
  // the auto-default + click behavior; this spec only pins the frame exists).
  await expect(page.getByTestId('answers-evidence-pane')).toBeVisible();

  // The picker switches columns — the SAME scoped cell's content follows.
  await picker.selectOption(String(seeded.valueColumnId));
  await expect(cells).toHaveCount(1);
  await expect(cells.nth(0)).toContainText(seeded.valueValues[0]);
  await expect(cells.nth(0).getByTestId('answers-citation-chip')).toHaveCount(1);

  // Leaving to Grid and returning keeps the Answers view (mirrors Document's
  // "keeping prior options if returning to the same sheet").
  await page.getByTestId('view-switch-grid').click();
  await expect(page.getByTestId('grid')).toBeVisible();
  await page.getByTestId('view-switch-answers').click();
  await expect(page.getByTestId('grounded-answers-view')).toBeVisible();
  await expect(page.getByTestId('answers-column-picker')).toHaveValue(String(seeded.valueColumnId));
});

test('selection syncs BOTH ways through SelectedGridRows, matching the Document view contract', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('answers-view-sync'));
  const seeded = seedAnswersSheet(pid);
  await openProject(page, pid, seeded.sheetId);

  // Grid → Answers: selecting a grid row and switching carries the
  // selection, so the answers view highlights that row — and the middle
  // pane's SOLE scoped cell is that row's, not row 0's.
  await selectRow(page, 1);
  await openAnswersView(page);
  await expect(page.getByTestId('answers-row-item').nth(1)).toHaveAttribute('data-active', 'true');
  const cells = page.getByTestId('answers-cell');
  await expect(cells).toHaveCount(1);
  await expect(cells.first()).toHaveAttribute('data-active', 'true');
  await expect(cells.first()).toContainText(seeded.summaryValues[1]);

  // Answers → Grid: clicking a different row item writes the SAME
  // SelectedGridRows state; switching back, the grid toolbar reflects it.
  await page.getByTestId('answers-row-item').nth(0).click();
  await expect(page.getByTestId('answers-row-item').nth(0)).toHaveAttribute('data-active', 'true');
  await page.getByTestId('view-switch-grid').click();
  await expect(page.getByTestId('delete-rows-button')).toHaveAttribute(
    'title',
    /Delete 1 selected row/,
  );
});

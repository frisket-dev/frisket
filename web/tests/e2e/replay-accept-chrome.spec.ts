// User-facing contract for preserve-and-surface: a re-run NEVER overwrites a
// human-edited generated cell; the fresh generated value is recorded and
// SURFACED. This spec drives the whole flow end-to-end against the real
// stack:
//   seed a generated column -> hand-edit cells -> re-run so pending values exist
//   -> the column chip renders "N updated values · Review" (full-column N)
//   -> Review scrolls to the first pending cell and opens a popover showing
//      "Your edit" vs "Newer generated" with Accept / Keep edit
//   -> Accept and Keep each AUTO-ADVANCE to the next pending cell
//   -> accepting updates the cell and DECREMENTS the chip
//   -> Keep edit (dismiss) is DURABLE across reload
//   -> accept-all-in-column clears the whole column in one click.
//
// Deterministic generation uses map.python (local, no model): run 1
// renders 'A', we hand-edit three cells to 'EDIT#', run 2 renders 'B'. Because
// map_runner REUSES the same-named ai_generated column on re-run, run 2 writes a
// fresh (current_run_id, row_id, column_id) results row under the surviving
// edits; the three edited cells (overlay 'EDIT#') now differ from the fresh 'B'
// and become the pending set. Setup is API-driven; the page is opened afterward
// so the grid renders the chip on load. Wire rows key cells by COLUMN ID.

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  runAndWait,
  sheetColumns,
  sheetData,
  uniqueName,
  type WireColumn,
} from './helpers';

const OUT = 'out';

function seedCsv(rows: number): string {
  const body = Array.from({ length: rows }, (_, r) => `${r}`).join('\n');
  return `n\n${body}\n`;
}

// The tiny program keeps values per-row: prefix 'A' -> A0, A1, …; prefix 'B'
// -> B0, B1, …. A re-run into the existing column uses the `overwrite_existing`
// output intent (the app's regenerate-in-place path).
async function programRun(
  page: Page,
  pid: string,
  sheetId: number,
  prefix: string,
  overwrite = false,
): Promise<void> {
  await runAndWait(page.request, pid, {
    action_id: 'map.python',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: {
      input_columns: ['n'],
      code: `result = ${JSON.stringify(prefix)} + str(row['n'])`,
      return_schema: { type: 'string' },
      output_routes: [{
        name: OUT,
        path: '$',
        target: { kind: 'column', type: 'text' },
      }],
    },
    output_names: { [OUT]: OUT },
    ...(overwrite ? { replace_existing: true } : {}),
    idempotency_key: `replay-accept-${prefix}-${pid}`,
  });
}

async function cellValue(
  page: Page,
  pid: string,
  sheetId: number,
  outId: number,
  rowIndex: number,
): Promise<unknown> {
  const data = await sheetData(page.request, pid, sheetId, 0, 20);
  return data.rows[rowIndex]?.cells?.[String(outId)] ?? null;
}

function pendingCount(columns: WireColumn[], name: string): number {
  const col = columns.find((c) => c.name === name) as
    | (WireColumn & { replay_pending_count?: number })
    | undefined;
  return col?.replay_pending_count ?? 0;
}

interface Pending {
  pid: string;
  sheetId: number;
  outId: number;
}

/** Build a column with three pending cells (API-driven), then open the sheet so
 *  the grid renders the chip: render 'A', hand-edit rows 0..2 to 'EDIT#', re-run
 *  'B'. */
async function setupPendingColumn(page: Page): Promise<Pending> {
  const pid = await createProject(page.request, uniqueName('e2e-replay-accept'));
  const sheetId = await importCsv(page.request, pid, 'seed.csv', seedCsv(6));

  // Run 1: rows render A0..A5.
  await programRun(page, pid, sheetId, 'A');
  const columns = await sheetColumns(page.request, pid, sheetId);
  const outId = columns.find((c) => c.name === OUT)?.id;
  if (!outId) throw new Error('generated column "out" was not created');
  const data = await sheetData(page.request, pid, sheetId, 0, 20);
  const rowIds = data.rows.map((r) => r.id);
  expect(data.rows[5]?.cells?.[String(outId)]).toBe('A5');

  // Hand-fix rows 0,1,2 to distinct edits (manual_edit overlays).
  await editCells(page.request, pid, [
    { rowId: rowIds[0], columnId: outId, value: 'EDIT0' },
    { rowId: rowIds[1], columnId: outId, value: 'EDIT1' },
    { rowId: rowIds[2], columnId: outId, value: 'EDIT2' },
  ]);

  // Run 2 (re-run in place, reuses the same column id): rows regenerate B0..B5.
  // The three edited cells now differ from the fresh B# -> pending; rows 3..5
  // (no overlay) resolve to B# and are NOT pending.
  await programRun(page, pid, sheetId, 'B', true);
  expect(await cellValue(page, pid, sheetId, outId, 5)).toBe('B5');

  // Open the sheet fresh so the grid fetches the pending state and renders the
  // chip.
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  return { pid, sheetId, outId };
}

function chip(page: Page) {
  return page.getByTestId('grid-column-annotation');
}

async function expectChipCount(page: Page, outId: number, n: number): Promise<void> {
  const c = chip(page);
  await expect(c).toBeVisible({ timeout: 20_000 });
  await expect(c).toHaveAttribute('data-column-ids', new RegExp(String(outId)));
  await expect(c).toContainText(new RegExp(`${n} updated value`));
  await expect(c).toContainText(/Review/i);
}

test('the replay chip surfaces pending cells; Review + Accept/Keep auto-advance, decrement, and keep-edit is durable', async ({
  page,
}) => {
  const { pid, sheetId, outId } = await setupPendingColumn(page);

  // The column chip renders a FULL-COLUMN-accurate "3 updated values · Review".
  await expectChipCount(page, outId, 3);

  // Review scrolls to the first pending cell and opens the two-value popover.
  await page.getByTestId('replay-review-button').click();
  const popover = page.getByTestId('replay-accept-popover');
  await expect(popover).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('replay-your-edit')).toContainText('EDIT0');
  await expect(page.getByTestId('replay-newer-generated')).toContainText('B');

  // Accept the newer value: the cell takes 'B', the chip decrements to 2, and
  // the popover AUTO-ADVANCES to the next pending cell (row 1).
  await page.getByTestId('replay-accept-button').click();
  await expect(page.getByTestId('replay-your-edit')).toContainText('EDIT1', {
    timeout: 10_000,
  });
  await expect(page.getByTestId('replay-newer-generated')).toContainText('B');
  await expectChipCount(page, outId, 2);
  await expect
    .poll(async () => cellValue(page, pid, sheetId, outId, 0), { timeout: 10_000 })
    .toBe('B0'); // accepting updated the underlying cell to the fresh value

  // Keep edit on row 1: durable dismiss, AUTO-ADVANCE to row 2, chip -> 1.
  await page.getByTestId('replay-keep-button').click();
  await expect(page.getByTestId('replay-your-edit')).toContainText('EDIT2', {
    timeout: 10_000,
  });
  await expectChipCount(page, outId, 1);

  // Accept the last pending cell: pending set empties, the chip clears.
  await page.getByTestId('replay-accept-button').click();
  await expect(chip(page)).toHaveCount(0, { timeout: 10_000 });

  // Durable across reload: the kept edit (row 1 = 'EDIT1') does NOT re-nag.
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect(chip(page)).toHaveCount(0, { timeout: 15_000 });
  expect(await cellValue(page, pid, sheetId, outId, 1)).toBe('EDIT1'); // kept
  const finalCols = await sheetColumns(page.request, pid, sheetId);
  expect(pendingCount(finalCols, OUT)).toBe(0);
});

test('accept-all-in-column clears every pending cell in one click', async ({ page }) => {
  const { pid, sheetId, outId } = await setupPendingColumn(page);

  await expectChipCount(page, outId, 3);

  // Accept all: one click takes the fresh value for every pending cell.
  await page.getByTestId('replay-accept-all-button').click();
  await expect(chip(page)).toHaveCount(0, { timeout: 15_000 });

  // Every previously-edited cell now shows its fresh B#; count is zero.
  await expect
    .poll(async () => {
      const data = await sheetData(page.request, pid, sheetId, 0, 20);
      return [0, 1, 2].map((i) => data.rows[i]?.cells?.[String(outId)]).join(',');
    }, { timeout: 15_000 })
    .toBe('B0,B1,B2');
  const cols = await sheetColumns(page.request, pid, sheetId);
  expect(pendingCount(cols, OUT)).toBe(0);
});

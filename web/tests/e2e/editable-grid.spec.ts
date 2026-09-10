// Editable grid parity gate:
// - source cells can be edited through the canvas and persist via v1 cell.edit
// - users can append a row through the UI via v1 row.add
// - the operation history refreshes after both UI writes

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  dblclickCell,
  openHistory,
  openCellDrawer,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
  type WireColumn,
  type WireData,
} from './helpers';

function valueFor(data: WireData, columns: WireColumn[], rowIndex: number, columnName: string): unknown {
  const col = columns.find((c) => c.name === columnName);
  if (!col) throw new Error(`missing column ${columnName}`);
  return data.rows[rowIndex]?.cells[String(col.id)];
}

async function editDrawerCell(page: Page, columnName: string, value: string) {
  // The edit pencil is hover-revealed (like copy/explain) — hover the field
  // row first, as a user would.
  await page.getByTestId(`row-field-${columnName}`).hover();
  await page.getByTestId(`cell-edit-${columnName}`).click();
  await page.getByTestId(`cell-editor-${columnName}`).fill(value);
  await page.getByTestId(`cell-save-${columnName}`).click();
  await expect(page.getByTestId('row-drawer')).toContainText(value);
}

test('grid edits source cells and appends rows through logged UI actions', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('editable-grid'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'story\n"The first report mentions Alpha city hall."\n"The second report mentions Beta transit."\n',
  );
  const cellEditActions: Array<Record<string, unknown>> = [];
  const rowAddActions: Array<Record<string, unknown>> = [];
  let legacyEditCalled = false;
  let legacyRowAddCalled = false;
  await page.route(`**/api/projects/${pid}/edits`, async (route) => {
    legacyEditCalled = true;
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy /edits route should not be called' }),
    });
  });
  await page.route(`**/api/projects/${pid}/sheets/${sheetId}/rows`, async (route) => {
    legacyRowAddCalled = true;
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy /rows route should not be called' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'cell.edit') cellEditActions.push(payload);
    if (payload.action_id === 'row.add') rowAddActions.push(payload);
    await route.continue();
  });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 1 column');
  await openHistory(page);

  // 'story' is a plain non-AI text column, so it's now in-place editable
  // (grid-in-place-edit-v1) — open the drawer via the floating icon rather
  // than Enter, which glide now routes to its own overlay editor instead.
  await openCellDrawer(page, columns, 'story', 0);
  await expect(page.getByTestId('row-drawer')).toContainText('Alpha city hall');
  await editDrawerCell(page, 'story', 'The corrected report mentions Alpha zoning.');
  await expect
    .poll(async () => valueFor(await sheetData(page.request, pid, sheetId), columns, 0, 'story'))
    .toBe('The corrected report mentions Alpha zoning.');
  expect(legacyEditCalled).toBe(false);
  expect(cellEditActions).toHaveLength(1);
  expect(cellEditActions[0]).toMatchObject({
    action_id: 'cell.edit',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      edits: [
        {
          row_id: expect.any(Number),
          column_id: expect.any(Number),
          value: 'The corrected report mentions Alpha zoning.',
        },
      ],
    },
  });
  expect(cellEditActions[0]).not.toHaveProperty('kind');
  expect(cellEditActions[0]).not.toHaveProperty('capabilities');
  expect(String(cellEditActions[0]?.idempotency_key)).toMatch(/^web-cell\.edit:/);
  await expect(page.getByTestId('history-list')).toContainText('manual edit');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  await page.getByTestId('add-row-button').click();
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 1 column');
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(3);
  expect(legacyRowAddCalled).toBe(false);
  expect(rowAddActions).toHaveLength(1);
  expect(rowAddActions[0]).toMatchObject({
    action_id: 'row.add',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      sheet_id: Number(sheetId),
      cells: {},
    },
  });
  expect(rowAddActions[0]).not.toHaveProperty('kind');
  expect(rowAddActions[0]).not.toHaveProperty('capabilities');
  expect(String(rowAddActions[0]?.idempotency_key)).toMatch(/^web-row\.add:/);
  await expect(page.getByTestId('history-list')).toContainText('add row');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 1 column');
});

test('a rejected drawer cell edit stays editable and surfaces the failure', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('editable-grid-failure'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'story\n"The original report."\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id !== 'cell.edit') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'cell.edit', action_id: 'rejected-cell-edit' },
        status: 'failed',
        project_id: pid,
        run_id: null,
        receipt_id: null,
        outputs: [],
        errors: [{ code: 'invalid_cell_value', message: 'The cell edit was rejected.' }],
      }),
    });
  });

  await openProject(page, pid, sheetId);
  await openCellDrawer(page, columns, 'story', 0);
  await page.getByTestId('row-field-story').hover();
  await page.getByTestId('cell-edit-story').click();
  const editor = page.getByTestId('cell-editor-story');
  await editor.fill('Keep this draft visible.');
  await page.getByTestId('cell-save-story').click();

  await expect(page.getByTestId('error-toast')).toContainText('The cell edit was rejected.');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue('Keep this draft visible.');
  await expect.poll(async () => (
    valueFor(await sheetData(page.request, pid, sheetId), columns, 0, 'story')
  )).toBe('The original report.');
});

test('the in-place editor wraps a long unbroken text value', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('editable-grid-long-token'));
  const filename = 'ann-arbor-policy-committee-2022-04-19-extraordinarily-long-audio-excerpt.mp3';
  const sheetId = await importCsv(page.request, pid, 'files.csv', `filename\n${filename}\n`);
  const columns = await sheetColumns(page.request, pid, sheetId);

  await openProject(page, pid, sheetId);
  await dblclickCell(page, columns, 'filename', 0);

  const editor = page.locator('.gdg-growing-entry textarea.gdg-input');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue(filename);
  await expect.poll(async () => editor.evaluate((element) => {
    const textarea = element as HTMLTextAreaElement;
    const style = getComputedStyle(textarea);
    return {
      overflowWrap: style.overflowWrap,
      wordBreak: style.wordBreak,
      wrapped: textarea.getBoundingClientRect().height > Number.parseFloat(style.lineHeight) * 1.5,
      noHorizontalScroll: textarea.scrollWidth <= textarea.clientWidth + 1,
    };
  })).toEqual({
    overflowWrap: 'anywhere',
    wordBreak: 'break-word',
    wrapped: true,
    noHorizontalScroll: true,
  });
});
